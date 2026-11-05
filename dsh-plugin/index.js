/**
 * Host half of @local/dsh-plugin-dingtalk-notify.
 *
 * DSH already records every turn as an event-sourced log, so this plugin never
 * reads files or polls: it listens on `session/event`, keeps one tally per
 * session, and on `turn/end` hands a finished summary to the existing
 * `notify-dingtalk.py`. `approval/asked` becomes the "waiting for you" push.
 *
 * The Python side owns the transport (MCP / enterprise robot / webhook) and all
 * formatting, so this file only shapes data. A failed push never touches a
 * turn: every error path is swallowed and logged.
 *
 * Not covered on purpose: Claude Code work items opened from DSH run their own
 * `claude -p` process and write their own transcript; they never produce DSH
 * session events, so their Stop hook keeps reporting them (nothing to dedupe).
 *
 * @module @local/dsh-plugin-dingtalk-notify
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, join } from 'node:path'

/** Sensible defaults; every field can be overridden from `cordis.patch.yml`. */
const DEFAULTS = {
  enabled: true,
  /** Python 3.9+ executable. Empty means "try `python`, then `python3`, then `py`". */
  python: '',
  /** notify-dingtalk.py. Empty means `~/.claude/notify-dingtalk.py`. */
  script: '',
  /** Notify on turn/end. */
  notifyTurnEnd: true,
  /** Notify when a tool approval is waiting. */
  notifyApproval: true,
  /** Turns shorter than this are skipped, matching the script's own threshold. */
  minDurationSeconds: 0,
  /** Hard stop for one Python run; the push itself times out after 6 s. */
  timeoutMs: 30_000,
}

/** Per-session tally between `turn/start` and `turn/end`. */
function freshTurn() {
  return {
    startedAt: null,
    prompt: '',
    tools: Object.create(null),
    /** callId → argument preview, for naming the tool an approval is about. */
    callArguments: new Map(),
    input: 0,
    output: 0,
    cacheRead: 0,
    requests: 0,
    interrupted: false,
    lastReply: '',
  }
}

/** Join the text blocks of one message's content. */
function textOf(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
}

/**
 * Render one value for a human: strings pass through, arrays join, objects fall
 * back to JSON.
 * @param {unknown} value - a tool argument or a reason.
 * @returns {string} one line of text.
 */
function asText(value) {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join('\n')
  if (value === null || value === undefined) return ''
  try {
    return JSON.stringify(value, null, 0)
  } catch {
    return String(value)
  }
}

/** The most telling part of a tool's arguments, for a one-line preview. */
function briefOf(args) {
  if (args === null || typeof args !== 'object') return asText(args)
  const candidate = args.command ?? args.file_path ?? args.path ?? args.url ?? args.pattern ?? args.query
  if (typeof candidate === 'string' && candidate !== '') return candidate
  return asText(args)
}

/**
 * Register the listeners.
 * @param ctx - Host Cordis context.
 * @param config - options merged over {@link DEFAULTS}.
 */
