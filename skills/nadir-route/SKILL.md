---
name: nadir-route
description: "Explains that this session's subagent spawns are already right-sized by the Nadir PreToolUse hook, and points at the full nadir-route skill for classifying a task by hand. Invoke when the user asks why a subagent ran on a different model, how to disable or tune the routing, or wants a model-tier decision for work that is not a spawn."
---

# Nadir route (hook edition)

This plugin routes **spawns only**. Its `PreToolUse` hook on the `Agent` tool asks
Nadir's decision API which model the task needs and rewrites `model` in the
spawn's input. Your main thread stays on the model you chose.

For Superpowers sessions, use the full skill's `references/superpowers.md`
active companion and set `NADIR_ROUTE_DISABLE=1` in the host session. Execute
the companion's returned `tool_input` for every dispatch. This hook
cannot read referenced task files. Do not let it reclassify a file-path wrapper,
cancel an independent review or downgrade a protected fix/escalation stage.

## What it actually does, so you can answer "why did that run on Haiku"

Nadir buckets the spawn's prompt and returns `selected_model`. The hook uses
that final choice when it is an accepted alias; null or an unrepresentable ID
leaves the spawn untouched. The default ladder is:

| bucket | hook writes | effect |
|---|---|---|
| `simple` | `haiku` | your cheapest model, whichever version your config names |
| `medium` | `sonnet` | your default working model |
| `complex` | **nothing** | the spawn keeps the model this session already runs |

`complex` is deliberately left alone: the top tier stays on the model the user
chose, including which Opus generation. The hook also never moves a spawn *up* —
if the agent already asked for something cheaper than the routed tier, that
stands. The one exception is an explicit policy pin, which is a standing
instruction from you and so wins in either direction; a pin naming a full model
id cannot be expressed on the alias enum, so a modern response leaves the spawn
unchanged. Tier fallback applies only to older responses without `selected_model`.
If you are asked what a specific spawn cost or saved, the per-decision
log is on the dashboard at **/dashboard/agents → Engine decisions**, which shows
the bucket, the confidence, the model swap and which rules fired.

- Any failure (network, timeout, non-200, bad JSON) produces no output and the
  spawn proceeds untouched. It is a nudge, not a gate.
- `NADIR_ROUTE_DISABLE=1` turns the hook off. `NADIR_CLAUDE_LADDER` retunes the
  table above (`{"simple":"inherit","medium":"inherit"}` = record decisions,
  change nothing). `NADIR_AGENT_POLICY` (raw JSON)
  pins a role instead of letting the router pick. Without it a keyless install
  defaults to `{"subagent":"auto"}`, while a keyed one uses the policy saved on
  your account.
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

For coding work, keep handoffs concise and include the objective, file ownership,
constraints, exact failures and acceptance check. Reuse existing code before
adding it, at every tier. Inspect the diff and run the relevant check before
calling a cheap-model task complete; restore missing context before escalating.
Compaction is already running: this plugin registers the same hooks on
SessionStart, SubagentStart, Read and Bash, so bulky tool output and large file
reads are shrunk to a recoverable view with an exact archive and SHA-256 before
they reach the context window. It makes no model call, no API call, and cannot
rewrite the host's existing conversation, and it installs with no behaviour
policy so it does not compete with one you already run. Do not invoke
`scripts/compact_context.py` by hand for output the hooks already handled; use
it for a file you captured yourself. With this hook active, skip only the full
skill's classification call, not its coding guidance.

Reference: <https://getnadir.com/.well-known/agent-skills/nadir-route/SKILL.md>
