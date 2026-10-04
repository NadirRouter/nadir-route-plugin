#!/usr/bin/env python3
"""Nadir UserPromptSubmit hook: right-size the user's prompt, not only the spawns.

The spawn hooks only ever fire when the agent decides to delegate, which on a
normal session is rarely, so an installed Nadir routed almost nothing unless
the user asked for subagents by name. This hook closes that gap. On every
prompt it asks /v1/bucket which tier the prompt needs, priced against the model
the session is running, and when a cheaper tier suffices it gives the agent
Nadir's read (the tier odds), its pick, and the cheaper options. The agent
decides: do the work itself, or hand it to a subagent. It sees the whole
conversation and the classifier sees only this prompt, so the pick informs the
choice rather than making it. The main thread keeps its model and its warm
prompt cache; a handoff happens in a fresh subagent, and the spawn hook then
sees that spawn like any other.

Model AND effort. As a plugin, the options are the worker agents shipped in
agents/ (WORKERS below), each a fixed model and effort. That is the only way to
set a subagent's effort in Claude Code: the Agent tool has no effort field, and
its description tells the model to set `model` only when the user asks for one
(2.1.280). Choosing an agent type carries no such rule. Installed without the
plugin there are no workers, and the options are the Agent tool's `model`.

Two harnesses, one file. With NADIR_CODEX_LADDER set it speaks Codex: the
session model is stated on the hook payload, the ladder maps tiers to catalog
slugs, and the directive names spawn_agent with the slug and, on gpt-6 and
gpt-5.6 slugs, the reasoning effort the plan chose. Otherwise it speaks Claude Code:
the session model is read off the transcript (usage accounting only, never
message text), and the directive names the Agent tool with the alias enum.

It never touches the prompt, never blocks it, and never speaks unless a
change is warranted. Every failure path is exit 0 with no output: no baseline,
an oversized prompt, a slash command, a network error, a truncated encoder
view, a warm-cache verdict, a policy pin already in force.

The decision is half the record; the other half is what the turn actually ran.
With a key, each decision carries the session id (X-Nadir-Session-Id) and a
`prompt_<hex>` join id in `tool_use_id`, and the turn's outcome (the model the
main thread ran, its round trips, tool calls and tokens, never text) is posted
to /v1/bucket/outcome against it: at Stop (`--stop`, plugin lane) and again on
the next prompt, which also covers the installer lane that has no Stop hook.

Env: NADIR_ROUTE_DISABLE=1, NADIR_API_KEY (or the plugin's api_key option),
NADIR_BUCKET_URL, NADIR_OUTCOME_URL, NADIR_TIMEOUT, NADIR_CONTEXT_STATE_DIR,
NADIR_MAX_PROMPT_CHARS, NADIR_BASELINE_MODEL (explicit session model; wins),
NADIR_CLAUDE_LADDER / NADIR_CODEX_LADDER, NADIR_AGENT_POLICY, CLAUDE_EFFORT.
"""
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ALIASES = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TIERS = ("simple", "medium", "complex")
MAX_PROMPT_CHARS = 1362
OPAQUE_BRIEF = re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}={0,2}")
# The decision this prompt got, for the local log; filled once /v1/bucket answers.
LAST = {}
# The plugin's worker agents, cheapest first: (agent name, model alias, effort).
# test_route_spawn.py holds this table to agents/*.md so the two cannot drift.
WORKERS = (("haiku", "haiku", None), ("sonnet-low", "sonnet", "low"),
           ("sonnet-medium", "sonnet", "medium"), ("sonnet-high", "sonnet", "high"),
           ("opus-medium", "opus", "medium"))
# The effort a tier gets when the plan states none, as bucket_plan.EFFORT_BY_TIER.
TIER_EFFORT = {"simple": "low", "medium": "medium", "complex": "xhigh"}
# A turn the server's size head expects to take at least this many tool calls is
# long enough that handing routine parts of it off can pay, even when no cheaper
# model fits the whole task. It is the head's first cut (log1p = 2.19): below it
# a turn is short, and doing it inline beats the fixed cost of a fresh subagent.
# ponytail: one fixed cut; tune it against outcome data once handoffs are measured.
LONG_TOOL_CALLS = 8
# Confidence decides who decides (founder, 2026-09-25). When Nadir is confident
# and its cache-aware costing says handing the work off is cheaper, the note is
# an order; below this confidence the agent decides from the information. A
# short prompt ("do 1 and 2") counts as low confidence whatever the classifier
# says: it leans on a conversation the classifier never sees.
ORDER_MIN_CONFIDENCE = 0.8
ORDER_MIN_WORDS = 12


