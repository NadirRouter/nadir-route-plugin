# nadir-route

Right-size Claude Code's subagent spawns. A `PreToolUse` hook on the `Agent`
tool asks [Nadir](https://getnadir.com)'s free decision API which model the task
actually needs, then rewrites `model` in the spawn's input.

Since 0.8.0 it also right-sizes the prompt itself: a `UserPromptSubmit` hook
asks the same decision API which tier the user's prompt needs, priced against
the model the session runs, and when a cheaper tier suffices it asks Claude to
delegate that work to a subagent on it. Routing no longer waits for anyone to
ask for subagents; complex prompts change nothing, and the main thread keeps
its model and its warm cache.

0.8.1 routes spawns issued by a session's first turn, which 0.8.0 mostly left
on the session model. The hook reads that model off the session transcript, and
Claude Code writes the turn there a few milliseconds after the hook starts, so
an inherited spawn now waits up to a second for it.

0.8.2 routes a session's first prompt too. Claude Code states no model on that
prompt and the transcript has no turn to read one off yet, so 0.8.1 stayed
silent there, which left every session that opens with its task, and every
`claude -p`, unrouted. With no readable model it now offers only simple work to
Haiku, the cheapest alias, which cannot be a move up from any session model;
medium waits for the next prompt, once the model is on record.

0.8.3 routes Claude Code's Explore searches. Explore is the read-only search
agent; since Claude Code 2.1.280 it inherits the session model (capped at Opus),
and nearly every real search brief rates complex, so every search ran on the
frontier model. An Explore spawn that names no model now takes the rung below on
complex (Sonnet under an Opus session, never level or up).
`NADIR_EXPLORE_COMPLEX=inherit` turns that off.

0.8.4 extends that to Explore briefs the classifier cannot see whole (65% of
real ones were over the prefilter): they take the same rung without a decision,
since no tier under that ladder sits above it. It also stops the prompt router
classifying background-task notifications, which reach it as queued prompts.

0.8.5 writes a local route log, `~/.nadir/route-log.jsonl`: what Nadir decided
for each prompt and spawn, and which model the main thread and each subagent
then ran on. Metadata only, never prompt text; `NADIR_ROUTE_LOG=off` stops it.
Follow it live:

```bash
python3 ~/.claude/plugins/cache/nadir/nadir-route/*/scripts/route_log.py -f
```

0.8.8 never moves a spawn above the model its agent definition sets: a Haiku
agent that names no model stays on Haiku when Nadir says medium.

0.9.0 lets Claude decide. The prompt hook no longer gives an order. It gives
Nadir's read (the tier odds), its pick and the options, and Claude chooses: do
the work itself, or hand it to one of five **worker agents** this plugin ships,
each a fixed model and effort:

| Worker | Model | Effort |
|---|---|---|
| `nadir-route:haiku` | Haiku | none (Haiku takes no effort setting) |
| `nadir-route:sonnet-low` | Sonnet | low |
| `nadir-route:sonnet-medium` | Sonnet | medium |
| `nadir-route:sonnet-high` | Sonnet | high |
| `nadir-route:opus-medium` | Opus | medium |

0.9.1 adds size. Nadir's turn-size head estimates how many tool calls a prompt
will take, and the note says so. When no cheaper model fits the whole task but
the task is long (8+ tool calls), the note still speaks: keep the judgment on
your model and hand routine, well-specified parts to a worker. A session's first
prompt now offers Sonnet workers too, not only Haiku.

0.9.2 prices it. When Nadir knows your session model, it estimates what the task
costs done in place and handed off, and the note says both. In clear cases
(a cheaper model fits the whole task, a confident rating, a long task that is
cheaper to hand off, and a prompt that reads as a whole task) the note asks
Claude to hand the work off, unless it needs this conversation's context.

0.9.3: confidence decides who decides. When Nadir is confident and its costing
says handing off is cheaper, the note is an order; below that, Claude decides
from the information. The costing counts your warm prompt cache, so a warm
session that makes staying cheaper keeps the work where it is.

Workers are how a subagent's effort gets set at all: Claude Code's Agent tool
has no effort field, and it tells Claude to set `model` only when you ask for
one. A worker Claude picks is left exactly as chosen; the spawn hook still logs
Nadir's tier beside it, so the dashboard shows both.

Nadir is a **decision engine here, not a gateway**. Your prompts and completions
go straight from Claude Code to Anthropic on your own auth; Nadir is consulted
out of band with the spawn's task text and never sees the request, the response,
or a provider key. There is no added latency on the token stream.

```bash
claude plugin marketplace add https://getnadir.com/marketplace.json
claude plugin install nadir-route@nadir
```

No key required. Add one to attribute decisions to your account and get a
savings figure on the dashboard (see below).

## What it does

| Nadir's bucket | the hook writes | effect |
| --- | --- | --- |
| `simple` | `haiku` | your cheapest model |
| `medium` | `sonnet` | your default working model |
| `complex` | **nothing** | the spawn keeps the model the session already runs |

Three rules, all deliberate:

- **`complex` is never rewritten.** The top tier stays on the model you chose,
  including which Opus generation. Nadir right-sizes the cheap end; it does not
  move you off your frontier model.
- **It only writes harness aliases.** `Agent`'s `model` parameter is an enum
  (`haiku`, `sonnet`, `opus`, `fable`), and Claude Code converts a schema-invalid
  hook rewrite into a *denied* tool call. Aliases also resolve through your own
  `ANTHROPIC_DEFAULT_*_MODEL` config, so a routed spawn lands in your family and
  your generation rather than one this plugin hardcoded.
- **It never moves a spawn up on its own.** If the agent already asked for
  something cheaper than the routed tier, that stands. So does a cheaper model
  set in the agent's own definition (a Haiku agent such as
  `caveman:cavecrew-investigator` or the built-in `claude-code-guide`), which is
  what a spawn naming no model runs on. The one exception is an
  explicit policy pin from your account, which is a standing instruction from
  you and so wins in either direction. A pin naming a full model id
  (`claude-sonnet-5`) cannot be expressed on the alias enum at all, so it falls
  through to the tier ladder rather than doing nothing.

