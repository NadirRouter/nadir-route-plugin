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
# An oversized routing input ABSTAINS; it is never truncated. This hook used to
# clip the prompt at 2000 characters and classify the prefix, so a task whose
# only hard constraint sat in its tail ("...and it must handle the Decimal
# context correctly") got routed on an easy-looking opening and downgraded. A
# prefix is not the task, and a wrong cheap pick costs far more than a missed
# saving, so the limit is a decision boundary rather than a buffer.
#
# Correctness rests on the SERVER, not on a character count. /v1/bucket runs
# whatever COMPLEXITY_ANALYZER_TYPE names, and that default is `wide_deep_asym`
# (backend/app/settings.py), which encodes with BAAI/bge-base-en-v1.5 at
# max_seq_length 512. Only the server holds that tokenizer, so only the server
# can say whether a decision was made from a prefix: it answers with
# `input_truncated` (and `encoder_tokens`, the real tokenized length), and a
# true there makes this hook discard the decision and emit nothing. No
# client-side constant can do that job, because characters are not tokens.
#
# NADIR_MAX_PROMPT_CHARS is a cheap PRE-FILTER on top of that, default 1362, and
# nothing more: it saves a round trip on an input that is PROBABLY too long.
# 1362 is 512 tokens at 2.66 chars/token, the WORST ratio measured over the
# frozen coding-agent task corpus through the real BGE tokenizer (median 3.58).
# It errs in BOTH directions and neither is a correctness problem:
#   - too eager: measured on that corpus, 2 of 12 prompts it rejects actually fit
#     (sqlparse__per_call_lexer 2007 chars / 511 tokens, and
#     textdistance__lcsstr_long_input_cost 1993 / 469). Those spawns keep their
#     model and the saving is lost. Raise the var to trade round trips for them.
#   - too lax: code-dense text (diffs, stack traces, file listings) reaches
#     2.1 chars/token, where 1362 characters is already 649 tokens and past the
#     window. The server catches those with `input_truncated`.
# Raising it does not widen the encoder, it only hands more inputs to the server,
# which then reports `input_truncated` and this hook abstains anyway.
#
# Abstention is the same fail-open shape as every other failure path here: exit
# 0 with no stdout, and the spawn keeps whatever model it already had.
#
# Env: NADIR_BUCKET_URL, NADIR_TIMEOUT, NADIR_MAX_PROMPT_CHARS, NADIR_AGENT_POLICY (raw JSON), NADIR_ROUTE_DISABLE=1,
# NADIR_TURNS_BY_TYPE (JSON subagent_type -> expected turns; generate it from your
# own transcripts with `backend/measure_agent_turns.py --json`, never borrow one),
# NADIR_EXPECTED_TURNS (flat fallback for types absent from that map),
# CLAUDE_EFFORT (set by the harness, not by you: the session's effort level,
# forwarded as baseline_effort so the top tier is never told to think less),
# NADIR_CLAUDE_LADDER (tier->alias JSON; a tier mapped to "inherit" or omitted
# is left untouched), NADIR_API_KEY (keyed mode: your account's saved agent
# policy governs the decision and it shows up in the dashboard's Engine
# decisions; an explicit NADIR_AGENT_POLICY still wins over the account policy).

# Every early exit drains stdin first, so the harness never sees EPIPE on a
# large tool_input it is still writing.
[ "$NADIR_ROUTE_DISABLE" = "1" ] && { cat >/dev/null 2>&1; exit 0; }

# CLAUDE_CODE_SUBAGENT_MODEL outranks a hook's rewrite, so anything but
# `inherit` makes this hook a guaranteed no-op. Bail before spending a decision
# the spawn will discard -- and, more importantly, before the server prices a
# saving the dashboard would then report for a model that never changed.
case "${CLAUDE_CODE_SUBAGENT_MODEL:-inherit}" in
    inherit) ;;
    *) cat >/dev/null 2>&1; exit 0 ;;
esac

hook_input=$(cat)

