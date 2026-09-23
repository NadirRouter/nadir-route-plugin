#!/usr/bin/env python3
"""Stop hook: say what a prompt-cache miss cost and, when it will recur, suggest /compact.

Nothing in Claude Code lets a hook start compaction, so this is a nudge. On a
miss it tells the user how many tokens were re-written and why. When the cause
is an idle gap on a large context it points at /compact, the one pattern where
compaction pays: measured on 999 local sessions, two thirds of re-cached tokens
follow a gap longer than the cache TTL, half of those sessions hit a second
gap, and the median re-write is ~294k tokens at the write rate.

Reads only usage counts, timestamps, the model id and row types from the
transcript the harness names; never message text. State lives in the same
private per-session directory the compaction hooks use. No network, no model.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from context_session import private_directory, write_json

MIN_PREFIX = 20_000
# ponytail: one fixed floor; a per-model break-even needs prices this hook does not have.
NUDGE_CONTEXT = 150_000
# A session older than the hook starts from its tail: the count begins at install.
FIRST_SCAN = 2_000_000
TTL_SECONDS = {"1h": 3600, "5m": 300}


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _row(record):
    """Usage of a main-thread assistant turn, or None for anything else."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message") or {}
    usage = message.get("usage") or {}
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    if read + write + int(usage.get("input_tokens") or 0) <= 0:
        return None
    t = _stamp(record.get("timestamp"))
    if t is None:
        return None
    detail = usage.get("cache_creation") or {}
    ttl = "1h" if detail.get("ephemeral_1h_input_tokens") else ("5m" if detail.get("ephemeral_5m_input_tokens") else None)
    return {"t": t, "read": read, "write": write, "ttl": ttl, "model": message.get("model"), "id": message.get("id")}


def scan(transcript, state):
    """Advance over the transcript lines not yet seen; return the last miss, if any."""
    miss, last, compact_seen = None, state.get("last"), False
    with open(transcript, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        offset = int(state.get("offset") or 0)
        if offset > size:
            offset = 0
        if offset == 0 and size > FIRST_SCAN:
            stream.seek(size - FIRST_SCAN)
            stream.readline()
            offset = stream.tell()
        else:
            stream.seek(offset)
        raw = stream.read()
    end = raw.rfind(b"\n")
    if end < 0:
        return None
    for line in raw[:end].split(b"\n"):
        if b'"usage"' not in line and b"compact_boundary" not in line and b"isCompactSummary" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if (record.get("type") == "system" and record.get("subtype") == "compact_boundary") or (
                record.get("type") == "user" and record.get("isCompactSummary")):
            compact_seen = True
            continue
        row = _row(record)
        # The harness writes one line per content block, all with the same message id.
        if row is None or (last and row["id"] and row["id"] == last.get("id")):
            continue
        if row["ttl"] is None and last:
            row["ttl"] = last.get("ttl")
        if last:
            prefix = last["read"] + last["write"]
            if prefix > MIN_PREFIX and row["read"] < prefix / 2:
                gap = row["t"] - last["t"]
                if gap > TTL_SECONDS.get(last.get("ttl") or "5m", 300):
                    cause = "idle"
                    state["idle_misses"] = int(state.get("idle_misses") or 0) + 1
                elif compact_seen:
                    cause = "compact"
                elif row["model"] != last.get("model"):
                    cause = "model_switch"
                else:
                    cause = "rewrite"
                if cause != "compact":
                    # Session totals; report-outcome.sh forwards the delta with the next spawn outcome.
                    state["misses"] = int(state.get("misses") or 0) + 1
                    state["recache_tokens"] = int(state.get("recache_tokens") or 0) + row["write"]
                miss = {"cause": cause, "gap": gap, "write": row["write"], "context": row["read"] + row["write"],
                        "from": last.get("model"), "to": row["model"]}
        compact_seen, last = False, row
    state["last"], state["offset"] = last, offset + end + 1
    return miss


def message(miss, state):
    if miss is None or miss["cause"] == "compact":
        return None
    k = lambda n: f"{n / 1000:.0f}k"
    if miss["cause"] == "idle":
        gap = int(miss["gap"])
        why = f"idle {gap // 3600}h{gap % 3600 // 60:02d}m, past the cache TTL"
    elif miss["cause"] == "model_switch":
        why = f"model switched from {miss['from']} to {miss['to']}"
    else:
        why = "conversation re-cached while the system prompt stayed warm; an effort, thinking or tool change between turns does this"
    text = f"Nadir: prompt cache miss, re-wrote {k(miss['write'])} tokens at the write rate ({why})."
    if miss["cause"] == "idle" and miss["context"] >= NUDGE_CONTEXT:
        n = int(state.get("idle_misses") or 1)
        text += (f" This session has paid that {n} time{'s' if n != 1 else ''} and its context is ~{k(miss['context'])} tokens."
                 " Run /compact before the next break to shrink the next re-write.")
    return text


def handle(event, *, state_dir):
    if not isinstance(event, dict) or os.environ.get("NADIR_CONTEXT_DISABLE") == "1":
        return {}
    if event.get("hook_event_name") != "Stop":
        return {}
    session_id, transcript = event.get("session_id"), event.get("transcript_path")
    if not isinstance(session_id, str) or not 0 < len(session_id) <= 1024:
        return {}
    if not isinstance(transcript, str) or not transcript:
        return {}
    directory = Path(state_dir) / hashlib.sha256(session_id.encode()).hexdigest()
    private_directory(Path(state_dir))
    private_directory(directory)
    path = directory / "cache.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    try:
        miss = scan(transcript, state)
    except OSError:
        return {}
    write_json(path, state, replace=True)
    text = message(miss, state)
    return {"systemMessage": text} if text else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="Persistent private local state directory")
    args = parser.parse_args()
    try:
        output = handle(json.loads(sys.stdin.read() or "{}"), state_dir=args.state_dir)
    except Exception as error:  # a nudge must never fail the turn
        print(f"nadir cache nudge: {error}", file=sys.stderr)
        output = {}
    if output:
        json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
