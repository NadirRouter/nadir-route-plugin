---
name: nadir-route
description: "Explains that this session's subagent spawns are already right-sized by the Nadir PreToolUse hook, and points at the full nadir-route skill for classifying a task by hand. Invoke when the user asks why a subagent ran on a different model, how to disable or tune the routing, or wants a model-tier decision for work that is not a spawn."
---

# Nadir route (hook edition)

This plugin routes **spawns only**. Its `PreToolUse` hook on the `Agent` tool asks
Nadir's decision API which model the task needs and rewrites `model` in the
spawn's input. Your main thread stays on the model you chose.

- Any failure (network, timeout, non-200, bad JSON) produces no output and the
  spawn proceeds untouched.
- `NADIR_ROUTE_DISABLE=1` turns the hook off. `NADIR_AGENT_POLICY` (raw JSON,
  default `{"subagent":"auto"}`) pins a role instead of letting the router pick.
- `CLAUDE_CODE_SUBAGENT_MODEL` outranks the hook: if it is set to anything other
  than `inherit`, the hook's model is ignored.

**Do not classify prompts by hand here.** For tiering work that is not a subagent
spawn (batch items, "which model should this use", delegate-vs-inline cost), install
the full skill, which documents the whole `/v1/bucket` contract:

```
npx skills add https://getnadir.com
```

Reference: <https://getnadir.com/.well-known/agent-skills/nadir-route/SKILL.md>
