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
# already running, whichever Opus (or Fable) that is. One exception, Explore,
# the read-only search agent: since Claude Code 2.1.280 it inherits the session
# model (capped at opus), and the classifier rates nearly every real search
# brief complex (11 of 12 sampled, at 0.93 or higher), so every search ran on
# the frontier model. Claude Code ran Explore on Haiku before that. An Explore
# spawn that names no model therefore takes the rung below on complex: sonnet,
# never a move up or level. An Explore brief too long to classify takes the
# same rung, which is the ceiling of every decision that ladder allows.
# NADIR_EXPLORE_COMPLEX=inherit turns all of it off.
#
# Transport and malformed-response failures leave the spawn untouched. A
# model-policy denial from the server blocks it.
# NADIR_TIMEOUT defaults to 5s. Fan-out and longer inputs can take much longer
# than a short isolated decision; a shorter cap can lose routing decisions.
#
# An oversized routing input ABSTAINS; it is never truncated. This hook used to
# clip the prompt at 2000 characters and classify the prefix, so a task whose
# only hard constraint sat in its tail ("...and it must handle the Decimal
# context correctly") got routed on an easy-looking opening and downgraded. A
# prefix is not the task, and a wrong cheap pick costs far more than a missed
# saving, so the limit is a decision boundary rather than a buffer.
#
# Only the server tokenizer can verify that the classifier saw the whole
# input. Automatic changes require input_truncated=false and encoder_tokens>0.
# NADIR_MAX_PROMPT_CHARS (default 1362) is only a round-trip prefilter; it is
# not derived from the currently serving analyzer. Raising it does not widen
# that analyzer's window or bypass the server coverage check.
#
# Abstention is the same fail-open shape as every other failure path here: exit
# 0 with no stdout, and the spawn keeps whatever model it already had.
#
# Env: NADIR_BUCKET_URL, NADIR_TIMEOUT, NADIR_MAX_PROMPT_CHARS, NADIR_AGENT_POLICY (raw JSON), NADIR_ROUTE_DISABLE=1,
# NADIR_TURNS_BY_TYPE (JSON subagent_type -> expected turns; generate it from your
# own transcripts with `backend/measure_agent_turns.py --json`, never borrow one),
# NADIR_EXPECTED_TURNS (flat fallback for types absent from that map),
# NADIR_SPAWN_CACHED_TOKENS (size of the prompt-cache prefix a spawn on the
# baseline model actually shares; absent means unknown, explicit 0 means cold),
# NADIR_CACHE_TTL (5m|1h, default 5m), NADIR_CURRENT_INPUT_TOKENS,
# NADIR_CACHE_AGE_SECONDS (age at request start; refresh each call),
# CLAUDE_EFFORT (set by the harness, not by you: the session's effort level,
# forwarded as baseline_effort so the top tier is never told to think less),
# NADIR_CLAUDE_LADDER (tier->alias JSON; a tier mapped to "inherit" or omitted
# is left untouched), NADIR_EXPLORE_COMPLEX (alias a complex Explore search
# takes, default sonnet; inherit keeps it on the session model), NADIR_API_KEY (keyed mode: your account's saved agent
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

