#!/usr/bin/env python3
"""Nadir UserPromptSubmit hook: right-size the user's prompt, not only the spawns.

The spawn hooks only ever fire when the agent decides to delegate, which on a
normal session is rarely, so an installed Nadir routed almost nothing unless
the user asked for subagents by name. This hook closes that gap. On every
prompt it asks /v1/bucket which tier the prompt needs, priced against the model
the session is running, and when a cheaper tier suffices it tells the agent to
delegate the work to a subagent on that tier. The main thread keeps its model
and its warm prompt cache; the switch happens where it is free, in a fresh
subagent, and the spawn hook then sees that spawn like any other.

Two harnesses, one file. With NADIR_CODEX_LADDER set it speaks Codex: the
session model is stated on the hook payload, the ladder maps tiers to catalog
slugs, and the directive names spawn_agent with the slug and, on gpt-5.6
slugs, the reasoning effort the plan chose. Otherwise it speaks Claude Code:
the session model is read off the transcript (usage accounting only, never
message text), and the directive names the Agent tool with the alias enum.

It never touches the prompt, never blocks it, and never speaks unless a
change is warranted. Every failure path is exit 0 with no output: no baseline,
an oversized prompt, a slash command, a network error, a truncated encoder
view, a warm-cache verdict, a policy pin already in force.

Env: NADIR_ROUTE_DISABLE=1, NADIR_API_KEY, NADIR_BUCKET_URL, NADIR_TIMEOUT,
NADIR_MAX_PROMPT_CHARS, NADIR_BASELINE_MODEL (explicit session model; wins),
NADIR_CLAUDE_LADDER / NADIR_CODEX_LADDER, NADIR_AGENT_POLICY, CLAUDE_EFFORT.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

ALIASES = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TIERS = ("simple", "medium", "complex")
MAX_PROMPT_CHARS = 1362
OPAQUE_BRIEF = re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}={0,2}")


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

    Same reading the skill's session_probe takes: cache_read_input_tokens is
    the warm prefix, read + creation + input is the current input, the
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
    made = count(usage.get("cache_creation_input_tokens")) or 0
    fresh = count(usage.get("input_tokens")) or 0
    block = {"model": model, "cached_tokens": read, "ttl": "5m",
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


def _post(body):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("NADIR_API_KEY")
    if key:
        headers["X-API-Key"] = key
    try:
        timeout = float(os.environ.get("NADIR_TIMEOUT") or 5)
    except ValueError:
        timeout = 5.0
    req = urllib.request.Request(os.environ.get("NADIR_BUCKET_URL") or "https://api.getnadir.com/v1/bucket",
                                 data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            return None
        return json.loads(resp.read())


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
    try:
        max_chars = int(env.get("NADIR_MAX_PROMPT_CHARS") or 0)
    except ValueError:
        max_chars = 0
    if len(text) > (max_chars if max_chars > 0 else MAX_PROMPT_CHARS):
        return None

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
        # The session sits at the tier whose slug it runs; unknown slugs sit above
        # the ladder. Only tiers strictly below are offered: never up, never level.
        position = next((i for i, t in enumerate(TIERS) if slugs.get(t) == baseline), len(TIERS))
        ladder = {t: slugs[t] for i, t in enumerate(TIERS) if t in slugs and i < position}
        source = "codex-hook"
    else:
        entry = None
        if not baseline:
            entry = _last_main_turn(hook.get("transcript_path"))
            baseline = str(((entry or {}).get("message") or {}).get("model") or "").strip()
        if not baseline:
            return None
        alias = next((a for a in ALIASES if baseline.lower() == a or baseline.lower().startswith("claude-" + a + "-")), None)
        if alias is None:
            return None
        rank = ALIASES.index(alias)
        ladder = {"simple": "haiku", "medium": "sonnet"}
        raw = (env.get("NADIR_CLAUDE_LADDER") or "").strip()
        if raw:
            try:
                ladder.update({k: str(v) for k, v in json.loads(raw).items() if k in TIERS})
            except Exception:
                pass
        ladder = {t: a.strip().lower() for t, a in ladder.items()
                  if a.strip().lower() in ALIASES and ALIASES.index(a.strip().lower()) < rank}
        if entry is not None:
            cache = _cache_block(entry, baseline, now or datetime.now(timezone.utc))
        stated = (env.get("CLAUDE_EFFORT") or "").strip().lower()
        effort = stated if stated in EFFORTS else None
        source = "claude-code-hook"
    if not ladder:
        return None

    body = {"prompt": text, "source": source, "role": "main",
            "requested_model": baseline, "ladder": ladder}
    if effort:
        body["context"] = {"baseline_effort": effort}
    if cache:
        body["cache"] = cache
    try:
        body["agent_policy"] = json.loads(env["NADIR_AGENT_POLICY"])
    except Exception:
        if not env.get("NADIR_API_KEY"):
            body["agent_policy"] = {"main": "auto"}
    try:
        resp = _post(body)
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
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
    if not isinstance(selected, str) or selected not in ladder.values() or selected == baseline:
        return None
    try:
        conf = float(resp.get("confidence"))
        conf_text = f"confidence {conf:.2f}; "
    except (TypeError, ValueError):
        conf_text = ""
    head = (f"Nadir routing (automatic hook, not the user): this prompt classifies as tier {tier} "
            f"({conf_text}the classifier saw the whole prompt). Delegate the work: ")
    tail = (" with a complete, self-contained brief (files, acceptance criteria, constraints), "
            "then check the result and answer. Do this without asking. Keep the work inline only "
            "if it needs conversation context a brief cannot carry, or the user pinned a model in "
            "this prompt.")
    if codex_ladder:
        chosen = resp.get("selected_effort") if "selected_model" in resp else plan.get("effort")
        effort_text = (f' and reasoning_effort "{chosen}"'
                       if isinstance(chosen, str) and chosen in EFFORTS and selected.startswith("gpt-5.6") else "")
        directive = (head + f'call spawn_agent with model "{selected}"{effort_text}' + tail +
                     " The user installed this routing, so naming the model here is what they asked for.")
    else:
        directive = head + f'use the Agent tool with model "{selected}" (subagent_type "general-purpose")' + tail
    return {"tier": tier, "model": selected, "directive": directive}


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return
    result = decide(hook, os.environ)
    if not result:
        return
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                               "additionalContext": result["directive"]},
        "systemMessage": f"Nadir: tier {result['tier']}, delegating to {result['model']}",
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