export function apply(ctx, config) {
  const options = { ...DEFAULTS, ...(config ?? {}) }
  const turns = new Map()
  const logger = ctx.logger ?? console

  const script = options.script || join(homedir(), '.claude', 'notify-dingtalk.py')
  const pythonCandidates = options.python
    ? [options.python]
    : ['python', 'python3', 'py']

  if (!existsSync(script)) {
    logger.warn(`dingtalk-notify: script not found at ${script}; nothing will be pushed`)
    return
  }

  /** Hand one payload to the Python script. Never throws, never blocks a turn. */
  function notify(payload) {
    if (!options.enabled) return
    const argv = ['-I', '-X', 'utf8', script, 'dsh']
    const body = Buffer.from(JSON.stringify(payload), 'utf8')

    const attempt = (index) => {
      if (index >= pythonCandidates.length) {
        logger.warn('dingtalk-notify: no working python interpreter found')
        return
      }
      let child
      try {
        child = spawn(pythonCandidates[index], argv, {
          stdio: ['pipe', 'ignore', 'ignore'],
          windowsHide: true,
        })
      } catch (error) {
        logger.warn(`dingtalk-notify: spawn ${pythonCandidates[index]} failed: ${String(error)}`)
        attempt(index + 1)
        return
      }

      let settled = false
      const settle = () => {
        settled = true
        clearTimeout(timer)
      }
      const timer = setTimeout(() => {
        settle()
        try {
          child.kill()
        } catch {
          // Already gone.
        }
      }, options.timeoutMs)
      timer.unref?.()

      child.on('error', (error) => {
        if (settled) return
        settle()
        // A missing interpreter is the one failure worth retrying with the next name.
        if (error?.code === 'ENOENT') attempt(index + 1)
        else logger.warn(`dingtalk-notify: ${String(error)}`)
      })
      child.on('close', (code) => {
        if (settled) return
        settle()
        if (code !== 0) logger.warn(`dingtalk-notify: python exited with ${code}`)
      })
      // A child that dies before reading closes the pipe; `close` reports why.
      child.stdin.on('error', () => {})
      child.stdin.end(body)
      child.unref?.()
    }

    attempt(0)
  }

  ctx.on('session/event', (session, event) => {
    // Subagent sessions would multiply every reply; their parent reports the turn.
    if (session.header?.origin === 'subagent') return

    const id = session.id

    switch (event.type) {
      case 'turn/start': {
        const turn = freshTurn()
        turn.startedAt = event.time
        turns.set(id, turn)
        return
      }

      case 'user/message': {
        const turn = turns.get(id)
        if (turn === undefined) return
        // Only the human's own words: runtime context, policy notices and
        // agent messages are also `user/message`s but carry another `source.kind`.
        if (event.data?.source?.kind !== 'user') return
        const text = textOf(event.data)
        if (text !== '') turn.prompt = text
        return
      }

      case 'assistant/message': {
        const turn = turns.get(id)
        if (turn === undefined) return
        turn.requests += 1
        const usage = event.data?.usage
        if (usage) {
          turn.input += Number(usage.inputTokens ?? 0)
          turn.output += Number(usage.outputTokens ?? 0)
          turn.cacheRead += Number(usage.cacheReadTokens ?? 0)
        }
        if (event.data?.interrupted === true) turn.interrupted = true
        const text = textOf(event.data?.message)
        if (text !== '') turn.lastReply = text
        return
      }

      case 'tool/call': {
        const turn = turns.get(id)
        if (turn === undefined) return
        const name = event.data?.name ?? '?'
        turn.tools[name] = (turn.tools[name] ?? 0) + 1
        // Remember what each call was about so a later approval can name it.
        if (typeof event.data?.callId === 'string') {
          turn.callArguments.set(event.data.callId, briefOf(event.data.arguments))
        }
        return
      }

      case 'approval/asked': {
        if (!options.notifyApproval) return
        const callId = event.data?.callId
        const args = turns.get(id)?.callArguments.get(callId) ?? ''
        notify({
          kind: 'permission_prompt',
          host: 'DSH',
          session_id: id,
          cwd: session.header?.cwd ?? '',
          tool: event.data?.toolName ?? '?',
          // The tool's own arguments say more than the generic reason text.
          brief: args !== '' ? args : asText(event.data?.reason ?? ''),
        })
        return
      }

      case 'turn/end': {
        const turn = turns.get(id)
        turns.delete(id)
        if (!options.notifyTurnEnd) return
        // Loading this plugin in the middle of a turn means no `turn/start` was
        // seen: there is no tally and nothing worth pushing.
        if (turn === undefined) return

        const now = event.time ?? Date.now()
        const started = turn.startedAt ?? now
        const duration = Math.max(0, (now - started) / 1000)
        if (duration < options.minDurationSeconds) return

        const cwd = session.header?.cwd ?? ''
        const reason = event.data?.reason ?? {}
        notify({
          host: 'DSH',
          session_id: id,
          cwd,
          project: cwd === '' ? id : basename(cwd),
          duration,
          last_assistant_message: turn.lastReply,
          stats: {
            ok: true,
            prompt: turn.prompt,
            tools: turn.tools,
            input_tokens: turn.input,
            output_tokens: turn.output,
            cache_read: turn.cacheRead,
            requests: turn.requests,
            interrupted: turn.interrupted || reason.kind === 'aborted',
            error: reason.kind === 'error',
          },
        })
        return
      }

      default:
        return
    }
  })
}

/** Loader entry name, matching the `name` field in cordis.patch.yml. */
export const name = 'dingtalk-notify'

/** Services this plugin waits for: none — Python is spawned by Node directly. */
export const inject = []
