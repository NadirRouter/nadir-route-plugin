#!/bin/sh
# Nadir PreToolUse hook for the Claude Code `Agent` tool: ask /v1/bucket which
# tier this spawn needs, then rewrite `model` via hookSpecificOutput.updatedInput.
#
# The Agent tool's `model` is an ENUM of harness aliases (sonnet|opus|haiku|
# fable) — a full model id fails Claude Code's updatedInput schema check, and a
# failed check DENIES the spawn. So this hook only ever emits an alias, and the
# alias resolves to whatever concrete version the user's own config names
# (ANTHROPIC_DEFAULT_HAIKU_MODEL etc.), which keeps the pick inside their family
# and their generation.
#
# `complex` is never rewritten: the top tier stays on the model the session is
# already running, whichever Opus (or Fable) that is.
#
# Fail-open by construction: every failure path exits 0 with NO stdout, which
# Claude Code reads as "no decision" and the spawn proceeds untouched.
# NADIR_TIMEOUT defaults to 5s, not 2s: measured against prod, a 10-way parallel
# spawn fan-out takes 1.87-2.28s per call (one shared 1-vCPU worker serializes
# the classifier), so a 2s cap silently lost 9 of 10 decisions in a fan-out —
# exactly the traffic shape this hook exists for. A spawn that will run for
# minutes can afford the wait; losing its decision costs the whole measurement.
#
# Env: NADIR_BUCKET_URL, NADIR_TIMEOUT, NADIR_AGENT_POLICY (raw JSON), NADIR_ROUTE_DISABLE=1,
# NADIR_CLAUDE_LADDER (tier->alias JSON; a tier mapped to "inherit" or omitted
# is left untouched), NADIR_API_KEY (keyed mode: your account's saved agent
# policy governs the decision and it shows up in the dashboard's Engine
# decisions; an explicit NADIR_AGENT_POLICY still wins over the account policy).

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
body = {
    "prompt": prompt,
    # Tag the channel so spawn decisions are separable from the skill and from
    # ad-hoc API calls on the dashboard. Without it everything logs as "direct"
    # and a pilot cannot show which decisions came from its coding agents.
    "source": "claude-code-hook",
    # The model this spawn would have run on without Nadir. Claude Code puts
    # `model` in the tool input only when the spawn names one explicitly;
    # default spawns inherit the session model, and the hook cannot see it, so
    # NADIR_BASELINE_MODEL supplies it. Without a baseline the server has
    # nothing to price the decision against and every row logs unpriced — the
    # dashboard reads "N decisions, $0.00", which is the opposite of the point.
    "role": "subagent",
    "requested_model": ti.get("model") or os.environ.get("NADIR_BASELINE_MODEL") or "",
}
try:
    body["agent_policy"] = json.loads(os.environ["NADIR_AGENT_POLICY"])
except Exception:
    # Keyed calls inherit the account policy server-side; keyless ones have
    # no account, so default to letting the router decide.
    if not os.environ.get("NADIR_API_KEY"):
        body["agent_policy"] = {"subagent": "auto"}
print(json.dumps(body))
') || exit 0
[ -n "$req" ] || exit 0

if [ -n "$NADIR_API_KEY" ]; then
    resp=$(printf '%s' "$req" | curl -s -f -m "${NADIR_TIMEOUT:-5}" -X POST \
        "${NADIR_BUCKET_URL:-https://api.getnadir.com/v1/bucket}" \
        -H 'Content-Type: application/json' -H "X-API-Key: $NADIR_API_KEY" \
        --data-binary @-) || exit 0
else
    resp=$(printf '%s' "$req" | curl -s -f -m "${NADIR_TIMEOUT:-5}" -X POST \
        "${NADIR_BUCKET_URL:-https://api.getnadir.com/v1/bucket}" \
        -H 'Content-Type: application/json' --data-binary @-) || exit 0
fi

printf '%s' "$resp" | NADIR_HOOK_INPUT="$hook_input" python3 -c '
import json, os, sys

# The Agent tool accepts these four and nothing else. Ranked cheapest first so
# the hook can refuse to move a spawn UP: this is a cost-control hook, and
# raising a model the agent already picked is the one thing it must never do.
RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
# complex is deliberately absent: the top tier keeps the session default.
LADDER = {"simple": "haiku", "medium": "sonnet"}

try:
    body = json.load(sys.stdin)
    ti = json.loads(os.environ["NADIR_HOOK_INPUT"]).get("tool_input") or {}
    tier = (body.get("plan") or {}).get("tier") or body.get("bucket") or ""
    rr = body.get("role_resolution") or {}
except Exception:
    raise SystemExit

# An explicit policy pin is a standing instruction from the user, so it wins
# over the tier ladder — including on complex, and including an up-move. It is
# still held to the enum: a policy naming a full model id ("claude-haiku-4-5")
# cannot be expressed on this surface at all, so fall through to the ladder
# rather than emit a value that would deny the spawn.
if rr.get("decided_by") == "policy":
    pinned = str(rr.get("model") or "").strip().lower()
    if pinned in RANK and pinned != str(ti.get("model") or "").strip().lower():
        ti["model"] = pinned
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": ti,
        }}))
    raise SystemExit

try:
    ladder = dict(LADDER)
    ladder.update(json.loads(os.environ["NADIR_CLAUDE_LADDER"]))
except KeyError:
    pass
except Exception:
    raise SystemExit  # a malformed ladder is not a licence to guess

alias = ladder.get(tier)
if alias not in RANK:
    # Unmapped tier ("complex"), "inherit", or a value the tool would reject.
    raise SystemExit

current = str(ti.get("model") or "").strip().lower()
if current in RANK and RANK[current] <= RANK[alias]:
    raise SystemExit  # already at or below the routed tier
if current and current not in RANK:
    # The agent asked for something this hook cannot rank (a full model id, or
    # a value from a newer harness). Leave it: replacing it could easily be an
    # upgrade, and guessing its rank is how a cost hook starts costing money.
    raise SystemExit

ti["model"] = alias  # updatedInput REPLACES tool_input, so echo every field
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": ti,
}}))
'
exit 0