Your main thread is never touched, by construction: the hook fires only on
spawns.

Measured end to end on Claude Code 2.1.220 — a trivial read bucketed `medium`
and the subagent started on `claude-sonnet-5` (from an Opus inherit); a
multi-region failover design bucketed `complex`, the hook emitted nothing, and
the subagent ran on `claude-opus-5`.

## Configuration

Set these in `~/.claude/settings.json` under `env`, or export them.

| Var | Default | Meaning |
| --- | --- | --- |
| `NADIR_ROUTE_DISABLE` | unset | `1` turns the hook off |
| `NADIR_BASELINE_MODEL` | unset | the model your sessions run on, e.g. `claude-opus-5`, which inherited spawns are routed and priced against. Unset, the hook reads it off the session transcript; set it to pin one, or for sessions that persist no transcript, where an inherited spawn waits up to a second for one and then keeps its model |
| `NADIR_API_KEY` | unset | attributes decisions to your account and surfaces them on the dashboard. Keyless calls store no row at all, by design, so your dashboard stays empty |
| `NADIR_CLAUDE_LADDER` | `{"simple":"haiku","medium":"sonnet"}` | retune the table above. Map a tier to `inherit` to leave it alone; `{"simple":"inherit","medium":"inherit"}` is audit mode — decisions recorded, nothing changed |
| `NADIR_AGENT_POLICY` | `{"subagent":"auto"}` keyless | raw JSON role policy. Pin a value (`{"subagent":"haiku"}`) instead of letting the router pick |
| `NADIR_TIMEOUT` | `5` | seconds the decision call may take. Do not lower to 2: a 10-way parallel fan-out measures 1.9–2.3s per call, so a 2s cap loses most decisions in exactly the traffic this is for |
| `NADIR_BUCKET_URL` | `https://api.getnadir.com/v1/bucket` | endpoint |

Requires `python3` on PATH for JSON handling. Without it every path exits
silently and spawns proceed unrouted.

## Fail-open, and how to tell

Every failure path exits 0 with **no stdout** — disabled, empty prompt,
unparseable input, bad policy JSON, network error, timeout, non-200, missing
decision field, a model equal to the one already requested, or a rejected key.
Claude Code reads that as "no decision" and the spawn proceeds exactly as the
agent intended. Only exit code 2 blocks a tool call, and this script cannot
produce one.

The cost of that design is that "working" and "doing nothing" look identical, so
check explicitly rather than assuming:

```bash
hook=$(python3 -c 'import json, os; d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"); print(json.load(open(d + "/plugins/installed_plugins.json"))["plugins"]["nadir-route@nadir"][0]["installPath"])')
echo '{"tool_name":"Agent","tool_input":{"prompt":"rename a variable in one file","description":"rename var","subagent_type":"Explore"}}' \
  | NADIR_BASELINE_MODEL=claude-opus-5 sh "$hook/scripts/route-spawn.sh"
```

Expect JSON containing `"model":"haiku"`. The first line finds the version
Claude Code actually runs: an update leaves the previous version's directory in
the plugin cache, so a glob over it can pick the old hook. The baseline stands
in for the session transcript a real spawn is read against: without either, an
inherited spawn keeps its model by design and this prints nothing. With it,
empty output means it is not routing.
An invalid `NADIR_API_KEY` produces exactly the same silence as an unreachable
network, so if you are keyed, re-run the same check with `NADIR_API_KEY=` — if it
starts working, your key is being rejected.

Inside a session, Claude Code's `PostToolUse` `tool_response.resolvedModel`
names the model a subagent actually started on.

## Precedence traps

- `CLAUDE_CODE_SUBAGENT_MODEL`, if set to anything but `inherit`, outranks the
  per-invocation model this hook writes. The hook now detects this and exits
  before calling the API, so it spends no decision — and books no savings — on a
  rewrite that is guaranteed to be discarded.
- The hook does beat a subagent's frontmatter `model`, including `model: inherit`.
- This is a nudge, not a control. Claude Code's `Agent` permission rules match
  the agent *type*, not the model, so there is no local way to enforce a ceiling.

## Alternative install

One command, no plugin, same hook — it merges into `settings.json`, backs it up,
and verifies routing is live before it exits:

```bash
curl -fsSL https://getnadir.com/install/claude-code.sh | sh
```

For tiering work that is not a subagent spawn (batch items, "which model should
this use", delegate-vs-inline cost), the full skill documents the whole
`/v1/bucket` contract: `npx skills add https://getnadir.com`

## Source

Mirrored from `integrations/agent-hooks/claude-code` in the Nadir monorepo,
which is the source of truth. Issues: <https://getnadir.com>