req=$(printf '%s' "$hook_input" | python3 -c '
import json, os, sys

# The deployed analyzer (wide_deep_asym) encodes with bge-base-en-v1.5 at 512
# tokens and stops. 1362 characters is that window at 2.66 chars/token, the
# worst ratio measured on the frozen coding-agent corpus. A PRE-FILTER to save
# a round trip, never the correctness check -- `input_truncated` on the
# response is, because only the server holds the tokenizer. It over-rejects:
# 2 of the 12 frozen prompts it stops (511 and 469 tokens) would have fit.
MAX_PROMPT_CHARS = 1362

try:
    hook = json.load(sys.stdin)
    ti = hook.get("tool_input") or {}
    # Matchers are regex, so a future broadening of "Agent" could route another
    # tool through here; writing `model` into an input whose schema forbids it
    # fails validation, and Claude Code turns a failed rewrite into a denied call.
    if hook.get("tool_name") not in ("Agent", "Task", None):
        raise SystemExit
    # str(): a list/int prompt would otherwise be forwarded as-is (422) or raise.
    prompt = str(ti.get("prompt") or ti.get("description") or "")
except Exception:
    raise SystemExit
if not prompt:
    raise SystemExit
# Abstain rather than classify a prefix, and abstain BEFORE the call so an
# oversized spawn costs no decision and books no priced row against text that
# is not the task.
#
# An EXPLICIT NADIR_AGENT_POLICY pin survives the abstention. It is client-side
# data this hook already parses, so honouring it needs no request at all, and a
# standing instruction from the user should not be collateral damage of a
# length check. Only when it names a value the Agent tool enum can express: a
# pin naming a full model id ("claude-haiku-4-5") would fail updatedInput
# validation, and a failed validation DENIES the spawn, so that one still falls
# through to abstention.
#
# A KEYED account-side policy is genuinely lost here, and it is not recoverable
# from this side: it lives on the account and only ever arrives inside a
# /v1/bucket response, which also carries selected_model -- a server-side choice
# made from whatever text we sent. There is no ask-for-the-pin-alone shape, and
# asking would mean sending the prefix we just refused to route on.
try:
    _max_chars = int(os.environ.get("NADIR_MAX_PROMPT_CHARS") or 0)
except (TypeError, ValueError):
    _max_chars = 0
# Junk, empty and non-positive all fall back to the default: an operator who
# fat-fingers this var must not silently end up with the truncation hole back.
if _max_chars <= 0:
    _max_chars = MAX_PROMPT_CHARS
if len(prompt) > _max_chars:
    try:
        _pin = str((json.loads(os.environ["NADIR_AGENT_POLICY"]) or {}).get("subagent") or "").strip().lower()
    except Exception:
        _pin = ""  # unset, malformed, or not an object: no pin to apply
    # "auto"/"passthrough" are not pins, they are "let the router decide", and
    # the router is exactly what we just declined to consult.
    if _pin in ("haiku", "sonnet", "opus", "fable") and _pin != str(ti.get("model") or "").strip().lower():
        ti["model"] = _pin
        # EMIT tags this as the FINAL answer of this hook, not a request body:
        # the abstained path never reaches the response handler that normally
        # prints, and the shell would otherwise POST this to /v1/bucket.
        print("EMIT " + json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": ti,
        }}))
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
# The harness already knows what KIND of agent this is, and it is the grouping
# key for the one number the decision is still guessing at: how many turns a run
# of this kind takes. Recorded server-side, never parsed -- users name their own
# agents, so any meaning read into the string would be an invention of ours.
_kind = ti.get("subagent_type")
if isinstance(_kind, str) and _kind:
    body["subagent_type"] = _kind[:200]

# The join key for the outcome report. Claude Code passes the SAME tool_use_id
# to PreToolUse and PostToolUse, so sending it here is what lets report-outcome.sh
# attach the measured turn count to THIS decision without either hook keeping
# state. Opaque id, never content. Ignored server-side on keyless calls, which
# store no row for an outcome to attach to.
_tuid = hook.get("tool_use_id")
if isinstance(_tuid, str) and _tuid:
    body["tool_use_id"] = _tuid[:200]