# The model the named agent's own definition sets. A spawn that names no model
# runs on THAT, not on the session model, so it is the ceiling a rewrite is
# measured against. Compared against the session instead, a Haiku agent
# (nadir-simple from the installer tier pack, caveman:cavecrew-investigator,
# the built-in claude-code-guide) read as an Opus spawn, and a medium decision
# moved it UP to Sonnet. Lookup order follows Claude Code: project
# .claude/agents, then user agents, then the built-ins below; a plugin agent
# ("plugin:name") only in that plugin agents/ directory. Empty when the agent
# inherits or no definition is found, which keeps the session comparison.
# ponytail: an agent defined outside files (--agents JSON, the SDK) is not
# seen and still compares against the session model.
NADIR_AGENT_MODEL=$(printf '%s' "$hook_input" | python3 -c '
import json, os, re, sys
from pathlib import Path

# Claude Code 2.1.280 built-ins that set a model; every other built-in inherits.
BUILTIN = {"claude-code-guide": "haiku", "statusline-setup": "sonnet"}
QUOTES = "\"" + chr(39)


def defined_model(root, name):
    """The model field of the agent called name under root; "" if it sets none, None if absent."""
    for path in sorted(root.rglob("*.md")) if root.is_dir() else ():
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        fields = dict(re.findall(r"(?m)^(name|model):[ \t]*(.*?)[ \t]*$", text[3:].split("\n---", 1)[0]))
        if fields.get("name", path.stem).strip(QUOTES) == name:
            return fields.get("model", "").strip(QUOTES)
    return None


try:
    hook = json.load(sys.stdin)
    ti = hook.get("tool_input") or {}
    kind = ti.get("subagent_type")
    if ti.get("model") or not isinstance(kind, str) or not kind:
        raise SystemExit
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    plugin, _, name = kind.rpartition(":")
    if plugin:
        installed = json.loads((config / "plugins" / "installed_plugins.json").read_text()).get("plugins") or {}
        roots = [Path(entry["installPath"]) / "agents"
                 for key, entries in installed.items() if key.split("@")[0] == plugin
                 for entry in (entries if isinstance(entries, list) else [entries])
                 if isinstance(entry, dict) and entry.get("installPath")]
    else:
        project = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd")
        roots = ([Path(project) / ".claude" / "agents"] if project else []) + [config / "agents"]
    model = next((m for m in (defined_model(root, name) for root in roots) if m is not None), None)
    if model is None and not plugin:
        model = BUILTIN.get(name)
except Exception:
    raise SystemExit
model = (model or "").strip().lower()
if model and model != "inherit":
    print(next((a for a in ("haiku", "sonnet", "opus", "fable")
                if model == a or model.startswith("claude-" + a + "-")), model))
' 2>/dev/null) || NADIR_AGENT_MODEL=""
export NADIR_AGENT_MODEL

# The baseline is the model THIS session runs, and the session states it.
#
# Claude Code fills tool_input.model only when a spawn names one explicitly, so
# an inherited spawn arrives with no model at all. That leaves the decision
# unpriced (the dashboard reads "N decisions, $0.00") and leaves the ladder with
# nothing to compare against, which is why NADIR_BASELINE_MODEL existed: a
# hand-set env var that goes stale the moment the user switches model.
#
# It does not have to be hand-set. The harness passes transcript_path on every
# hook event, and the last main-thread assistant turn in that file carries the
# real API id. Read it once here and export it, so both python blocks below keep
# reading NADIR_BASELINE_MODEL exactly as before and an explicit value still
# wins. ONLY the model id is read; message text is never parsed.
#
# Sidechain turns are skipped deliberately: they are subagents, and a previous
# Haiku subagent must not be mistaken for the session baseline, which would make
# the hook believe it is already at the cheap tier and stop routing entirely.
#
# The transcript is appended asynchronously, so a spawn issued by the FIRST
# assistant turn of a session reaches this hook before that turn is on disk,
# usually before the file exists at all. With no baseline an inherited spawn
# keeps its model, so those spawns went unrouted: 9 of 10 when Claude Code
# 2.1.282 was driven by the scripted API in test_route_native.py, where the
# turn landed 10-36 ms after the hook started in 12 of 12 runs. An inherited
# spawn therefore waits up to BASELINE_WAIT_SECONDS for a main-thread turn to
# appear. Once one exists nothing waits, so only a session's first spawns pay
# it; a session that persists no transcript pays the whole wait, and
# NADIR_BASELINE_MODEL skips it.
if [ -z "$NADIR_BASELINE_MODEL" ]; then
    NADIR_BASELINE_MODEL=$(printf '%s' "$hook_input" | python3 -c '
import json, os, sys, time
from pathlib import Path

ALIASES = ("haiku", "sonnet", "opus", "fable")
BASELINE_WAIT_SECONDS = 1.0
try:
    hook = json.load(sys.stdin)
    ti = hook.get("tool_input") or {}
    # An explicit spawn model is what the routing compares against, so the
    # baseline only feeds pricing context there and is not worth a wait.
    wait = (0 if isinstance(ti, dict) and (ti.get("model") or os.environ.get("NADIR_AGENT_MODEL"))
            else BASELINE_WAIT_SECONDS)
except Exception:
    raise SystemExit


def locate():
    path = str(hook.get("transcript_path") or "")
    if path:
        return path
    # Older harness builds omit the field. The session id names the same file,
    # and globbing it avoids reimplementing the project-directory slug.
    session = str(os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
    if not session or "/" in session or "\\" in session or session in (".", ".."):
        raise SystemExit
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    try:
        found = sorted(root.glob("*/" + session + ".jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return str(found[0]) if found else None


def last_main_model(path):
    try:
        # Tail only: these files reach tens of megabytes and the answer is at
        # the end. A turn split by the boundary fails to parse and the next one
        # back is used, which is why this walks backwards rather than taking
        # the first hit.
        with open(path, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 262144))
            raw = stream.read()
    except OSError:
        return None
    for line in reversed(raw.split(b"\n")):
        if b"assistant" not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        model = (message or {}).get("model") if isinstance(message, dict) else None
        # An alias here would buy a pricing_unknown plan, which is worse than
        # sending nothing, so it is dropped the same way a hand-set one is.
        if isinstance(model, str) and model and model.lower() not in ALIASES:
            return model
    return None


deadline = time.monotonic() + wait
while True:
    path = locate()
    model = last_main_model(path) if path else None
    if model or time.monotonic() >= deadline:
        break
    time.sleep(0.02)
if model:
    print(model)
' 2>/dev/null) || NADIR_BASELINE_MODEL=""
    export NADIR_BASELINE_MODEL
fi

req=$(printf '%s' "$hook_input" | python3 -c '
import json, math, os, sys, time

# Round-trip prefilter only; the server coverage check is authoritative.
MAX_PROMPT_CHARS = 1362


def _route_log(entry):
    # One metadata line in the local route log (NADIR_ROUTE_LOG, default
    # ~/.nadir/route-log.jsonl, off disables it). Never prompt text.
    path = os.environ.get("NADIR_ROUTE_LOG") or os.path.join(os.path.expanduser("~"), ".nadir", "route-log.jsonl")
    if path.strip().lower() in ("off", "0", "false", "no"):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 5000000:
            os.replace(path, path + ".1")
        entry = dict({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "harness": "claude-code"}, **entry)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass

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
    _asked = ti.get("model") or None
    _spawn_log = {"event": "spawn", "session": hook.get("session_id"), "tool_use_id": hook.get("tool_use_id"),
                  "subagent_type": ti.get("subagent_type"), "requested": _asked,
                  "agent_model": os.environ.get("NADIR_AGENT_MODEL") or None,
                  "session_model": os.environ.get("NADIR_BASELINE_MODEL") or None,
                  "tier": None, "nadir_pick": None, "applied": None, "why": "brief too long to classify"}
    try:
        _pin = str((json.loads(os.environ["NADIR_AGENT_POLICY"]) or {}).get("subagent") or "").strip().lower()
    except Exception:
        _pin = ""  # unset, malformed, or not an object: no pin to apply
    # "auto"/"passthrough" are not pins, they are "let the router decide", and
    # the router is exactly what we just declined to consult.
    if _pin in ("haiku", "sonnet", "opus", "fable") and _pin != str(ti.get("model") or "").strip().lower():
        ti["model"] = _pin
    else:
        # An inherited Explore search too long to classify still takes its rung.
        # Under the Explore ladder no tier sits above that rung, so it is the
        # most conservative answer a decision could have given; a decision only
        # ever moves such a search lower. Only on a session ranked above it.
        _ranks = ("haiku", "sonnet", "opus", "fable")
        _rung = (os.environ.get("NADIR_EXPLORE_COMPLEX") or "sonnet").strip().lower()
        _base = (os.environ.get("NADIR_BASELINE_MODEL") or "").strip().lower()
        _seat = next((a for a in _ranks if _base == a or _base.startswith("claude-" + a + "-")), None)
        if not (ti.get("subagent_type") == "Explore" and not ti.get("model") and _rung in _ranks
                and _seat and _ranks.index(_seat) > _ranks.index(_rung)):
            _route_log(_spawn_log)
            raise SystemExit
        ti["model"] = _rung
        _spawn_log["why"] = "Explore rung, brief too long to classify"
    if _spawn_log["why"] == "brief too long to classify":
        _spawn_log["why"] = "policy pin, brief too long to classify"
    _spawn_log["applied"] = ti["model"]
    _route_log(_spawn_log)
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
    # `model` in the tool input only when the spawn names one explicitly; other
    # spawns run on their agent definition model (NADIR_AGENT_MODEL) or inherit
    # the session model, which the hook cannot see, so NADIR_BASELINE_MODEL
    # supplies it. Without a baseline the server has
    # nothing to price the decision against and every row logs unpriced — the
    # dashboard reads "N decisions, $0.00", which is the opposite of the point.
    "role": "subagent",
    "requested_model": (ti.get("model") or os.environ.get("NADIR_AGENT_MODEL")
                        or os.environ.get("NADIR_BASELINE_MODEL") or ""),
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
# The warm model, resolved once: `context.warm_model` and the `cache` block
# below are the same claim about the same model, so they share one guard.
#
# NADIR_BASELINE_MODEL only, and never ti["model"]: that field is the Agent
# tool ENUM (haiku|sonnet|opus|fable), and a bare alias has no price entry, so
# passing one buys a `pricing_unknown` plan that defaults to `delegate` without
# costing anything -- strictly worse than sending nothing at all. Aliases are
# filtered here too, in case a baseline was set to one by hand.
_warm = (os.environ.get("NADIR_BASELINE_MODEL") or "").strip()
if _warm.lower() in ("haiku", "sonnet", "opus", "fable"):
    _warm = ""
ctx = {}
if _turns and _turns > 0:
    # `warm_model` has to ride along or the horizon prices nothing: with no
    # inline alternative the plan reports `no_warm_model` and never runs the
    # cost comparison the turn count exists to feed. Same value already sent as
    # `requested_model`, so it cannot move the advisory saving.
    ctx["expected_turns"] = min(_turns, 100)
    if _warm:
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
# Struggle counters for the PARENT thread, read from the transcript the harness
# names in `transcript_path`. This is the only place the signal is reachable: a
# PostToolUse tool_response carries no error field (120 sampled locally showed
# only completed/async_launched), but the transcript carries `is_error` on
# tool_result blocks, which is exactly what the proxy tier already scans.
#
# Content-free by construction -- four ints, never a tool name, an id, or any
# message text -- so the payload is safe under any store_prompts setting. It
# mirrors backend/app/services/agent_roles.py:scan_struggle; the thresholds live
# server-side in struggle_reason and are deliberately NOT duplicated here.
#
# Measured on 813 real spawns: fires on 1.35%, all tool_churn. Rare on purpose.
# It only ever SUSPENDS a downgrade, so a false negative costs nothing beyond
# current behaviour and a false positive costs one un-downgraded spawn.
def _struggle(path):
    # Tail read, never the whole file: transcripts reach 26.8 MB here, which is
    # 57ms to read whole against 0.3ms for the last 256KB, inside a hook whose
    # whole budget is seconds and whose failure mode is losing the decision.
    # A tail holding fewer messages than the window under-counts, which
    # fails toward "not struggling" -- i.e. toward current behaviour.
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 262144))
        chunk = fh.read()
    if size > 262144:
        chunk = chunk.split(b"\n", 1)[-1]  # drop the partial first line
    window = []
    for raw in chunk.splitlines():
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        # Sidechain records are a SUBAGENT struggling, not this thread. Counting
        # them would let one failing child suspend downgrades for its parent.
        if rec.get("isSidechain"):
            continue
        window.append(rec)
    window = window[-20:]
    names, per_tool = {}, {}
    err = run = longest = tot = 0
    saw = False
    for rec in window:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                saw = True
                names[b.get("id")] = b.get("name")
            elif b.get("type") == "tool_result":
                saw = True
                tot += 1
                if b.get("is_error"):
                    err += 1
                    run += 1
                    if run > longest:
                        longest = run
                    n = names.get(b.get("tool_use_id"))
                    if n:
                        per_tool[n] = per_tool.get(n, 0) + 1
                else:
                    run = 0
    if not saw:
        # No tool blocks in the window: a statement about the window SHAPE, not
        # a measurement. Absent, never zeros -- zeros read downstream as
        # "measured, and this thread was fine".
        return None
    return {
        "err": err,
        "run": longest,
        "rep": max(per_tool.values()) if per_tool else 0,
        "tot": tot,
    }


_tp = hook.get("transcript_path")
if isinstance(_tp, str) and _tp:
    try:
        _s = _struggle(_tp)
    except Exception:
        _s = None  # unreadable, truncated, moved: never lose the decision over it
    if _s:
        body["struggle"] = _s

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

# Unknown cache is no block, never a fabricated zero or an omitted token
# count inside a block (the API assumes that means fully cached). Measurements
# must describe this spawn on the baseline model, not another model/session.
_current = str(ti.get("model") or _warm).strip()
_same_model = _current == _warm or (
    _current in ("haiku", "sonnet", "opus", "fable")
    and _warm.startswith("claude-" + _current + "-"))
if _warm and _same_model:
    try:
        _cached = int(os.environ["NADIR_SPAWN_CACHED_TOKENS"])
        _ttl = (os.environ.get("NADIR_CACHE_TTL") or "5m").strip().lower()
        if not 0 <= _cached <= 1000000 or _ttl not in ("5m", "1h"):
            raise ValueError("invalid cache state")
        _cache = {"model": _warm, "cached_tokens": _cached, "ttl": _ttl}
        for _env, _field, _parse, _ceiling in (
            ("NADIR_CURRENT_INPUT_TOKENS", "current_input_tokens", int, 1000000),
            ("NADIR_CACHE_AGE_SECONDS", "seconds_since_last_request", float, 86400),
        ):
            if os.environ.get(_env):
                _value = _parse(os.environ[_env])
                if not math.isfinite(_value) or not 0 <= _value <= _ceiling:
                    raise ValueError("invalid cache measurement")
                _cache[_field] = _value
        if _cached > _cache.get("current_input_tokens", 1000000):
            raise ValueError("cached prefix exceeds input")
        if _turns and _turns > 0:
            _cache["expected_remaining_turns"] = min(_turns, 100)
        body["cache"] = _cache
    except (KeyError, TypeError, ValueError, OverflowError):
        pass  # Unusable measurements stay unknown; the routing call still runs.
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
# Explore, the read-only search agent, takes a complex rung (see the header).
# Only an inherited spawn: a model named on the call is left to the caller, and
# an explicit complex entry in NADIR_CLAUDE_LADDER wins.
if ti.get("subagent_type") == "Explore" and not ti.get("model"):
    _ladder.setdefault("complex", (os.environ.get("NADIR_EXPLORE_COMPLEX") or "sonnet").strip().lower())
# Declare the same no-upgrade boundary the response handler enforces. Otherwise
# an inherited Haiku session advertises a Sonnet swap that it will never apply.
_aliases = ("haiku", "sonnet", "opus", "fable")
_current = str(ti.get("model") or os.environ.get("NADIR_AGENT_MODEL")
               or os.environ.get("NADIR_BASELINE_MODEL") or "").strip().lower()
_current_alias = next((a for a in _aliases if _current == a or _current.startswith("claude-" + a + "-")), None)
for _tier, _model in _ladder.items():
    _alias = str(_model).strip().lower()
    if (_current_alias is None or
            (_alias in _aliases and _aliases.index(_alias) >= _aliases.index(_current_alias))):
        _ladder[_tier] = "inherit"
# A Nadir worker (the agents this plugin ships, or nadir-simple and nadir-medium
# from the installer tier pack) is a model and effort the agent chose itself,
# with the read from Nadir in hand, so the choice stands. The decision is still
# asked for, so the dashboard shows the Nadir tier beside the choice, but every
# tier is declared no-change and nothing is priced as moved.
_pick = str(ti.get("subagent_type") or "")
if _pick.startswith("nadir-route:") or _pick in ("nadir-simple", "nadir-medium"):
    _ladder = {_tier: "inherit" for _tier in _ladder}
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
    resp=$(printf '%s' "$req" | curl -s -m "${NADIR_TIMEOUT:-5}" -X POST \
        "${NADIR_BUCKET_URL:-https://api.getnadir.com/v1/bucket}" \
        -H 'Content-Type: application/json' -H "X-API-Key: $NADIR_API_KEY" \
        --data-binary @- -w '\nNADIR_HTTP_STATUS:%{http_code}') || true
else
    resp=$(printf '%s' "$req" | curl -s -m "${NADIR_TIMEOUT:-5}" -X POST \
        "${NADIR_BUCKET_URL:-https://api.getnadir.com/v1/bucket}" \
        -H 'Content-Type: application/json' --data-binary @- \
        -w '\nNADIR_HTTP_STATUS:%{http_code}') || true
fi
# A failed call (timeout, refused, DNS) still reaches the handler below with
# status 000, so the route log records it; the spawn is left untouched either way.

printf '%s' "$resp" | NADIR_HOOK_INPUT="$hook_input" python3 -c '
import atexit, builtins, json, os, sys, time


def _route_log(entry):
    # One metadata line in the local route log (NADIR_ROUTE_LOG, default
    # ~/.nadir/route-log.jsonl, off disables it). Never prompt text.
    path = os.environ.get("NADIR_ROUTE_LOG") or os.path.join(os.path.expanduser("~"), ".nadir", "route-log.jsonl")
    if path.strip().lower() in ("off", "0", "false", "no"):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 5000000:
            os.replace(path, path + ".1")
        entry = dict({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "harness": "claude-code"}, **entry)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# Logged once this handler exits, whatever path it took. Every decision it
# emits goes through print, so wrapping print records exactly what the harness
# was told to run rather than what this file meant to tell it.
try:
    _hook = json.loads(os.environ["NADIR_HOOK_INPUT"])
    _ti0 = _hook.get("tool_input") or {}
except Exception:
    _hook, _ti0 = {}, {}
LOG = {"event": "spawn", "session": _hook.get("session_id"), "tool_use_id": _hook.get("tool_use_id"),
       "subagent_type": _ti0.get("subagent_type"), "requested": _ti0.get("model") or None,
       "agent_model": os.environ.get("NADIR_AGENT_MODEL") or None,
       "session_model": os.environ.get("NADIR_BASELINE_MODEL") or None,
       "tier": None, "nadir_pick": None, "applied": None}
atexit.register(lambda: _route_log(LOG))


def print(text, *args, **kwargs):
    try:
        out = json.loads(text).get("hookSpecificOutput") or {}
        LOG["applied"] = ((out.get("updatedInput") or {}).get("model")
                          if out.get("permissionDecision") == "allow" else "DENIED")
    except Exception:
        pass
    builtins.print(text, *args, **kwargs)

# The Agent tool accepts these four and nothing else. Ranked cheapest first so
# the hook can refuse to move a spawn UP: this is a cost-control hook, and
# raising a model the agent already picked is the one thing it must never do.
RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
# complex is deliberately absent: the top tier keeps the session default.
LADDER = {"simple": "haiku", "medium": "sonnet"}

try:
    payload, _, status = sys.stdin.read().rpartition("\nNADIR_HTTP_STATUS:")
    if status == "403":
        detail = json.loads(payload).get("detail")
        if isinstance(detail, dict) and detail.get("error") == "model_not_allowed":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Nadir organization model policy denied this spawn.",
            }}))
        raise SystemExit
    if status != "200":
        LOG["why"] = ("no decision (timeout or network error)" if status in ("", "000")
                      else "no decision (HTTP %s)" % status)
        raise SystemExit
    body = json.loads(payload)
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
    # Same isinstance discipline: a 200 whose cache_advice is a string, a list
    # or null must fail open and route normally, not raise on the .get() below.
    adv = body.get("cache_advice")
    adv = adv if isinstance(adv, dict) else {}
    LOG["tier"] = tier or None
    LOG["nadir_pick"] = body.get("selected_model") if isinstance(body.get("selected_model"), str) else None
