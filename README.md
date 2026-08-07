# Agent hooks: the agent routes its own spawns

A `PreToolUse` hook on the subagent-spawn tool asks Nadir's free decision API
(`POST https://api.getnadir.com/v1/bucket`, keyless) which model the task needs,
then rewrites `model` in the spawn's input. The main thread is never touched,
by construction: the hook only fires on spawns.

```
claude-code/    Claude Code plugin (plugin.json + hooks/hooks.json + scripts/ + skills/)
codex/          ~/.codex hooks.json + route-spawn.sh
```

## Fail-open contract

Both scripts exit 0 with **no stdout** on every failure path: disabled, empty
prompt, unparseable stdin, bad policy JSON, curl network error, curl timeout
(`-m 2`), non-200 (`curl -f`), missing decision field, or a model equal to the
one already requested. Claude Code and Codex read "exit 0, no output" as *no
decision*, so the spawn proceeds exactly as the agent intended. Only exit code 2
blocks a tool call, and neither script can produce it.

This hook is a nudge, not a control. To *enforce* a model ceiling, back it with
a permission rule (`Agent(model:opus)` in `permissions.deny`).

## Env knobs (both agents)

| Var | Default | Meaning |
| --- | --- | --- |
| `NADIR_ROUTE_DISABLE` | unset | `1` turns the hook off entirely |
| `NADIR_BUCKET_URL` | `https://api.getnadir.com/v1/bucket` | decision endpoint |
| `NADIR_AGENT_POLICY` | `{"subagent":"auto"}` | Claude Code only: raw JSON role policy; `auto` = let the router pick, or pin a value (`{"subagent":"haiku"}`). The Codex script routes by tier via `NADIR_CODEX_LADDER` and ignores this. |

Both scripts need `python3` on PATH for JSON handling; without it every path exits silently and spawns proceed unrouted.
| `NADIR_CODEX_LADDER` | unset | **Codex only.** JSON tier→slug map; unset means pass through untouched |

## Claude Code

Tool name is `Agent` (`Task` still works as an alias). The hook receives
`tool_input` `{prompt, description, subagent_type, model}` and returns
`hookSpecificOutput.updatedInput`, which **replaces the whole input object** —
the script echoes every field it received and only adds `model`.

**Precedence caveat:** `CLAUDE_CODE_SUBAGENT_MODEL` outranks the per-invocation
`model` the hook writes. If it is set to anything other than `inherit`, this hook
does nothing. The hook does beat a subagent's frontmatter `model`, including
`model: inherit`.

### Install: plugin (one command, once the plugin repo is published)

```bash
claude plugin marketplace add https://getnadir.com/marketplace.json
claude plugin install nadir-route@nadir
```

When mirroring `claude-code/` into that repo, set the exec bit through git —
this repo has `core.fileMode` off, so a plain `chmod +x` is silently dropped and
the hook lands non-executable:

```bash
git update-index --chmod=+x scripts/route-spawn.sh
```

`app/public/marketplace.json` carries a loud `_comment`: a URL marketplace
downloads only `marketplace.json`, so the plugin `source` must be absolute. It
points at `NadirRouter/nadir-route-plugin`, which must exist and mirror
`claude-code/` before the marketplace is announced. Publishing it is a founder step.

### Install: bare settings.json (no plugin, works today)

```bash
mkdir -p ~/.claude/nadir
cp integrations/agent-hooks/claude-code/scripts/route-spawn.sh ~/.claude/nadir/
chmod +x ~/.claude/nadir/route-spawn.sh
```

then in `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent",
        "hooks": [{ "type": "command", "command": "$HOME/.claude/nadir/route-spawn.sh" }]
      }
    ]
  }
}
```

Hooks also fire inside subagents and under `claude -p`, so nested fan-out is
routed too (unless `--bare`, which skips hook discovery).

## Codex

Same shape: `PreToolUse` on `spawn_agent`, matcher alias `Agent`,
`hookSpecificOutput.updatedInput`. Two Codex-specific constraints:

1. **Model catalog.** The injected slug is validated against
   `models_manager.list_models()`; anything outside the catalog is rejected.
   There is no universal slug to guess, so the script routes through
   `NADIR_CODEX_LADDER` — a JSON tier→slug map *you* set to match your catalog
   (`model_catalog_json` is the lever for registering custom slugs). With it
   unset the hook is a no-op instead of a source of 400s.
2. **`deny_unknown_fields`.** `spawn_agent` args (`model`, `reasoning_effort`,
   `service_tier`, `agent_type`, `task_name`, `message`) reject unknown keys, so
   `updatedInput` echoes exactly the fields that arrived plus `model`. The script
   never invents a field.

```bash
mkdir -p ~/.codex/nadir
cp integrations/agent-hooks/codex/route-spawn.sh ~/.codex/nadir/
chmod +x ~/.codex/nadir/route-spawn.sh
cp integrations/agent-hooks/codex/hooks.json ~/.codex/hooks.json   # or merge
export NADIR_CODEX_LADDER='{"simple":"gpt-5.6-mini","medium":"gpt-5.6","complex":"gpt-5.6-terra"}'
```

**One-time trust prompt:** non-managed command hooks must be approved once via
`/hooks` in Codex. Trust is keyed to the script's hash, so editing
`route-spawn.sh` re-prompts.

The Codex script classifies on `message` (falling back to `task_name`), capped at
2000 chars, mirroring the Claude Code script's `prompt` → `description`. The task
text is the classification input; the short label is only a fallback, since a
2-6 word label buckets far cheaper than the work it names.

## Local test recipe

Pipe a fabricated hook stdin through the script and read stdout.

```bash
S=integrations/agent-hooks/claude-code/scripts/route-spawn.sh

# normal case -> {"hookSpecificOutput": {... "updatedInput": {... "model": ...}}}
echo '{"tool_name":"Agent","tool_input":{"prompt":"rename a variable in one file","description":"rename a variable","subagent_type":"Explore","model":"opus"}}' | sh $S

# disabled -> no output
echo '{"tool_input":{"description":"rename a variable"}}' | NADIR_ROUTE_DISABLE=1 sh $S

# unreachable endpoint -> no output, exit 0
echo '{"tool_input":{"description":"rename a variable"}}' | NADIR_BUCKET_URL=http://127.0.0.1:9/v1/bucket sh $S; echo "exit=$?"

# garbage stdin -> no output, exit 0
echo 'not json' | sh $S; echo "exit=$?"
```

Codex equivalent, with the ladder set:

```bash
echo '{"tool_name":"spawn_agent","tool_input":{"agent_type":"explorer","task_name":"rename a variable","message":"rename a variable in one file","model":"gpt-5.6"}}' \
  | NADIR_CODEX_LADDER='{"simple":"gpt-5.6-mini","medium":"gpt-5.6","complex":"gpt-5.6-terra"}' \
    sh integrations/agent-hooks/codex/route-spawn.sh
```

Verify afterwards in the agent: Claude Code's `PostToolUse`
`tool_response.resolvedModel` names the model the subagent actually started on.
