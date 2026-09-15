#!/usr/bin/env python3
"""Opt-in Claude Code PostToolUse compression; no history or source edits.

Install only at a new-session boundary. Native updatedToolOutput must be
supported by the host. Read/Bash output shapes are validated conservatively;
unknown shapes, partial reads, recovery reads and failed tools stay unchanged.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

sys.dont_write_bytecode = True
from compact_context import compact_file, python_view

# Claude Code serves one Read page up to this many tokens and truncates the
# rest. A file past it never reaches PostToolUse whole, so `rewrite` preserves
# the truncated page and the reader silently loses everything below the cut.
READ_TOKEN_CAP = 25000
# ponytail: parsing is linear but the installed hook is given 5s, and ~10 MiB of
# Python takes ~6s to parse. Decline past this rather than time the hook out on
# every read; raise it only alongside a faster view.
MAX_VIEW_BYTES = 4 * 1024 * 1024
PYTHON_SUFFIXES = {".py", ".pyi"}

CODE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".sh", ".swift", ".kt"}


def rewrite(event, *, mode, archive_dir, max_chars=8000):
    if mode not in ("preview", "structural"):
        raise ValueError("unknown compression mode")
    if not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse":
        return {}, {"reason": "unsupported_event"}
    response, args = event.get("tool_response"), event.get("tool_input")
    if not isinstance(response, dict) or not isinstance(args, dict):
        return {}, {"reason": "unsupported_shape"}
    tool = event.get("tool_name")
    code_language = None
    if tool == "Read":
        file = response.get("file")
        if response.get("type") != "text" or not isinstance(file, dict):
            return {}, {"reason": "unsupported_read"}
        path = args.get("file_path")
        if isinstance(path, str) and (str(Path(archive_dir).resolve()) in path or "nadir-context-" in path):
            return {}, {"reason": "recovery_preserved"}
        if not isinstance(path, str) or Path(path).suffix.lower() not in CODE_SUFFIXES:
            return {}, {"reason": "unsupported_language"}
        if (args.get("offset") is not None or args.get("limit") is not None
                or file.get("startLine") != 1 or file.get("truncatedByTokenCap")
                or type(file.get("numLines")) is not int or file["numLines"] < 1
                or file.get("numLines") != file.get("totalLines")):
            return {}, {"reason": "partial_read_preserved"}
        original = file.get("content")
        code_language = "python" if mode == "structural" and Path(path).suffix.lower() in (".py", ".pyi") else None
    elif tool == "Bash":
        if (response.get("stderr") != "" or response.get("interrupted") is not False
                or response.get("isImage") is not False or response.get("exitCode", 0) != 0):
            return {}, {"reason": "diagnostics_preserved"}
        command = args.get("command")
        if not isinstance(command, str):
            return {}, {"reason": "unsupported_command"}
        # A recovery through cat/sed/rg must never be compressed again.
        if str(Path(archive_dir).resolve()) in command or "nadir-context-" in command:
            return {}, {"reason": "recovery_preserved"}
        original = response.get("stdout")
        try:
            words = shlex.split(command)
        except ValueError:
            return {}, {"reason": "unsupported_command"}
        # ponytail: only a single plain cat is a known complete source capture.
        program = Path(words[0]).name if words else ""
        programs = {Path(word).name for word in words}
        # Conservatively recognize wrapped commands too (cd ... && git diff).
        if "git" in programs and any(word in ("diff", "show") for word in words):
            return {}, {"reason": "patch_preserved"}
        if (len(words) == 2 and program == "cat" and words[1].endswith(".py")
                and not words[1].startswith("-")):
            code_language = "python" if mode == "structural" else None
        elif programs.intersection(("cat", "sed", "head", "tail", "rg", "grep", "awk")):
            return {}, {"reason": "targeted_read_preserved"}
    else:
        return {}, {"reason": "unsupported_tool"}
    if not isinstance(original, str) or "[Nadir tool-output preview:" in original:
        return {}, {"reason": "unsupported_or_existing_view"}
    if tool == "Bash" and (original.startswith("diff --git ") or "\ndiff --git " in original
                           or ("\n--- " in "\n" + original and "\n+++ " in "\n" + original)):
        return {}, {"reason": "patch_preserved"}
    if len(original) <= max_chars:
        return {}, {"reason": "below_limit"}
    # Capture the tool result, not a second read from a file that may have changed.
    with tempfile.NamedTemporaryFile(dir=archive_dir) as captured:
        captured.write(original.encode("utf-8"))
        captured.flush()
        preview, report = compact_file(captured.name, exit_code=0, max_chars=max_chars,
                                      archive_dir=archive_dir, code_language=code_language,
                                      summarize_tests=tool == "Bash" and code_language is None)
        if report["reason"] == "code_view_exceeds_budget":
            # Auto-navigation can fall back to the existing preview. Explicit
            # --keep-symbol requests use compact_file directly and stay exact.
            preview, report = compact_file(captured.name, exit_code=0, max_chars=max_chars,
                                          archive_dir=archive_dir)
            report["structural_fallback"] = "code_view_exceeds_budget"
    if not report["compacted"]:
        return {}, report
    updated = copy.deepcopy(response)
    if tool == "Read":
        updated["file"].update(content=preview, numLines=preview.count("\n") + 1)
    else:
        updated["stdout"] = preview
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}, report


def preview_read(event, *, mode, read_token_cap=READ_TOKEN_CAP):
    """Serve a structural view in place of a Read the host would truncate.

    PostToolUse cannot cover this: its input is already the truncated page, so
    `rewrite` preserves it as a partial read. Only whole-file reads of Python
    source estimated past the cap are served; the view carries original source
    lines and the file stays on disk, so recovery is a narrowed re-read rather
    than an archive. Every other Read passes through untouched.
    """
    if mode != "structural":
        return {}, {"reason": "preview_mode_not_served"}
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse":
        return {}, {"reason": "unsupported_event"}
    if event.get("tool_name") != "Read":
        return {}, {"reason": "unsupported_tool"}
    args = event.get("tool_input")
    if not isinstance(args, dict):
        return {}, {"reason": "unsupported_shape"}
    # A narrowed read is already the recovery path; never intercept one.
    if args.get("offset") is not None or args.get("limit") is not None:
        return {}, {"reason": "targeted_read_preserved"}
    path = args.get("file_path")
    if not isinstance(path, str) or Path(path).suffix.lower() not in PYTHON_SUFFIXES:
        return {}, {"reason": "unsupported_language"}
    try:
        # chars/4 under-counts dense source, so this fires only well past the
        # cap. A file just under it keeps today's behavior instead of losing
        # exact bodies the reader could have had.
        if Path(path).stat().st_size > MAX_VIEW_BYTES:
            return {}, {"reason": "oversized_preserved"}
        raw = Path(path).read_bytes()
        if len(raw) // 4 <= read_token_cap:
            return {}, {"reason": "below_read_cap"}
        original = raw.decode("utf-8")
    except (OSError, ValueError):
        return {}, {"reason": "unreadable_preserved"}  # let Read report it
    view = python_view(original)
    if view is not None and len(view) // 4 > read_token_cap:
        view = python_view(original, lean=True) or view
    # A view no smaller than the page the host would serve buys nothing.
    if view is None or len(view) >= len(original):
        return {}, {"reason": "code_structure_unavailable"}
    lines = original.count("\n") + 1
    reason = (
        f"Nadir served a structural view of {path} ({lines} lines) instead of a full read, "
        f"which this host would have truncated mid-file at {read_token_cap} tokens.\n"
        "Line numbers in the [source lines N-M] markers are the ORIGINAL file, not this text. "
        "Bodies are omitted: this is not valid replacement code. Re-read "
        f"{path} with offset/limit for the exact lines before editing or quoting them.\n\n"
        + view
    )
    return ({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "deny",
                                    "permissionDecisionReason": reason}},
            {"reason": "structural_view_served", "compacted": True,
             "original_chars": len(original), "returned_chars": len(reason)})


def audit(path, event, report):
    record = dict(report, tool_name=event.get("tool_name"), tool_use_id=event.get("tool_use_id"))
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "a") as log:
        log.write(json.dumps(record) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["preview", "structural"], required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--max-chars", type=int, default=8000)
    parser.add_argument("--audit-log", type=Path, help="Private local counts/recovery receipts; no tool text")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            return
        event = json.loads(raw)
        if not isinstance(event, dict):
            return
        output, report = rewrite(event, mode=args.mode, archive_dir=args.archive_dir, max_chars=args.max_chars)
        if args.audit_log:
            audit(args.audit_log, event, report)
        if output:
            print(json.dumps(output))
    except (OSError, ValueError, TypeError, RecursionError):
        # A failed local transform cannot replace or block the original tool result.
        print("Nadir compression unavailable; original tool result preserved.", file=sys.stderr)


if __name__ == "__main__":
    main()
