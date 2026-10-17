import { test, expect, mock } from 'claude-code/testing'
import type { On, TurnStepInput, TurnStepResult } from 'claude-code'

const NOW = Date.parse('2026-10-13T10:00:00Z')
const SESSION = 's1'
const HAIKU = 'claude-haiku-4-5'
const SESSION_MODEL = 'claude-opus-5-5'
const ON = { options: { switch_main_model: true } }
const row = (o: object) => JSON.stringify({
  ts: '2026-10-13T09:59:30Z', harness: 'claude-code', event: 'prompt', mode: 'recommend', session: SESSION, ...o })
let turns = 0
const step = (o: Partial<TurnStepInput> = {}): TurnStepInput =>
  ({ turnId: `t${++turns}`, index: 0, model: SESSION_MODEL, messageCount: 2, ...o })

// The world beneath the plugin: the route log, the session, the clock, the env,
// and the engine's model request at the bottom, which records the model it got.
function world(on: On, log: string | Error) {
  const sent: string[] = []
  mock.env(on, { HOME: '/tmp/u' })
  mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: SESSION }))
  on('fs.read', (_$, e) => {
    if (log instanceof Error) throw log
    if (e.path !== '/tmp/u/.nadir/route-log.jsonl') throw new Error(`unexpected read ${e.path}`)
    return { value: log }
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('turn.step', async function* (_$, e) {
    sent.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null } as TurnStepResult
  })
  return sent
}

const cases: [string, string | Error, string][] = [
  ['a cache-approved suggestion switches the model', row({ suggested: HAIKU }), HAIKU],
  ['no suggestion stays', row({ suggested: null, nadir_pick: HAIKU }), SESSION_MODEL],
  ['a strict-mode row is never a main-model switch', row({ mode: undefined, suggested: HAIKU }), SESSION_MODEL],
  ['a worker agent name is not a model', row({ suggested: 'nadir-route:haiku' }), SESSION_MODEL],
  ['another session\'s row is ignored', row({ session: 's2', suggested: HAIKU }), SESSION_MODEL],
  ['a stale row is ignored', row({ ts: '2026-10-13T09:50:00Z', suggested: HAIKU }), SESSION_MODEL],
  ['the latest row of this session wins', [row({ suggested: HAIKU }), row({ suggested: null })].join('\n'), SESSION_MODEL],
  ['a garbage line is skipped', ['{not json', row({ suggested: HAIKU })].join('\n'), HAIKU],
  ['an unreadable log leaves the session model', new Error('ENOENT'), SESSION_MODEL],
]
for (const [name, log, want] of cases) {
  test(name, ON, async ($, on) => {
    const sent = world(on, log)
    for await (const _ of $.turn.step(step())) { /* drain */ }
    expect(sent).toEqual([want])
  })
}

test('on by default: the manifest default switches', async ($, on) => {
  const sent = world(on, row({ suggested: HAIKU }))
  for await (const _ of $.turn.step(step())) {}
  expect(sent).toEqual([HAIKU])
})

test('turned off, it never switches', { options: { switch_main_model: false } }, async ($, on) => {
  const sent = world(on, row({ suggested: HAIKU }))
  for await (const _ of $.turn.step(step())) {}
  expect(sent).toEqual([SESSION_MODEL])
})

test('every step of a turn runs on the one decided model', ON, async ($, on) => {
  const sent = world(on, row({ suggested: HAIKU }))
  for await (const _ of $.turn.step(step({ turnId: 'same', index: 0 }))) {}
  for await (const _ of $.turn.step(step({ turnId: 'same', index: 1, messageCount: 4 }))) {}
  expect(sent).toEqual([HAIKU, HAIKU])
})

test('a subagent step keeps its own model', ON, async ($, on) => {
  const sent = world(on, row({ suggested: HAIKU }))
  for await (const _ of $.turn.step(step({ agentId: 'a1', model: 'claude-sonnet-5' }))) {}
  expect(sent).toEqual(['claude-sonnet-5'])
})
