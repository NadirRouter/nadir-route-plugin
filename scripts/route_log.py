#!/usr/bin/env python3
"""Show the local Nadir route log: what Nadir decided, and which model then ran.

    python3 route_log.py          # the last 30 events
    python3 route_log.py -f       # and keep following
    python3 route_log.py -n 100 --path ~/.nadir/route-log.jsonl

The hooks write it (metadata only, never prompt text) to NADIR_ROUTE_LOG,
default ~/.nadir/route-log.jsonl:
  prompt        Nadir's tier and pick for a user prompt, and delegate or stay
  spawn         Nadir's tier and pick for a subagent, and the model applied
  spawn_result  the model the subagent actually ran on
  turn          the model the main thread ran on
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime


def describe(e):
    kind = e.get("event")
    session = str(e.get("session") or "")[:8]
    if kind == "prompt":
        conf = e.get("confidence")
        conf = f" {conf:.2f}" if isinstance(conf, (int, float)) else ""
        if e.get("why"):
            what = f"prompt   {e['why']}  session {e.get('session_model') or '(first prompt)'}  {e.get('action')}"
        else:
            what = (f"prompt   tier {e.get('tier')}{conf}  Nadir -> {e.get('nadir_pick')}  "
                    f"session {e.get('session_model') or '(first prompt)'}  {e.get('action')}")
    elif kind == "spawn":
        applied = e.get("applied") or "unchanged"
        tier = f"tier {e.get('tier')}  Nadir -> {e.get('nadir_pick')}" if e.get("tier") else "no decision"
        why = f"  ({e['why']})" if e.get("why") else ""
        agent = e.get("subagent_type") or "agent"
        if e.get("agent_model"):
            agent += f" (defines {e['agent_model']})"
        what = f"spawn    {agent}  {tier}  applied {applied}{why}"
    elif kind == "spawn_result":
        what = f"result   subagent ran on {e.get('resolved_model')}"
    elif kind == "turn":
        what = f"turn     main thread ran on {e.get('main_model')}"
    else:
        what = json.dumps(e)
    try:
        stamp = datetime.fromisoformat(str(e.get("ts"))).astimezone().strftime("%H:%M:%S")
    except ValueError:
        stamp = "--:--:--"
    return f"{stamp}  {e.get('harness', '?'):<11}  {session:<8}  {what}"


def show(line):
    try:
        print(describe(json.loads(line)), flush=True)
    except (ValueError, AttributeError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=30, help="how many recent events to show")
    parser.add_argument("-f", "--follow", action="store_true", help="keep printing new events")
    parser.add_argument("--path", default=os.environ.get("NADIR_ROUTE_LOG")
                        or os.path.join(os.path.expanduser("~"), ".nadir", "route-log.jsonl"))
    args = parser.parse_args()
    path = os.path.expanduser(args.path)
    if not os.path.exists(path) and not args.follow:
        sys.exit(f"no route log yet at {path}: it appears after the first routed prompt or spawn")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    for line in lines[-args.n:]:
        show(line)
    if not args.follow:
        return
    offset = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        time.sleep(0.5)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        if size < offset:  # rotated
            offset = 0
        if size > offset:
            with open(path, encoding="utf-8") as fh:
                fh.seek(offset)
                chunk = fh.read()
            offset = size
            for line in chunk.splitlines():
                show(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