except Exception:
    raise SystemExit

governed = body.get("governance_model")
if governed is not None:
    # A denied requested model must be replaced even when classifier coverage
    # is unknown. The Agent tool only accepts aliases. Verify the exact model
    # behind an alias before applying a governed full ID.
    target = (body.get("selected_model") if "selected_model" in body
              else rr.get("model")) if rr.get("decided_by") == "policy" else governed
    alias = str(target).strip().lower()
    if alias not in RANK:
        alias = next((name for name in RANK
                      if os.environ.get("ANTHROPIC_DEFAULT_" + name.upper() + "_MODEL") == target), "")
    if alias and alias != str(ti.get("model") or "").strip().lower():
        ti["model"] = alias
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "updatedInput": ti,
        }}))
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Nadir cannot apply the required organization model replacement.",
        }}))
    raise SystemExit

# A suspended escalation outranks EVERYTHING below, including the ladder.
#
# This is the half that makes `struggle` mean anything. The server answers a
# struggling thread by handing back the model the caller named, and stamping
# `escalation: "suspended:<reason>"`, but it does that inside `role_resolution`
# and it flips `decided_by` to "passthrough" — which is not the policy branch,
# so control would fall straight through to the tier ladder and downgrade the
# spawn anyway. Sending the counters without this check is inert: the server
# would suspend the downgrade and the hook would re-apply it one line later.
if str(rr.get("escalation") or "").startswith("suspended:"):
    raise SystemExit

