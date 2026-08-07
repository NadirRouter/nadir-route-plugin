#!/bin/sh
# Nadir PreToolUse hook for the Claude Code `Agent` tool: ask /v1/bucket which
# model this spawn needs, then rewrite `model` via hookSpecificOutput.updatedInput.
#
# Fail-open by construction: every failure path exits 0 with NO stdout, which
# Claude Code reads as "no decision" and the spawn proceeds untouched.
# Env: NADIR_BUCKET_URL, NADIR_AGENT_POLICY (raw JSON), NADIR_ROUTE_DISABLE=1.

[ "$NADIR_ROUTE_DISABLE" = "1" ] && exit 0

hook_input=$(cat)

req=$(printf '%s' "$hook_input" | python3 -c '
import json, os, sys
try:
    ti = json.load(sys.stdin).get("tool_input") or {}
    prompt = (ti.get("prompt") or ti.get("description") or "")[:2000]
except Exception:
    raise SystemExit
if not prompt:
    raise SystemExit
try:
    policy = json.loads(os.environ["NADIR_AGENT_POLICY"])
except Exception:
    policy = {"subagent": "auto"}
print(json.dumps({
    "prompt": prompt,
    "role": "subagent",
    "requested_model": ti.get("model") or "",
    "agent_policy": policy,
}))
') || exit 0
[ -n "$req" ] || exit 0

resp=$(printf '%s' "$req" | curl -s -f -m 2 -X POST \
    "${NADIR_BUCKET_URL:-https://api.getnadir.com/v1/bucket}" \
    -H 'Content-Type: application/json' --data-binary @-) || exit 0

printf '%s' "$resp" | NADIR_HOOK_INPUT="$hook_input" python3 -c '
import json, os, sys
try:
    body = json.load(sys.stdin)
    model = (body.get("role_resolution") or {}).get("model") or ""
    if model == "auto":
        # Policy sentinel, not a model id: the router decides, and its decision
        # is the plan ladder entry. Older deployments return the sentinel raw.
        model = (body.get("plan") or {}).get("model") or ""
    ti = json.loads(os.environ["NADIR_HOOK_INPUT"]).get("tool_input") or {}
except Exception:
    raise SystemExit
if not isinstance(model, str) or not model or len(model) > 200 or model == ti.get("model"):
    raise SystemExit
ti["model"] = model  # updatedInput REPLACES tool_input, so echo every field
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": ti,
}}))
'
exit 0