# Turn estimate. Not shipped as a default table anywhere: over 880 real runs on
# one machine the median was 1 turn for one agent type and 92 for another, so a
# borrowed table is worse than the floor it replaces. Measure your own with
# `backend/measure_agent_turns.py --json` and export the result.
#   NADIR_TURNS_BY_TYPE   {"Explore": 28, "general-purpose": 10}
#   NADIR_EXPECTED_TURNS  a flat fallback for types not in that map
_turns = None
try:
    _turns = json.loads(os.environ["NADIR_TURNS_BY_TYPE"]).get(_kind)
except Exception:
    pass
if _turns is None:
    _turns = os.environ.get("NADIR_EXPECTED_TURNS")
try:
    _turns = int(_turns)
except (TypeError, ValueError):
    _turns = None
ctx = {}
if _turns and _turns > 0:
    # `warm_model` has to ride along or the horizon prices nothing: with no
    # inline alternative the plan reports `no_warm_model` and never runs the
    # cost comparison the turn count exists to feed. Same value already sent as
    # `requested_model`, so it cannot move the advisory saving.
    #
    # NADIR_BASELINE_MODEL only, and never ti["model"]: that field is the Agent
    # tool ENUM (haiku|sonnet|opus|fable), and a bare alias has no price entry,
    # so passing one as warm_model buys a `pricing_unknown` plan that defaults to
    # `delegate` without costing anything -- strictly worse than sending no
    # context at all. Aliases are filtered here too, in case a baseline was set
    # to one by hand.
    ctx["expected_turns"] = min(_turns, 100)
    _warm = (os.environ.get("NADIR_BASELINE_MODEL") or "").strip()
    if _warm and _warm.lower() not in ("haiku", "sonnet", "opus", "fable"):
        ctx["warm_model"] = _warm

# Effort. The harness states its own, so nobody has to guess it: `baseline_effort`
# is a FLOOR on the top tier, and its whole job is to stop a routing layer telling
# the hardest work to think less than the session already does. Guessed wrong in
# the cheap direction it silently downgrades that work; guessed at all, it is a
# claim about the user session that only the harness can actually make.
#
# Two sources, both from the harness. The hook payload carries
# `effort: {"level": ...}`, built only when the session model supports effort at
# all and already reduced to the level that model will really run
# ("after any silent downgrade for the selected model"). CLAUDE_EFFORT is the
# same value exposed to hook commands as an env var. Absent means the session
# model takes no effort parameter, which is exactly when nothing should be sent.
_effort = hook.get("effort")
if isinstance(_effort, dict):
    _effort = _effort.get("level")
if not isinstance(_effort, str) or not _effort:
    _effort = os.environ.get("CLAUDE_EFFORT")
# Held to the API enum before it is sent. `baseline_effort` is a Literal, so an
# unrecognised level is a 422 -- and a 422 does not degrade the decision, it
# loses it. A level this hook has never heard of is dropped, not forwarded.
if isinstance(_effort, str) and _effort.strip().lower() in (
        "low", "medium", "high", "xhigh", "max"):
    ctx["baseline_effort"] = _effort.strip().lower()

if ctx:
    body["context"] = ctx
# Declare the tiers this hook can actually apply, so the server prices the rest
# as no-change. `complex` is deliberately absent (the top tier keeps the session
# model), and an audit-mode ladder of {"simple":"inherit","medium":"inherit"}
# now books zero instead of a full savings figure for changing nothing.
_ladder = {"simple": "haiku", "medium": "sonnet"}
_raw_ladder = os.environ.get("NADIR_CLAUDE_LADDER", "").strip()
if _raw_ladder:
    try:
        _ladder.update(json.loads(_raw_ladder))
    except Exception:
        pass
# Declare the same no-upgrade boundary the response handler enforces. Otherwise
# an inherited Haiku session advertises a Sonnet swap that it will never apply.
_aliases = ("haiku", "sonnet", "opus", "fable")
_current = str(ti.get("model") or os.environ.get("NADIR_BASELINE_MODEL") or "").strip().lower()
_current_alias = next((a for a in _aliases if _current == a or _current.startswith("claude-" + a + "-")), None)
if _current_alias:
    for _tier, _model in _ladder.items():
        _alias = str(_model).strip().lower()
        if _alias in _aliases and _aliases.index(_alias) >= _aliases.index(_current_alias):
            _ladder[_tier] = "inherit"