# An explicit policy pin is a standing instruction from the user, so it wins
# over truncation and cache advice too: those qualify an automatic decision,
# while the pin is independent of the classifier input. Modern responses put
# the final pin in selected_model; legacy ones carry it only in role_resolution.
# It is still held to the enum: a full model id cannot be expressed here.
if rr.get("decided_by") == "policy":
    pinned = str(body.get("selected_model") if "selected_model" in body
                 else rr.get("model") or "").strip().lower()
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

# A Nadir worker is a choice the agent made itself (see the request block).
# Organization governance and an explicit policy pin, both above, still apply.
_pick = str(ti.get("subagent_type") or "")
if _pick.startswith("nadir-route:") or _pick in ("nadir-simple", "nadir-medium"):
    LOG["why"] = "the agent picked this worker"
    raise SystemExit

# THE correctness check for automatic routing. The server owns the tokenizer,
# so it is the only party that knows whether the decision was made from a
# prefix. Missing/unknown coverage is not permission to change models.
tokens = body.get("encoder_tokens")
if body.get("input_truncated") is not False or type(tokens) is not int or tokens <= 0:
    # Same rule as the oversized prefilter: an inherited Explore search the
    # classifier could not see whole takes its rung, the ceiling of every
    # decision the Explore ladder allows, unless the cache verdict says stay.
    # Only on an affirmative input_truncated: missing or malformed coverage is
    # a bad response, and a bad response fails open like every other one.
    rung = (os.environ.get("NADIR_EXPLORE_COMPLEX") or "sonnet").strip().lower()
    base = os.environ.get("NADIR_BASELINE_MODEL", "").strip().lower()
    seat = next((name for name in RANK if base == name or base.startswith("claude-" + name + "-")), "")
    if (body.get("input_truncated") is True
            and ti.get("subagent_type") == "Explore" and not ti.get("model") and rung in RANK
            and seat and RANK[seat] > RANK[rung] and adv.get("decision") != "stay_warm"):
        ti["model"] = rung
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": ti,
        }}))
    raise SystemExit

