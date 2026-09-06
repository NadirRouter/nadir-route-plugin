#!/bin/sh
# Nadir PostToolUse hook for the Claude Code `Agent` tool: report what the spawn
# ACTUALLY did back to the decision that routed it.
#
# Why this exists: /v1/bucket prices delegate-vs-inline over a number of turns,
# and with nobody reporting one it assumes a floor of 2. That floor sits below
# the real break-even (4 turns to Haiku, 8 to Sonnet), so an undeclared horizon
# systematically under-delegates. This hook is where a measured turn count comes
# from. It is also the only source of "what did the routed tier actually cost".
#
# The join is `tool_use_id`: Claude Code passes the SAME value to PreToolUse and
# PostToolUse, and route-spawn.sh sends it with the decision, so the server can
# attach this report without either hook keeping state.
#
# TURNS are counted as DISTINCT requestId in the subagent's own transcript. One
# model round trip emits several assistant records (a thinking block, a text
# block, tool calls), so counting records overstates the horizon ~2.4x and
# counting tool-bearing records ~1.47x. `totalToolUseCount` from the harness is
# a TOOL-USE count and is a different quantity again; it is reported separately,
# never as the turn count. Overstating the horizon overstates the case for
# delegating, which is the direction this router is already inclined to favour.
#
# PRIVACY: this reads an ALLOWLIST out of tool_response and forwards nothing
# else. That response carries `prompt`, `content`, `worktreePath` and
# `worktreeBranch` — a denylist would leak the day the CLI adds a field. Only
# counters, a status, and a model id ever leave the machine.
#
# Requires NADIR_API_KEY. An anonymous /v1/bucket decision writes no row by the
# privacy contract, so there is nothing for an outcome to attach to; without a
# key this hook exits silently rather than posting into the void.
#
# Fail-open and non-blocking by construction: every path exits 0 with no stdout,
# and the POST is backgrounded so a slow network never delays the agent.
# Env: NADIR_API_KEY, NADIR_OUTCOME_URL, NADIR_TIMEOUT, NADIR_ROUTE_DISABLE=1.

[ "$NADIR_ROUTE_DISABLE" = "1" ] && { cat >/dev/null 2>&1; exit 0; }
[ -n "$NADIR_API_KEY" ] || { cat >/dev/null 2>&1; exit 0; }

req=$(cat | python3 -c '
import json, os, sys, glob

try:
    hook = json.load(sys.stdin)
    if hook.get("tool_name") not in ("Agent", "Task"):
        raise SystemExit
    tuid = hook.get("tool_use_id")
    resp = hook.get("tool_response")
    if not isinstance(tuid, str) or not tuid or not isinstance(resp, dict):
        raise SystemExit
except Exception:
    raise SystemExit

body = {"tool_use_id": tuid[:200]}

def put(key, value, lo=0):
    # Ints only, and only real ones. A bool is an int in Python and would be
    # silently coerced, so it is excluded explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        return
    if value >= lo:
        body[key] = value

status = resp.get("status")
if isinstance(status, str) and status:
    body["status"] = status[:40]
model = resp.get("resolvedModel")
if isinstance(model, str) and model:
    body["resolved_model"] = model[:200]
if isinstance(hook.get("duration_ms"), int):
    put("duration_ms", hook["duration_ms"])

put("tool_uses", resp.get("totalToolUseCount"))
put("total_tokens", resp.get("totalTokens"))
usage = resp.get("usage")
if isinstance(usage, dict):
    put("input_tokens", usage.get("input_tokens"))
    put("output_tokens", usage.get("output_tokens"))
    put("cache_read_tokens", usage.get("cache_read_input_tokens"))
    put("cache_write_tokens", usage.get("cache_creation_input_tokens"))

# The turn count, from the subagent transcript this run produced. `agentId` is
# on tool_response; its transcript sits beside the parent session file.
agent_id = resp.get("agentId")
transcript = hook.get("transcript_path")
if isinstance(agent_id, str) and agent_id and isinstance(transcript, str) and transcript:
    try:
        session_dir = transcript[:-6] if transcript.endswith(".jsonl") else transcript
        hits = glob.glob(
            os.path.join(session_dir, "subagents", "**", "agent-%s.jsonl" % agent_id),
            recursive=True,
        )
        if hits:
            requests = set()
            with open(hits[0]) as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "assistant" and rec.get("requestId"):
                        requests.add(rec["requestId"])
            if requests:
                body["turns"] = len(requests)
    except Exception:
        pass  # no turn count is better than a wrong one

# A background spawn reports no outcome at all -- no tokens, no duration, no
# tool counts -- so there is nothing to measure and a row of zeroes would read
# as a zero-turn run. Status is still worth recording; anything less is not.
if len(body) < 2:
    raise SystemExit
print(json.dumps(body))
') || exit 0
[ -n "$req" ] || exit 0

# Backgrounded: a PostToolUse hook runs on the agent's critical path, and a
# label is never worth making a user wait for.
printf '%s' "$req" | curl -s -f -m "${NADIR_TIMEOUT:-5}" -X POST \
    "${NADIR_OUTCOME_URL:-https://api.getnadir.com/v1/bucket/outcome}" \
    -H 'Content-Type: application/json' -H "X-API-Key: $NADIR_API_KEY" \
    --data-binary @- >/dev/null 2>&1 &

exit 0