body["ladder"] = _ladder
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

# The abstained path can still carry a final answer: an explicit env pin is
# client-side data and needs no decision. Tagged so it is never mistaken for a
# request body and POSTed as a prompt.
case "$req" in
    "EMIT "*) printf '%s\n' "${req#EMIT }"; exit 0 ;;
esac

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
    # str()/isinstance guards: a 200 whose fields are the wrong JSON type must
    # fail open like any other bad response. Without them a list `tier` raises
    # TypeError at the ladder lookup and a string `role_resolution` raises
    # AttributeError below -- both outside this try, both spraying a traceback.
    plan = body.get("plan")
    plan = plan if isinstance(plan, dict) else {}
    tier = str(body.get("routing_tier") or plan.get("tier") or body.get("bucket") or "")
    rr = body.get("role_resolution")
    rr = rr if isinstance(rr, dict) else {}
except Exception:
    raise SystemExit

# THE correctness check. The server owns the tokenizer, so it is the only party
# that knows whether the decision was made from a prefix: `input_truncated` says
# it was, and a tier chosen from the opening of a task is not a tier for the
# task. Discard it and let the spawn keep its model. The character pre-filter
# upstream is only a proxy for this -- at 2.1 chars/token a prompt that passed
# it is still 649 tokens against a 512-token window -- so the guarantee lives
# here, not there. Truthy rather than `is True`: a strange value fails toward
# abstention, which is the cheap direction. An absent field is an older backend
# that does not report truncation, and abstaining on every one of those calls
# would disable the hook against every deployment but the newest.
if body.get("input_truncated"):
    raise SystemExit

# An explicit policy pin is a standing instruction from the user, so it wins
# over the tier ladder — including on complex, and including an up-move. It is
# still held to the enum: a policy naming a full model id ("claude-haiku-4-5")
# cannot be expressed on this surface at all, so fall through to the ladder
# rather than emit a value that would deny the spawn.
if "selected_model" not in body and rr.get("decided_by") == "policy":
    pinned = str(rr.get("model") or "").strip().lower()
    if pinned in RANK:
        if pinned != str(ti.get("model") or "").strip().lower():
            ti["model"] = pinned
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": ti,
            }}))
        raise SystemExit
    # A policy naming a full model id cannot be expressed on this enum, so fall
    # through to the ladder rather than do nothing at all. The shipped Settings
    # presets use full ids, so terminating here disabled spawn routing outright
    # for every keyed pilot that picked one.

ladder = dict(LADDER)
# `export NADIR_CLAUDE_LADDER=` is how a profile usually clears a var, and it is
# not a KeyError -- json.loads("") raises, which used to kill the whole hook.
_raw_ladder = os.environ.get("NADIR_CLAUDE_LADDER", "").strip()
if _raw_ladder:
    try:
        ladder.update(json.loads(_raw_ladder))
    except Exception:
        raise SystemExit  # a malformed ladder is not a licence to guess

# Lowercased so a ladder written {"simple":"HAIKU"} routes instead of silently
# no-opping; the requested-model check below is already case-insensitive.
# Modern responses already include policy, governance and no-change handling.
# Null or an unrepresentable full ID means no rewrite, never a tier fallback.
alias = str(body.get("selected_model") if "selected_model" in body else ladder.get(tier) or "").strip().lower()
if alias not in RANK:
    # Unmapped tier ("complex"), "inherit", or a value the tool would reject.
    raise SystemExit

current = str(ti.get("model") or "").strip().lower()
if not current:
    baseline = os.environ.get("NADIR_BASELINE_MODEL", "").strip().lower()
    # Only compare the tier here; never invent a concrete model from an alias.
    current = next((name for name in RANK if baseline == name or baseline.startswith("claude-" + name + "-")), "")
pinned = rr.get("decided_by") == "policy"
if current == alias:
    raise SystemExit
if current in RANK and RANK[current] <= RANK[alias] and not pinned:
    raise SystemExit  # already at or below the routed tier
if current and current not in RANK and not pinned:
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