def _usd(value):
    return "under $0.01" if value < 0.01 else "$%.2f" % value


def _plugin_prefix():
    """ "<plugin>:" when this runs from the plugin with its workers beside it, else None."""
    root = Path(__file__).resolve().parents[1]
    try:
        name = json.loads((root / ".claude-plugin" / "plugin.json").read_text()).get("name")
    except Exception:
        return None
    if not isinstance(name, str) or not all((root / "agents" / (w + ".md")).is_file() for w, _, _ in WORKERS):
        return None
    return name + ":"


def _worker_for(alias, effort, tier):
    """The worker running `alias` at the effort nearest the plan's, or None."""
    want = EFFORTS.index(effort if effort in EFFORTS else TIER_EFFORT.get(tier, "high"))
    options = [(w, e) for w, a, e in WORKERS if a == alias]
    if not options:
        return None
    return min(options, key=lambda o: abs(EFFORTS.index(o[1] or "low") - want))[0]


def _odds(resp):
    """ "simple 88%, medium 10%, complex 2%" from the class probabilities, else the confidence."""
    probs = resp.get("probabilities")
    if isinstance(probs, dict) and all(isinstance(probs.get(t), (int, float)) for t in TIERS):
        return ", ".join("%s %d%%" % (t, round(probs[t] * 100)) for t in TIERS)
    try:
        return "confidence %.2f" % float(resp.get("confidence"))
    except (TypeError, ValueError):
        return None


