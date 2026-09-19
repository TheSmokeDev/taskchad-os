import type { Register } from 'claude-code'

export const register: Register = (on) => {
  let sequence = 0
  on('session.start', async ($, e, next) => {
    const python = await $.env.get('HOMIE_COGNITION_HOOK_PYTHON')
    const command = await $.env.get('HOMIE_COGNITION_HOOK_COMMAND')
    if (python && command) {
      await $.process.run([python, command], { stdin: JSON.stringify({ event: 'session.start', event_id: 'start' }), timeoutMs: 8000 })
    }
    return next(e)
  })
  on('tool.call', async ($, e, next) => {
    let result: unknown
    try {
      result = await next(e)
      return result as Awaited<ReturnType<typeof next>>
    } finally {
      const python = await $.env.get('HOMIE_COGNITION_HOOK_PYTHON')
      const command = await $.env.get('HOMIE_COGNITION_HOOK_COMMAND')
      if (python && command && !e.tool.startsWith('mcp__homie')) {
        await $.process.run([python, command], { stdin: JSON.stringify({ event: 'tool.call', event_id: e.tool_use_id ?? `tool-${++sequence}`, details: { tool: e.tool, result: JSON.stringify(result ?? { failed: true }).slice(0, 12000) } }), timeoutMs: 8000 }).catch(() => undefined)
      }
    }
  })
  on('turn.complete', async ($, e, next) => {
    const python = await $.env.get('HOMIE_COGNITION_HOOK_PYTHON')
    const command = await $.env.get('HOMIE_COGNITION_HOOK_COMMAND')
    if (python && command) {
      await $.process.run([python, command], { stdin: JSON.stringify({ event: 'turn.complete', event_id: e.turnId, details: { reason: e.reason } }), timeoutMs: 8000 })
    }
    return next(e)
  })
  on('prompt.submit', async ($, e, next) => {
    const result = await next(e)
    if (result.drop !== undefined) return result
    const python = await $.env.get('HOMIE_COGNITION_HOOK_PYTHON')
    const command = await $.env.get('HOMIE_COGNITION_HOOK_COMMAND')
    if (python && command) {
      const receipt = await $.process.run([python, command], { stdin: JSON.stringify({ event: 'prompt.submit', event_id: e.turnId ?? 'prompt' }), timeoutMs: 8000 })
      if (receipt.exitCode === 0) {
        const value = JSON.parse(receipt.stdout)
        if (typeof value.context === 'string' && value.context) return { ...result, context: [...(result.context ?? []), value.context] }
      }
    }
    return result
  })
}
