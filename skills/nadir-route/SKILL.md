---
name: nadir-route
description: "Explains that this session's prompts and subagent spawns are already right-sized by the Nadir hooks (a UserPromptSubmit hook that asks you to delegate cheap work, a PreToolUse hook that rewrites spawn models), and points at the full nadir-route skill for classifying a task by hand. Invoke when the user asks why work was delegated or why a subagent ran on a different model, how to disable or tune the routing, or wants a model-tier decision by hand."
---

# Nadir route (hook edition)

This plugin routes at two points. A `UserPromptSubmit` hook asks Nadir's
decision API which tier each prompt needs, priced against the model this session
runs, and when a cheaper tier suffices it gives you Nadir's read (the tier
odds), its pick and the cheaper options, as hook context on the prompt (a
`systemMessage` shows the user the same pick). You decide: do the work yourself,
or hand it off. Installed as a plugin, the options are Nadir workers, each a
fixed model and effort (`nadir-route:haiku`, `nadir-route:sonnet-low`,
`nadir-route:sonnet-medium`, `nadir-route:sonnet-high`,
`nadir-route:opus-medium`); otherwise, the Agent tool's `model`. Handing off pays
for multi-step work you can brief completely; a one-step change, or work that
needs this conversation, is cheaper done yourself. The note also gives Nadir's
size estimate (expected tool calls); on a long task with no cheaper model for the
whole of it, it suggests keeping the judgment yourself and handing the routine,
well-specified parts to a worker. When Nadir could price the task, the note
gives its estimate for doing it yourself and for handing it off, with your warm
prompt cache counted in. When Nadir is confident and handing off is cheaper, the
note asks you to hand the work off; keep it only if it needs this conversation's
context. Below that confidence, it is information and you decide. A `PreToolUse` hook on the
`Agent` tool asks the same question about every other spawn and rewrites `model`
in its input; a worker you picked is left as you chose. Your main thread stays on
the model the user chose, and a handoff happens in a fresh subagent.

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

- Network, timeout, ordinary non-200, and bad JSON responses leave the spawn
  untouched. A `model_not_allowed` 403 blocks it, and an unrepresentable
  required governance replacement also blocks it.
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
it for a file you captured yourself. A Stop hook also watches the prompt cache: when a turn re-writes the cached
prefix it tells the user how many tokens were re-cached and why (an idle gap
past the TTL, a model switch, or a mid-session effort, thinking or tool change),
and on an idle-gap miss over a large context it suggests `/compact`, because
nothing in Claude Code lets a hook start compaction. It reads only usage counts,
never message text, and `NADIR_CONTEXT_DISABLE=1` silences it too. With this hook active, skip only the full
skill's classification call, not its coding guidance.

Reference: <https://getnadir.com/.well-known/agent-skills/nadir-route/SKILL.md>