def log_event(entry):
    """Append one metadata line to the local route log; never prompt text.

    ~/.nadir/route-log.jsonl by default, NADIR_ROUTE_LOG=<path> moves it and
    NADIR_ROUTE_LOG=off stops it. It is how a user sees what Nadir decided and
    which model then ran, without a key or a dashboard.
    """
    path = os.environ.get("NADIR_ROUTE_LOG") or os.path.join(os.path.expanduser("~"), ".nadir", "route-log.jsonl")
    if path.strip().lower() in ("off", "0", "false", "no"):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # ponytail: one rotation at 5 MB; a real rotator if anyone needs more history.
        if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
            os.replace(path, path + ".1")
        line = dict({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, **entry)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass


def _last_main_turn(path):
    """Last main-thread assistant entry of a Claude Code transcript, or None.

    Tail read only: transcripts reach tens of megabytes and the answer is at
    the end. Sidechain entries are subagents and are skipped, or a previous
    Haiku child would read as the session baseline and stop routing.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            raw = fh.read()
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
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model.lower() not in ALIASES:
            return entry
    return None


def _cache_block(entry, model, now):
    """The prompt-cache state of the session, whole or not at all.

    Same reading the skill's session_probe takes: read + creation is
    the next request's warm prefix, read + creation + input is the current input, the
    ephemeral split names the TTL, the timestamp gives the age. A block with
    an invented zero would tell the server the cache is cold when it is not.
    """
    usage = ((entry or {}).get("message") or {}).get("usage")
    if not isinstance(usage, dict):
        return None

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    read = count(usage.get("cache_read_input_tokens"))
    if read is None:
        return None
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    made = count(usage.get("cache_creation_input_tokens"))
    fresh = count(usage.get("input_tokens"))
    if made is None or fresh is None:
        return None
    block = {"model": model, "cached_tokens": read + made, "ttl": "5m",
             "current_input_tokens": read + made + fresh}
    if (count(creation.get("ephemeral_1h_input_tokens")) or 0) > (count(creation.get("ephemeral_5m_input_tokens")) or 0):
        block["ttl"] = "1h"
    stamp = entry.get("timestamp")
    if isinstance(stamp, str):
        try:
            then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (now - then).total_seconds()
            if 0 <= age <= 86400:
                block["seconds_since_last_request"] = age
        except ValueError:
            pass
    return block


def _codex_cache(path, model, now):
    """The Codex session prompt-cache state from its rollout, whole or not at all.

    The last `token_count` event carries the previous request: input_tokens
    (cached included, as OpenAI counts it) and cached_input_tokens. Token
    counts and a timestamp only; message text is never parsed.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 262144))
            raw = fh.read()
    except OSError:
        return None
    for line in reversed(raw.split(b"\n")):
        if b"token_count" not in line:
            continue
        try:
            entry = json.loads(line)
            usage = entry["payload"]["info"]["last_token_usage"]
            cached, total = usage["cached_input_tokens"], usage["input_tokens"]
        except Exception:
            continue
        if not all(type(v) is int and v >= 0 for v in (cached, total)) or cached > total:
            return None
        # ponytail: OpenAI keeps a prefix 5-10 minutes idle, so the 5m TTL is the safe floor.
        block = {"model": model, "cached_tokens": cached, "ttl": "5m", "current_input_tokens": total}
        try:
            age = (now - datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))).total_seconds()
            if 0 <= age <= 86400:
                block["seconds_since_last_request"] = age
        except (KeyError, ValueError):
            pass
        return block
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(body, session=None, url=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("NADIR_API_KEY")
    if key:
        headers["X-API-Key"] = key
    # A header, not a body field: /v1/bucket rejects unknown keys, so a body
    # field would 422 every prompt against a server older than this hook.
    if isinstance(session, str) and 0 < len(session) <= 200:
        headers["X-Nadir-Session-Id"] = session
    try:
        timeout = float(os.environ.get("NADIR_TIMEOUT") or 5)
    except ValueError:
        timeout = 5.0
    endpoint = url or ((os.environ.get("NADIR_ROUTE_URL") or "https://api.getnadir.com/v1/route")
                       if body.get("decision_version") == "route-v2" else
                       (os.environ.get("NADIR_BUCKET_URL") or "https://api.getnadir.com/v1/bucket"))
    req = urllib.request.Request(endpoint,
                                 data=data, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
        if resp.status != 200:
            return None
        return json.loads(resp.read(2_000_000))


def _state_path(session):
    """This session's prompt-decision file in the hooks' private state dir, or None."""
    if not isinstance(session, str) or not 0 < len(session) <= 1024:
        return None
    root = os.environ.get("NADIR_CONTEXT_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".nadir", "context")
    return Path(root) / hashlib.sha256(session.encode()).hexdigest() / "prompt-decision.json"


def _turn(path, offset):
    """What the main thread ran since byte `offset` of the transcript, or {}.

    Counters and a model id only, never message text. Turns are distinct
    requestId, as report-outcome.sh counts them. Claude Code writes one record
    per content block and repeats the message's usage on each, so usage is kept
    per message.id, last record wins, and summed once.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size < offset:
                return {}  # the transcript was rewritten (a /compact): nothing to attribute
            # ponytail: the last 32 MB of a turn; a longer one undercounts its start.
            fh.seek(max(offset, size - (32 << 20)))
            raw = fh.read()
    except (OSError, TypeError):
        return {}
    fields = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
              ("cache_read_tokens", "cache_read_input_tokens"), ("cache_write_tokens", "cache_creation_input_tokens"))
    model, last, requests, usages, tools = None, None, set(), {}, 0
    for line in raw.split(b"\n"):
        if b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if isinstance(message.get("model"), str) and message["model"] not in ("", "<synthetic>"):
            model = message["model"]
        if isinstance(entry.get("requestId"), str):
            requests.add(entry["requestId"])
        if isinstance(entry.get("timestamp"), str):
            last = entry["timestamp"]
        content = message.get("content")
        if isinstance(content, list):
            tools += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
        usage = message.get("usage")
        if isinstance(usage, dict):
            usages[message.get("id") or entry.get("uuid")] = usage
    if model is None:
        return {}
    out = {"resolved_model": model[:200], "turns": len(requests), "tool_uses": tools}
    for key, field in fields:
        out[key] = sum(u[field] for u in usages.values() if type(u.get(field)) is int and u[field] >= 0)
    if last:
        out["last"] = last
    return out


def _background(send):
    """Run `send` after the hook has returned, so no user waits on a report."""
    try:
        if os.fork():
            return
    except (AttributeError, OSError):
        try:
            send()  # no fork (Windows): inline, bounded by NADIR_TIMEOUT
        except Exception:
            pass
        return
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)  # release the harness's pipes, or it waits on this child
        send()
    except BaseException:
        pass
    finally:
        os._exit(0)


def report_outcome(hook):
    """Post what the last decided turn ran, against its decision row.

    Keyed only: an anonymous decision writes no row for an outcome to attach
    to. Reports are cumulative from the prompt and the server merges them per
    field, so the Stop report and the next prompt's both land safely; a
    transcript that has not grown since the last report is not sent twice.
    """
    key = os.environ.get("NADIR_API_KEY")
    path = _state_path(hook.get("session_id"))
    transcript = hook.get("transcript_path")
    if not key or path is None or not isinstance(transcript, str):
        return
    try:
        state = json.loads(path.read_text())
        size = os.path.getsize(transcript)
    except (OSError, ValueError):
        return
    join = state.get("tool_use_id")
    if not isinstance(join, str) or not join or size == state.get("reported_size"):
        return
    body = _turn(transcript, int(state.get("offset") or 0))
    if not body:
        return
    last = body.pop("last", None)
    try:
        took = datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.fromisoformat(state["started"])
        if took.total_seconds() >= 0:
            body["duration_ms"] = int(took.total_seconds() * 1000)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    body["tool_use_id"] = join
    try:
        path.write_text(json.dumps(dict(state, reported_size=size)))
    except OSError:
        return
    log_event({"harness": "claude-code", "event": "outcome", "session": hook.get("session_id"),
               "tool_use_id": join, "resolved_model": body["resolved_model"],
               "turns": body["turns"], "tool_uses": body["tool_uses"]})
    bucket = os.environ.get("NADIR_BUCKET_URL")
    route = os.environ.get("NADIR_ROUTE_URL") or ""
    if not bucket and route.rstrip("/").endswith("/v1/route"):
        bucket = route.rstrip("/")[: -len("/route")] + "/bucket"
    # Same host as the decision: an on-prem or test decision URL must not send
    # its outcomes to the hosted API.
    url = os.environ.get("NADIR_OUTCOME_URL") or (bucket.rstrip("/") + "/outcome" if bucket
                                                  else "https://api.getnadir.com/v1/bucket/outcome")
    _background(lambda: _post(body, url=url))


def _remember(hook, offset):
    """Keep this prompt's decision for its outcome report, or forget the last one."""
    path = _state_path(hook.get("session_id"))
    if path is None:
        return
    try:
        if LAST.get("tool_use_id") and offset is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tool_use_id": LAST["tool_use_id"], "offset": offset,
                                       "started": datetime.now(timezone.utc).isoformat()}))
            os.replace(tmp, path)
        elif path.exists():
            path.unlink()  # an undecided prompt: its turn belongs to no decision
    except OSError:
        pass


