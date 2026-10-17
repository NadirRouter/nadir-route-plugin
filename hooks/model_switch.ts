import type { Register } from 'claude-code'

// The plugin's `switch_main_model` option, on by default. On, route_user_prompt.py
// runs in recommend mode and logs a `suggested` model only when Nadir's cache check
// prices switching below staying on the warm model; this module then runs that
// turn's main-thread requests on it. Subagents, other sessions, stale rows and
// strict-mode rows (a worker agent's name, not a model) are left alone, and an
// unreadable log leaves the session model. Transcript, tools and messages never change.
const FRESH_MS = 120_000
const SCAN_ROWS = 200
const MODEL_ID = /^claude-[a-z0-9.-]+$/

type Row = {
  ts?: string; harness?: string; event?: string; mode?: string
  session?: string | null; suggested?: string | null
}

export function suggestedModel(log: string, sessionId: string, now: number): string | undefined {
  const lines = log.trim().split('\n')
  for (let i = lines.length - 1; i >= 0 && i >= lines.length - SCAN_ROWS; i--) {
    let row: Row
    try { row = JSON.parse(lines[i] ?? '') } catch { continue }
    if (row.harness !== 'claude-code' || row.event !== 'prompt' || row.session !== sessionId) continue
    if (!row.ts || now - Date.parse(row.ts) > FRESH_MS) return undefined
    const model = row.mode === 'recommend' ? row.suggested : undefined
    return model && MODEL_ID.test(model) ? model : undefined
  }
  return undefined
}

export const register: Register = (on, options) => {
  if (options.switch_main_model !== true) return
  let decided: { turnId: string; model: string | undefined } | undefined // one decision per turn

  on('turn.step', async function* ($, e, next) {
    if (e.agentId) return yield* next(e) // subagents: the PreToolUse spawn hook covers them
    if (decided?.turnId !== e.turnId) {
      let model: string | undefined
      try {
        const home = await $.env.get('HOME')
        const log = (await $.env.get('NADIR_ROUTE_LOG')) || `${home}/.nadir/route-log.jsonl`
        const [text, id, now] = await Promise.all([$.fs.read(log), $.session.id(), $.clock.now()])
        model = suggestedModel(text, id, now)
      } catch {
        model = undefined
      }
      decided = { turnId: e.turnId, model }
    }
    const { model } = decided
    if (!model || model === e.model) {
      $.ui.status(undefined)
      return yield* next(e)
    }
    $.ui.status(`nadir: ${model}`)
    if (e.index === 0) $.ui.toast(`Nadir: this turn runs on ${model}`)
    return yield* next({ ...e, model })
  })
}
