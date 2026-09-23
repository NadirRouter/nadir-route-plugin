#!/usr/bin/env python3
"""Install or run Nadir's local Claude Code session/compaction hooks.

No model calls, network or transcript rewrites. Policy and configuration are
pinned when a fresh session starts. Existing/forked sessions without a pin
remain untouched. Raw archives persist until the user removes that session.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
from compact_hook import audit, preview_read, rewrite

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "references/cost-discipline.md"
HOOK_LABEL = "Nadir context"


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("state directory must be a private directory (mode 0700)")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("state directory must belong to the current user")


def write_json(path, value, *, replace=False):
    """Publish a complete private file atomically; first session initializer wins."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def handle(event, *, state_dir, policy_file=DEFAULT_POLICY, max_chars=8000, mode="structural"):
    if not isinstance(event, dict) or os.environ.get("NADIR_CONTEXT_DISABLE") == "1":
        return {}
    kind, session_id = event.get("hook_event_name"), event.get("session_id")
    if kind not in ("SessionStart", "SubagentStart", "PreToolUse", "PostToolUse"):
        return {}
    if not isinstance(session_id, str) or not 0 < len(session_id) <= 1024:
        return {}
    if not 1024 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 1024 and 100000")
    if mode not in ("preview", "structural"):
        raise ValueError("unknown compaction mode")
    key = hashlib.sha256(session_id.encode()).hexdigest()
    directory = Path(state_dir) / key
    path = directory / "session.json"
    source = event.get("source")
    fresh = kind == "SessionStart" and source in ("startup", "clear")
    if not path.exists() and not fresh:
        return {}
    private_directory(Path(state_dir))
    private_directory(directory)
    if fresh and (not path.exists() or source == "clear"):
        policy = ""
        if policy_file is not None:
            with Path(policy_file).open("rb") as text:
                raw = text.read(32769)
            if len(raw) > 32768:
                raise ValueError("policy exceeds 32 KiB")
            policy = raw.decode("utf-8").strip()
        digest = hashlib.sha256(policy.encode()).hexdigest()
        config = {"schema": 1, "mode": mode, "max_chars": max_chars,
                  "policy": policy, "policy_sha256": digest}
        created = write_json(path, config, replace=source == "clear")
        if not created:
            return {}
    else:
        if path.is_symlink():
            raise ValueError("session configuration must not be a symlink")
        config = json.loads(path.read_text(encoding="utf-8"))
        if fresh or (kind == "SessionStart" and source != "compact"):
            return {}  # Startup retries and resumes cannot add another policy.
    if (not isinstance(config, dict) or config.get("schema") != 1
            or config.get("mode") not in ("preview", "structural") or type(config.get("max_chars")) is not int
            or not 1024 <= config["max_chars"] <= 100000 or not isinstance(config.get("policy"), str)
            or hashlib.sha256(config["policy"].encode()).hexdigest() != config.get("policy_sha256")):
        raise ValueError("invalid pinned session configuration")
    if kind in ("SessionStart", "SubagentStart"):
        policy = config["policy"]
        # Claude Code deduplicates SubagentStart context and restores it after
        # native compaction. Return the same bytes; do not build a second cache.
        return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": policy}} if policy else {}
    if kind == "PreToolUse":
        # Budgeted against the host's read cap, not max_chars: a view that fits
        # 8 KiB would fall back to a head/tail preview and lose the outline.
        output, report = preview_read(event, mode=config["mode"])
    else:
        output, report = rewrite(event, mode=config["mode"], archive_dir=directory, max_chars=config["max_chars"])
    audit(directory / "counts.jsonl", event, dict(report, policy_sha256=config["policy_sha256"]))
    return output


def install(project, *, state_dir, policy_file=DEFAULT_POLICY, max_chars=8000):
    """Merge only our hooks into project-local settings; preserve other hooks."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("project must be an existing directory")
    if not 1024 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 1024 and 100000")
    if policy_file is not None:
        policy_file = Path(policy_file).resolve(strict=True)
        if policy_file.stat().st_size > 32768:
            raise ValueError("policy exceeds 32 KiB")
        policy_file.read_text(encoding="utf-8")
    state_dir = Path(state_dir).absolute()
    private_directory(state_dir)
    settings = project / ".claude/settings.local.json"
    if settings.is_symlink() or settings.parent.is_symlink():
        raise ValueError("project settings must not be a symlink")
    original = settings.read_bytes() if settings.exists() else None
    document = json.loads(original) if original is not None else {}
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise ValueError("settings and hooks must be JSON objects")
    hooks = document.setdefault("hooks", {})
    command = [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(state_dir),
               "--max-chars", str(max_chars)]
    command += ["--policy-file", str(policy_file)] if policy_file is not None else ["--no-policy"]
    nudge = [sys.executable, str(Path(__file__).resolve().with_name("cache_miss_nudge.py")),
             "--state-dir", str(state_dir)]
    for event, matcher, argv in (("SessionStart", "startup|clear|resume|compact", command),
                                 ("SubagentStart", ".*", command), ("PreToolUse", "Read", command),
                                 ("PostToolUse", "Read|Bash", command), ("Stop", None, nudge)):
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} must be an array")
        preserved = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ValueError(f"invalid hooks.{event} entry")
            other = [hook for hook in entry["hooks"] if not isinstance(hook, dict) or hook.get("statusMessage") != HOOK_LABEL]
            if other:
                preserved.append(dict(entry, hooks=other))
        entry = {"hooks": [{"type": "command", "command": shlex.join(argv), "timeout": 5, "statusMessage": HOOK_LABEL}]}
        if matcher is not None:  # Stop takes no matcher
            entry = {"matcher": matcher, **entry}
        preserved.append(entry)
        hooks[event] = preserved
    settings.parent.mkdir(mode=0o700, exist_ok=True)
    if (settings.read_bytes() if settings.exists() else None) != original:
        raise ValueError("settings changed during installation; retry")
    if original is not None:
        backup = settings.with_name("settings.local.json.nadir-backup")
        if backup.is_symlink():
            raise ValueError("settings backup must not be a symlink")
        # Retain the first pre-install snapshot; re-running must not erase it.
        write_json(backup, json.loads(original))
    write_json(settings, document, replace=True)
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-project", type=Path, help="Merge hooks into this project's local settings")
    parser.add_argument("--state-dir", type=Path, required=True, help="Persistent private local archive directory")
    policies = parser.add_mutually_exclusive_group()
    policies.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY, help="Pinned when the session starts; may be an exact upstream policy")
    policies.add_argument("--no-policy", action="store_true", help="Use when a behavior policy such as Ponytail is already active")
    parser.add_argument("--max-chars", type=int, default=8000)
    parser.add_argument("--mode", choices=["structural", "preview"], default="structural", help="Preview is a benchmark control; installation uses structural")
    args = parser.parse_args()
    policy = None if args.no_policy else args.policy_file
    try:
        if args.install_project:
            settings = install(args.install_project, state_dir=args.state_dir, policy_file=policy, max_chars=args.max_chars)
            print(f"Nadir compaction installed in {settings}. Start a fresh session; existing sessions are unchanged.")
        else:
            raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                return
            output = handle(json.loads(raw), state_dir=args.state_dir, policy_file=policy, max_chars=args.max_chars, mode=args.mode)
            if output:
                print(json.dumps(output))
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if args.install_project:
            parser.exit(1, f"Nadir installation failed: {error}.\n")
        print("Nadir context unavailable; original behavior preserved.", file=sys.stderr)


if __name__ == "__main__":
    main()
