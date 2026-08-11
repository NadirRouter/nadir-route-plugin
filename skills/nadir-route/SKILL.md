---
name: nadir-route
description: "Explains that this session's subagent spawns are already right-sized by the Nadir PreToolUse hook, and points at the full nadir-route skill for classifying a task by hand. Invoke when the user asks why a subagent ran on a different model, how to disable or tune the routing, or wants a model-tier decision for work that is not a spawn."
---

# Nadir route (hook edition)

This plugin routes **spawns only**. Its `PreToolUse` hook on the `Agent` tool asks
Nadir's decision API which model the task needs and rewrites `model` in the
spawn's input. Your main thread stays on the model you chose.

## What it actually does, so you can answer "why did that run on Haiku"

Nadir buckets the spawn's prompt as `simple`, `medium`, or `complex`, and the hook
maps that onto the `Agent` tool's model aliases:

| bucket | hook writes | effect |
|---|---|---|
| `simple` | `haiku` | your cheapest model, whichever version your config names |
| `medium` | `sonnet` | your default working model |
| `complex` | **nothing** | the spawn keeps the model this session already runs |

`complex` is deliberately left alone: the top tier stays on the model the user
chose, including which Opus generation. The hook also never moves a spawn *up* —
if the agent already asked for something cheaper than the routed tier, that
stands. If you are asked what a specific spawn cost or saved, the per-decision
log is on the dashboard at **/dashboard/agents → Engine decisions**, which shows
the bucket, the confidence, the model swap and which rules fired.

- Any failure (network, timeout, non-200, bad JSON) produces no output and the
  spawn proceeds untouched. It is a nudge, not a gate.
- `NADIR_ROUTE_DISABLE=1` turns the hook off. `NADIR_CLAUDE_LADDER` retunes the
  table above (`{"simple":"inherit","medium":"inherit"}` = record decisions,
  change nothing). `NADIR_AGENT_POLICY` (raw JSON, default `{"subagent":"auto"}`)
  pins a role instead of letting the router pick.
- `NADIR_BASELINE_MODEL` tells Nadir which model the session runs, which is what
  the savings figure is computed against. Without it decisions log unpriced.
- `CLAUDE_CODE_SUBAGENT_MODEL` outranks the hook: if it is set to anything other
  than `inherit`, the hook's model is ignored.

Do not try to write a full model id into a spawn's `model`. That parameter is an
enum (`haiku`, `sonnet`, `opus`, `fable`); anything else fails validation and
Claude Code turns a failed hook rewrite into a denied tool call.

**Do not classify prompts by hand here.** For tiering work that is not a subagent
spawn (batch items, "which model should this use", delegate-vs-inline cost), install
the full skill, which documents the whole `/v1/bucket` contract:

```
npx skills add https://getnadir.com
```

Reference: <https://getnadir.com/.well-known/agent-skills/nadir-route/SKILL.md>