# The cache verdict, and only `stay_warm`: every other decision (no_conflict,
# no_warm_cache, cache_expired, insufficient_data) leaves the router pick
# standing by definition. cache_advice is ADVISORY on /v1/bucket -- the server
# computes and logs it, but selected_model is decided independently of it and is
# never moved by it -- so honouring the verdict is the job of THIS hook. Do not
# carry that assumption to /v1/recommend, which applies it server-side.
#
# BELOW the policy pin on purpose. A pin is a standing instruction from the
# user, and this file already lets it beat the tier ladder including an up-move;
# a cache verdict is a cost estimate, and a cost estimate does not get to
# overrule what the user asked for. Above the ladder, which is the only thing it
# may veto.
if adv.get("decision") == "stay_warm":
    raise SystemExit

ladder = dict(LADDER)
# `export NADIR_CLAUDE_LADDER=` is how a profile usually clears a var, and it is
# not a KeyError -- json.loads("") raises, which used to kill the whole hook.
_raw_ladder = os.environ.get("NADIR_CLAUDE_LADDER", "").strip()
if _raw_ladder:
    try:
        ladder.update(json.loads(_raw_ladder))
    except Exception:
        raise SystemExit  # a malformed ladder is not a licence to guess
# The Explore complex rung, mirrored for responses that carry no selected_model.
if ti.get("subagent_type") == "Explore" and not ti.get("model"):
    ladder.setdefault("complex", (os.environ.get("NADIR_EXPLORE_COMPLEX") or "sonnet").strip().lower())

