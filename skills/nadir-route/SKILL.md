---
name: nadir-route
description: Explains Nadir's automatic cache-aware routing and recoverable context compaction. Use when asked why a model was selected, how costs are estimated, or how to configure or disable the installed hooks.
---

# Nadir routing and compaction

The installed hooks use Decision API v2. They consider the complete task,
current model, runnable model menu, measured configuration evidence, and cache
costs together. They apply only a supported recommendation whose estimated cost
clears the baseline savings threshold. The classifier tier alone never selects
a model.

`UserPromptSubmit` adds advice for an already authorized handoff. It does not
rewrite the user's prompt or change the session model. Delegation still requires
authorization and must make sense after coordinator overhead. `PreToolUse` can
update a native Agent spawn's model alias and prompt. Explicit model choices and
fixed worker definitions stay pinned. The main session model is unchanged.

The native model field accepts aliases (`haiku`, `sonnet`, `opus`, `fable`), not
provider IDs. The hook maps verified exact IDs to these aliases. Do not replace
an alias with a full provider ID: that fails the Agent input schema.

The session baseline comes from the transcript automatically. Input counts can
also be read there, but a child does not claim its parent's cache hit. When
reuse is unknown, the API compares a fully cached baseline with fully written
alternatives. Unknown input size, incomplete classifier coverage, or missing
capability evidence can retain the current model. Retention is a valid result.

The installer also enables local recoverable tool-output compaction. Large reads
and successful test output can be represented by outlines/summaries with exact
archives and recovery pointers. Errors, patches, and targeted reads retain their
protections. Renderer settings are pinned for the session to preserve stable
context. Follow a recovery pointer whenever the summary lacks information needed
for the task; never infer omitted code.

Configuration:

- `NADIR_ROUTE_URL`: private decision endpoint; default is the hosted `/v1/route`.
- `NADIR_CLAUDE_MODELS`: alias-to-exact-ID JSON map, or use the native
  `ANTHROPIC_DEFAULT_*_MODEL` variables. These must describe what the harness runs.
- `NADIR_API_KEY`: account attribution and organization policy. Anonymous
  decisions do not create hosted usage records.
- `NADIR_ROUTE_DISABLE=1`: disable routing. `NADIR_CONTEXT_DISABLE=1`: disable
  context compaction. Neither setting changes the original files or archives.
- `NADIR_BASELINE_MODEL`: explicit baseline override; normally omit it so the
  transcript remains authoritative.
- `NADIR_DECISION_VERSION=route-v1`: explicit legacy compatibility mode.

A malformed/costlier response or network failure keeps the original dispatch.
A forbidden baseline (`403 model_not_allowed` or `409 baseline_policy_conflict`)
blocks dispatch; do not restore it as a fallback.

Use the local route log and attributed decision/outcome records to distinguish a
recommendation from an applied model change. Dollar figures are estimates until
actual provider usage and task acceptance are recorded. Token reduction does not
prove extra subscription allowance or lower a fixed subscription invoice.

For complete file-based briefs and Superpowers workflows, install the full skill
with `npx skills add https://getnadir.com`, then follow its
`references/superpowers.md`. Disable duplicate automatic routing in that host
session with `NADIR_ROUTE_DISABLE=1` and execute the adapter's returned
`tool_input`. Preserve file ownership, acceptance criteria, independent reviews,
and escalation stages.
