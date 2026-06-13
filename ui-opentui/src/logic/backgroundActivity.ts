/**
 * Background-activity logic — pure parsers + derive helpers for the "ambient
 * activity" feature (notifications, long-running processes, background runs).
 * No state container here: the store owns the arrays; these functions parse
 * loose wire payloads (everything off the gateway is `unknown`) and compute
 * derived values over immutable arrays. Mirrors the defensive loose-read style
 * of `logic/slash.ts` (`readStr`) and the snake_case→camel mapping the wire
 * needs.
 *
 * Wire shapes (see boundary/schema/GatewayEvent.ts ~134):
 *   notification.show  payload {text, level, kind, ttl_ms, key, id}  (loose Record)
 *   notification.clear payload {key}
 *   agents.list result {processes:[{session_id, command, status, uptime_seconds}]}
 */

export interface ActivityNotification {
  id: string
  key?: string
  text: string
  level: 'info' | 'warn' | 'error'
  kind: string
  ttlMs?: number
}

export interface BackgroundProcess {
  sessionId: string
  command: string
  status: string
  uptimeSeconds: number
}

/** Loose-read a string field off an `unknown` object (slash.ts `readStr` style). */
function readStr(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = (value as { [k: string]: unknown })[key]
  return typeof v === 'string' ? v : undefined
}

/** Loose-read a finite number off an `unknown` object. */
function readNum(value: unknown, key: string): number | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = (value as { [k: string]: unknown })[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

/** Coerce any wire `level` to the closed union; anything that isn't a known
 *  level (absent, garbage, wrong-typed) falls back to 'info'. */
function coerceLevel(value: unknown): ActivityNotification['level'] {
  return value === 'warn' || value === 'error' ? value : 'info'
}

/**
 * Parse a `notification.show` payload (unknown) → ActivityNotification, or null
 * when there's no usable text (text is the load-bearing field — without it the
 * card has nothing to show). Maps snake_case `ttl_ms` → `ttlMs`, coerces a
 * garbage/missing `level` to 'info', and defaults `kind` to ''.
 *
 * id resolution: prefer the wire `id`, then fall back to `key`, else synthesize
 * `id = `n:${text}`` (a stable, text-derived id rather than a random one). The
 * original `key` (if any) is preserved separately so notification.clear by key
 * still targets the right cards.
 */
export function parseNotification(payload: unknown): ActivityNotification | null {
  const text = readStr(payload, 'text')
  if (!text) return null
  const key = readStr(payload, 'key')
  const id = readStr(payload, 'id') ?? key ?? `n:${text}`
  const out: ActivityNotification = {
    id,
    kind: readStr(payload, 'kind') ?? '',
    level: coerceLevel((payload as { level?: unknown } | null | undefined)?.level),
    text
  }
  if (key !== undefined) out.key = key
  const ttlMs = readNum(payload, 'ttl_ms')
  if (ttlMs !== undefined) out.ttlMs = ttlMs
  return out
}

/** Parse an `agents.list` result ({processes:[...]}) → BackgroundProcess[],
 *  skipping malformed rows (a row missing session_id/command is dropped, not
 *  defaulted). snake_case `session_id`/`uptime_seconds` → camelCase; a missing
 *  uptime defaults to 0, a missing status to ''. */
export function parseProcessList(result: unknown): BackgroundProcess[] {
  if (!result || typeof result !== 'object') return []
  const processes = (result as { processes?: unknown }).processes
  if (!Array.isArray(processes)) return []
  const out: BackgroundProcess[] = []
  for (const row of processes) {
    const sessionId = readStr(row, 'session_id')
    const command = readStr(row, 'command')
    if (!sessionId || !command) continue
    out.push({
      command,
      sessionId,
      status: readStr(row, 'status') ?? '',
      uptimeSeconds: readNum(row, 'uptime_seconds') ?? 0
    })
  }
  return out
}

/** Terminal (no-longer-running) process statuses. A process whose status is
 *  NOT one of these is treated as running — leniently, because the gateway's
 *  status vocabulary is open-ended and we'd rather over-count the ambient badge
 *  than silently hide a still-live process under an unfamiliar status string.
 *  Matched case-insensitively after trimming. */
/** Terminal (no-longer-running) process statuses — exported as the single
 *  source of truth (the panel imports `procIsRunning` rather than re-declaring). */
export const DONE_STATUSES = new Set(['exited', 'failed', 'complete', 'done', 'killed'])

/** Whether a process status is "running-ish": NOT one of DONE_STATUSES. Lenient
 *  by design — the gateway's status vocabulary is open-ended, so we over-count
 *  rather than hide a live process under an unfamiliar status. Case-insensitive. */
export function procIsRunning(status: string): boolean {
  return !DONE_STATUSES.has(status.trim().toLowerCase())
}

/** Count of running background processes (the ambient `bg:` badge). */
export function runningCount(procs: readonly BackgroundProcess[]): number {
  return procs.filter(p => procIsRunning(p.status)).length
}
