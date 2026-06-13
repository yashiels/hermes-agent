/**
 * Notification → desktop-OSC decision. EVERY notification renders an inline
 * transcript card (so there's nothing to decide there); this only decides whether
 * a notification is important enough to ALSO fire a desktop/terminal OSC ping
 * (to pull the user back). The OSC payload is termChrome's `TermNotification`;
 * the boundary (terminalChrome) owns the actual escape-sequence write.
 */
import type { ActivityNotification } from './backgroundActivity.ts'
import type { TermNotification } from './termChrome.ts'

/** Kind substrings that mark a "the work finished, look here" notification —
 *  matched case-insensitively anywhere in the kind. */
const COMPLETION_KIND_HINTS = ['complete', 'done', 'finish']

function isImportant(n: ActivityNotification): boolean {
  if (n.level === 'error' || n.level === 'warn') return true
  const kind = n.kind.toLowerCase()
  return COMPLETION_KIND_HINTS.some(hint => kind.includes(hint))
}

/**
 * The desktop OSC notification for `n`, or `undefined` when it's not important
 * enough to interrupt — level 'error'/'warn', or a kind containing
 * 'complete'/'done'/'finish' (case-insensitive). Title is always 'Hermes' with
 * the notification text as the body.
 */
export function notificationOsc(n: ActivityNotification): TermNotification | undefined {
  return isImportant(n) ? { body: n.text, title: 'Hermes' } : undefined
}