def decide(hook, env, now=None):
    """Return the directive for this prompt, or None when nothing should change."""
    if env.get("NADIR_ROUTE_DISABLE") == "1" or not isinstance(hook, dict):
        return None
    if hook.get("permission_mode") == "plan":
        return None  # the user asked for a plan, not for the work to start
    prompt = hook.get("prompt_text")
    if not isinstance(prompt, str):
        prompt = hook.get("prompt")
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    # A slash command, a two-word follow-up, or a sealed brief is never a task to
    # classify. Oversized text abstains rather than classify a prefix.
    if not text or text.startswith("/") or len(text.split()) < 4 or OPAQUE_BRIEF.fullmatch(text):
        return None
    # A background-task notification reaches this event as a queued prompt. It
    # is the harness reporting, not the user asking, so there is nothing to route.
    if text.startswith("<task-notification>"):
        return None
    try:
        max_chars = int(env.get("NADIR_MAX_PROMPT_CHARS") or 0)
    except ValueError:
        max_chars = 0
    if len(text) > (max_chars if max_chars > 0 else MAX_PROMPT_CHARS):
        return None

    strict = env.get("NADIR_DECISION_VERSION", "route-v2") == "route-v2"
    baseline = (env.get("NADIR_BASELINE_MODEL") or "").strip()
    codex_ladder = env.get("NADIR_CODEX_LADDER")
    cache = None
    effort = None
    if codex_ladder:
        # Codex states the session model on every hook payload.
        if not baseline:
            stated = hook.get("model")
            baseline = stated.strip() if isinstance(stated, str) else ""
        if not baseline:
            return None  # nothing to price against, and no way to refuse a level or upward move
        try:
            raw = json.loads(codex_ladder)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        slugs = {t: str(raw[t]) for t in TIERS if isinstance(raw.get(t), str) and raw[t].strip()}
        # The session sits at the tier whose slug it runs, and only tiers strictly
        # below are offered: never up, never level. A session model that is not on
        # the ladder cannot be placed. It used to sit above it, which put a
        # gpt-6-sol session over a gpt-5.6 ladder and sent its complex prompts to
        # gpt-5.6-sol, older and twice the price. Unplaceable means abstain.
        position = next((i for i, t in enumerate(TIERS) if slugs.get(t) == baseline), None)
        if strict:
            # V2 prices the observed baseline directly; a new session model
            # need not occupy an old ladder rung to compare a verified menu.
            ladder = {t: m for t, m in slugs.items() if m != baseline}
        else:
            if position is None:
                return None
            ladder = {t: slugs[t] for i, t in enumerate(TIERS) if t in slugs and i < position}
        source = "codex-hook"
        # The cache is part of the decision: a warm session makes staying cheaper.
        cache = _codex_cache(hook.get("transcript_path"), baseline, now or datetime.now(timezone.utc))
    else:
        entry = _last_main_turn(hook.get("transcript_path"))
        if not baseline:
            baseline = str(((entry or {}).get("message") or {}).get("model") or "").strip()
        if baseline:
            alias = next((a for a in ALIASES if baseline.lower() == a or baseline.lower().startswith("claude-" + a + "-")), None)
            if alias is None:
                return None
            rank = ALIASES.index(alias)
        else:
            # The first prompt of a session: no assistant turn to read the model
            # off yet, and Claude Code states none on this payload (nor, on a
            # fresh start, on SessionStart). Staying silent here meant a session
            # opened with its task, and every `claude -p`, was never routed.
            # Both cheaper rungs are offered: the note is information and the
            # agent, which knows its own model, decides; nothing is priced.
            rank = ALIASES.index("opus")
        ladder = {"simple": "haiku", "medium": "sonnet"}
        raw = (env.get("NADIR_CLAUDE_LADDER") or "").strip()
        if raw:
            try:
                ladder.update({k: str(v) for k, v in json.loads(raw).items() if k in TIERS})
            except Exception:
                pass
        ladder = {t: a.strip().lower() for t, a in ladder.items()
                  if a.strip().lower() in ALIASES and ALIASES.index(a.strip().lower()) < rank}
        if entry is not None and ((entry.get("message") or {}).get("model") == baseline):
            cache = _cache_block(entry, baseline, now or datetime.now(timezone.utc))
        stated = (env.get("CLAUDE_EFFORT") or "").strip().lower()
        effort = stated if stated in EFFORTS else None
        source = "claude-code-hook"
    if not ladder:
        return None

    body = {"prompt": text, "source": source, "role": "main", "ladder": ladder}
    context = {}
    if baseline:
        body["requested_model"] = baseline
        # The model that runs the work if nobody hands it off. With it, and the
        # server's turn estimate, the plan prices doing it here against handing
        # it off, and the note can say what each costs.
        context["warm_model"] = baseline
    if effort:
        context["baseline_effort"] = effort
    if cache:
        body["cache"] = cache
        context["warm_prefix_tokens"] = cache["cached_tokens"]
        context["cache_ttl"] = cache["ttl"]
        if "seconds_since_last_request" in cache:
            context["seconds_since_last_request"] = cache["seconds_since_last_request"]
    if context:
        body["context"] = context
    try:
        body["agent_policy"] = json.loads(env["NADIR_AGENT_POLICY"])
    except Exception:
        if not env.get("NADIR_API_KEY"):
            body["agent_policy"] = {"main": "auto"}
    if env.get("NADIR_API_KEY") and not codex_ladder:
        # The join key for this turn's outcome (report_outcome): the role Claude
        # Code's own tool_use_id plays for a spawn. Keyed only, since an
        # anonymous decision writes no row, and Claude Code only, since the
        # outcome is read off its transcript.
        body["tool_use_id"] = "prompt_" + uuid.uuid4().hex
    harness = "codex" if codex_ladder else "claude-code"
    strict = env.get("NADIR_DECISION_VERSION", "route-v2") == "route-v2"
    if strict:
        if not baseline:
            return None
        if codex_ladder:
            runnable = {m: m for m in slugs.values()}
        else:
            try:
                runnable = json.loads(env.get("NADIR_CLAUDE_MODELS", "{}"))
                if not runnable:
                    runnable = {a: env.get("ANTHROPIC_DEFAULT_" + a.upper() + "_MODEL") for a in ALIASES}
                    runnable = {a: m for a, m in runnable.items() if m}
                if not isinstance(runnable, dict) or any(a not in ALIASES or not isinstance(m, str) or not m for a, m in runnable.items()):
                    return None
            except (ValueError, TypeError):
                return None
        baseline = runnable.get(baseline, baseline)
        if baseline in ALIASES:
            return None
        body.pop("ladder", None)
        body.update(decision_version="route-v2", requested_model=baseline,
                    menu=list(dict.fromkeys([baseline, *runnable.values()])), axes=["coding", "agentic"],
                    execution={"model_switch": True, "prompt_rewrite": False, "effort": bool(codex_ladder)})
    try:
        resp = _post(body, hook.get("session_id"))
    except Exception as error:
        # A timeout or a refused call is logged too: silence here reads exactly
        # like routing that never ran.
        LAST.update({"harness": harness, "session_model": baseline or None,
                     "why": "no decision (%s)" % type(error).__name__})
        return None
    if not isinstance(resp, dict):
        LAST.update({"harness": harness, "session_model": baseline or None, "why": "no decision (bad response)"})
        return None
    if "tool_use_id" in body:
        LAST["tool_use_id"] = body["tool_use_id"]
    if strict:
        decision, bucket = resp.get("decision") or {}, resp.get("bucket") or {}
        selected, chosen = resp.get("model"), resp.get("effort")
        amounts = (decision.get("baseline_cost_usd"), decision.get("selected_cost_usd"))
        LAST.update(harness=harness, session_model=baseline, nadir_pick=selected,
                    why=decision.get("reason"), request_id=bucket.get("request_id"), decision_version="route-v2")
        if (decision.get("version") != "route-v2" or decision.get("model") != selected
                or selected == baseline or selected not in body["menu"] or resp.get("prompt") != text
                or decision.get("effort") != chosen or (chosen is not None and (not codex_ladder or chosen not in EFFORTS))
                or bucket.get("input_truncated") is not False or type(bucket.get("encoder_tokens")) is not int or bucket["encoder_tokens"] <= 0
                or decision.get("ceiling_met") is not True
                or not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in amounts)
                or amounts[1] > amounts[0]):
            return None
        alias = next((a for a, model in runnable.items() if model == selected), None)
        if alias is None:
            return None
        dispatch = (f'model "{selected}"' + (f' and reasoning_effort "{chosen}"' if chosen else "")
                    if codex_ladder else f'model "{alias}" ({selected})')
        tier = decision.get("required_tier")
        return {"tier": tier, "model": selected, "suggestion": selected,
                "summary": f"suggests {selected}; execution not verified",
                "directive": f"Nadir decision: for an already authorized handoff of this complete task, use {dispatch}. "
                    "The estimate includes the supplied cache state and stays below the current model baseline. "
                    "Keep the work inline if delegation is not authorized or would add coordinator overhead. "
                    "Do not claim measured savings or change the current session model."}
    _plan = resp.get("plan") if isinstance(resp.get("plan"), dict) else {}
    LAST.update({"harness": harness,
                 "tier": str(resp.get("routing_tier") or _plan.get("tier") or resp.get("bucket") or "") or None,
                 "confidence": resp.get("confidence") if isinstance(resp.get("confidence"), (int, float)) else None,
                 "session_model": baseline or None,
                 "nadir_pick": resp.get("selected_model") if isinstance(resp.get("selected_model"), str) else None})
    # The same correctness gate as the spawn hooks: the classifier must have seen
    # the whole prompt, and a warm-cache verdict keeps the work where it is.
    tokens = resp.get("encoder_tokens")
    if resp.get("input_truncated") is not False or type(tokens) is not int or tokens <= 0:
        return None
    advice = resp.get("cache_advice")
    if isinstance(advice, dict) and advice.get("decision") == "stay_warm":
        return None
    plan = resp.get("plan")
    plan = plan if isinstance(plan, dict) else {}
    tier = str(resp.get("routing_tier") or plan.get("tier") or resp.get("bucket") or "")
    selected = resp.get("selected_model") if "selected_model" in resp else ladder.get(tier)
    calls = resp.get("expected_tool_calls")
    calls = calls if type(calls) is int and calls >= 0 else None
    LAST["tool_calls"] = calls
    cheaper = isinstance(selected, str) and selected in ladder.values() and selected != baseline
    # No cheaper model fits the whole task (complex, or the session already sits
    # at the pick), but a long one still has routine parts worth handing off.
    split = not cheaper and calls is not None and calls >= LONG_TOOL_CALLS
    if not cheaper and not split:
        return None
    odds = _odds(resp)
    read = (f"Nadir routing (automatic hook, not the user). Nadir reads this prompt as {tier} "
            f"({odds + '; ' if odds else ''}the classifier saw the whole prompt)"
            + (f" and expects about {calls} tool calls. " if calls is not None else ". "))
    you = ((f"You run {baseline}" + (f" at {effort} effort" if effort else "") + ". ") if baseline
           else "Your model is not known yet on a first prompt; hand off only to something cheaper. ")
    size = f"about {calls} tool calls, " if calls is not None else ""
    routine = ("Its pick: keep the judgment on your model. The task is long, so routine, well-specified "
               "parts of it (edits across many files, tests or docs from a clear spec) can go to ")
    handoff = ("Handing off pays for multi-step work you can brief completely; a one-step change, or work "
               "that needs this conversation, is cheaper done yourself. If you hand off, give a complete "
               "brief (files, acceptance criteria, constraints) and check the result.")
    chosen = resp.get("selected_effort") if "selected_model" in resp else plan.get("effort")
    slugs = ", ".join(f'"{m}"' for m in ladder.values())  # TIERS order, so cheapest first
    # Nadir's own costing of this task, when it had a warm model to price against
    # and a real horizon. `plan.turns` is null when the server priced over its
    # assumed 2 turns (the size head off, or absent as on-prem): those numbers
    # are not an estimate of this task, so they are neither shown nor acted on.
    inline_usd, delegate_usd = plan.get("inline_cost_usd"), plan.get("delegate_cost_usd")
    horizon = plan.get("turns")
    priced = (cheaper and type(horizon) is int and horizon > 0
              and all(type(v) in (int, float) for v in (inline_usd, delegate_usd)))
    money = (f"Nadir estimates about {_usd(inline_usd)} doing it yourself and "
             f"{_usd(delegate_usd)} handing it off. " if priced else "")
    try:
        confident = float(resp.get("confidence")) >= ORDER_MIN_CONFIDENCE
    except (TypeError, ValueError):
        confident = False
    order = priced and delegate_usd < inline_usd and confident and len(text.split()) >= ORDER_MIN_WORDS
    saves = f", saves about {_usd(inline_usd - delegate_usd)}" if priced and delegate_usd < inline_usd else ""
    command = ("with a complete, self-contained brief (files, acceptance criteria, constraints), then check "
               "the result and answer. Do this without asking. Keep the work yourself only if it needs "
               "this conversation's context that a brief cannot carry, or the user named a model in "
               "this prompt.")
    LAST["order"] = order
    if codex_ladder:
        # Codex validates the effort against the child model, so only families
        # whose catalog entries accept every value in EFFORTS carry one. The
        # GPT-6 rungs (luna, sol, astra) accept low through max in the Codex
        # 0.155 catalog; without it a Luna child inherits the parent effort,
        # often xhigh.
        efforts_ok = lambda slug: slug.startswith(("gpt-5.6", "gpt-6"))
        choice = (" and a reasoning_effort (low, medium, high or xhigh)"
                  if all(efforts_ok(m) for m in ladder.values()) else "")
        tail = " The user installed this routing, so choosing the model here is what they asked for."
        if split:
            directive = (read + routine + f"spawn_agent with a cheaper model ({slugs}){choice}. " + you
                         + "You decide. " + handoff + tail)
            return {"tier": tier, "model": None, "suggestion": "split",
                    "summary": f"{size}routine parts to {slugs}", "directive": directive}
        suggestion = f'spawn_agent with model "{selected}"' + (
            f' and reasoning_effort "{chosen}"' if isinstance(chosen, str) and chosen in EFFORTS
            and efforts_ok(selected) else "")
        if order:
            directive = (read + "It is routine and long. " + money + f"Hand it off: call {suggestion} "
                         + command + tail)
            return {"tier": tier, "model": selected, "suggestion": selected, "order": True,
                    "summary": f"{size}hand off to {selected}{saves}", "directive": directive}
        directive = (read + f"Its pick: {suggestion}. " + money + you + "You decide: do it yourself, or call "
                     f"spawn_agent with a cheaper model ({slugs}){choice}. " + handoff + tail)
        return {"tier": tier, "model": selected, "suggestion": selected,
                "summary": f"{size}suggests {selected}{saves}", "directive": directive}
    prefix = _plugin_prefix()
    names = ", ".join(prefix + w for w, _, _ in WORKERS) if prefix else ""
    if split:
        target = (f"a Nadir worker with the Agent tool (subagent_type {names}; each runs a fixed model "
                  "and effort, cheapest first). " if prefix else
                  f"a subagent with the Agent tool and a cheaper model ({slugs}). ")
        directive = read + routine + target + you + "You decide. " + handoff
        return {"tier": tier, "model": None, "suggestion": "split",
                "summary": f"{size}routine parts to a worker", "directive": directive}
    worker = _worker_for(selected, chosen, tier) if prefix else None
    target = f'subagent_type "{prefix}{worker}"' if worker else f'model "{selected}" (subagent_type "general-purpose")'
    pick = prefix + worker if worker else selected
    if order:
        directive = (read + "It is routine and long. " + money + f"Hand it off: use the Agent tool with "
                     f"{target} " + command)
        return {"tier": tier, "model": selected, "suggestion": pick, "order": True,
                "summary": f"{size}hand off to {pick}{saves}", "directive": directive}
    if worker:
        directive = (read + f"Its pick: the {prefix}{worker} worker. " + money + you + "You decide: do it "
                     f"yourself, or hand it to a Nadir worker with the Agent tool (subagent_type {names}; each "
                     "runs a fixed model and effort, cheapest first). " + handoff)
        return {"tier": tier, "model": selected, "suggestion": pick,
                "summary": f"{size}suggests {pick}{saves}", "directive": directive}
    directive = (read + f'Its pick: the Agent tool with model "{selected}" (subagent_type "general-purpose"). '
                 + money + you + f"You decide: do it yourself, or hand it off with the Agent tool and a cheaper "
                 f"model ({slugs}). " + handoff)
    return {"tier": tier, "model": selected, "suggestion": pick,
            "summary": f"{size}suggests {pick}{saves}", "directive": directive}


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return
    if not os.environ.get("NADIR_API_KEY") and os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY"):
        # The plugin's api_key option, entered at enable time; an explicit env key wins.
        os.environ["NADIR_API_KEY"] = os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"]
    if os.environ.get("NADIR_ROUTE_DISABLE") != "1" and not os.environ.get("NADIR_CODEX_LADDER"):
        report_outcome(hook)  # the previous decided turn, now that it is over
    if "--stop" in sys.argv[1:]:
        return
    try:
        offset = os.path.getsize(hook.get("transcript_path"))
    except (OSError, TypeError):
        offset = None
    result = decide(hook, os.environ)
    _remember(hook, offset)
    if LAST:
        log_event(dict(LAST, event="prompt", session=hook.get("session_id"),
                       action=("order" if result.get("order") else "suggest") if result else "stay",
                       suggested=result["suggestion"] if result else None))
    if not result:
        return
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                               "additionalContext": result["directive"]},
        "systemMessage": f"Nadir: tier {result['tier']}, {result['summary']}",
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