# Lowercased so a ladder written {"simple":"HAIKU"} routes instead of silently
# no-opping; the requested-model check below is already case-insensitive.
# Modern responses already include policy, governance and no-change handling.
# Null or an unrepresentable full ID means no rewrite, never a tier fallback.
alias = str(body.get("selected_model") if "selected_model" in body else ladder.get(tier) or "").strip().lower()
if alias not in RANK:
    # Unmapped tier ("complex"), "inherit", or a value the tool would reject.
    raise SystemExit

# A named model, else the agent definition model, else the session model.
current = str(ti.get("model") or os.environ.get("NADIR_AGENT_MODEL") or "").strip().lower()
if not current:
    baseline = os.environ.get("NADIR_BASELINE_MODEL", "").strip().lower()
    # Only compare the tier here; never invent a concrete model from an alias.
    current = next((name for name in RANK if baseline == name or baseline.startswith("claude-" + name + "-")), "")
pinned = rr.get("decided_by") == "policy"
if current == alias:
    raise SystemExit
if current in RANK and RANK[current] <= RANK[alias] and not pinned:
    LOG["why"] = "already at or below the routed tier"
    raise SystemExit
if current not in RANK and not pinned:
    # Unknown inherited baselines and unrankable explicit models both stay:
    # without their tier, a replacement could increase cost.
    raise SystemExit

ti["model"] = alias  # updatedInput REPLACES tool_input, so echo every field
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": ti,
}}))
'
exit 0
