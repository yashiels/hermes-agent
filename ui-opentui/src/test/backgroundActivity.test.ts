/**
 * Background-activity logic tests — pure parsers + derive helpers. Everything
 * off the wire is `unknown`, so the parsers must defend against garbage/missing
 * fields and map snake_case → camelCase.
 */
import { describe, expect, test } from 'vitest'

import {
  type BackgroundProcess,
  parseNotification,
  parseProcessList,
  runningCount
} from '../logic/backgroundActivity.ts'

describe('parseNotification', () => {
  test('happy path: full payload, snake_case ttl_ms → ttlMs', () => {
    expect(
      parseNotification({ id: 'job-1', key: 'k1', kind: 'task.complete', level: 'warn', text: 'done', ttl_ms: 5000 })
    ).toEqual({ id: 'job-1', key: 'k1', kind: 'task.complete', level: 'warn', text: 'done', ttlMs: 5000 })
  })

  test('garbage / missing level coerces to info; missing kind → ""', () => {
    expect(parseNotification({ id: 'a', level: 'screaming', text: 'hi' })).toEqual({
      id: 'a',
      kind: '',
      level: 'info',
      text: 'hi'
    })
    expect(parseNotification({ id: 'b', text: 'no level' })?.level).toBe('info')
  })

  test('missing/empty text → null (text is load-bearing for the card)', () => {
    expect(parseNotification({ id: 'a', level: 'info' })).toBeNull()
    expect(parseNotification({ id: 'a', text: '' })).toBeNull()
    expect(parseNotification(null)).toBeNull()
    expect(parseNotification('nope')).toBeNull()
  })

  test('id falls back to key when id is absent', () => {
    const n = parseNotification({ key: 'k-only', text: 'hello' })
    expect(n?.id).toBe('k-only')
    expect(n?.key).toBe('k-only')
  })

  test('no id and no key → synthesized stable id `n:${text}`', () => {
    const n = parseNotification({ text: 'build finished' })
    expect(n?.id).toBe('n:build finished')
    expect(n?.key).toBeUndefined()
  })

  test('id is preferred over key when both present', () => {
    expect(parseNotification({ id: 'real', key: 'k', text: 'x' })?.id).toBe('real')
  })

  test('non-number ttl_ms is dropped (no ttlMs)', () => {
    const n = parseNotification({ id: 'a', text: 'x', ttl_ms: 'soon' })
    expect(n?.ttlMs).toBeUndefined()
  })
})

describe('parseProcessList', () => {
  test('maps good rows, snake_case → camelCase', () => {
    expect(
      parseProcessList({
        processes: [
          { command: 'npm test', session_id: 's1', status: 'running', uptime_seconds: 12 },
          { command: 'build', session_id: 's2', status: 'exited', uptime_seconds: 99 }
        ]
      })
    ).toEqual([
      { command: 'npm test', sessionId: 's1', status: 'running', uptimeSeconds: 12 },
      { command: 'build', sessionId: 's2', status: 'exited', uptimeSeconds: 99 }
    ])
  })

  test('skips malformed rows (missing session_id or command); defaults status/uptime', () => {
    expect(
      parseProcessList({
        processes: [
          { command: 'ok', session_id: 's1' }, // no status/uptime → defaults
          { command: 'no-session' }, // dropped
          { session_id: 's3' }, // dropped
          null, // dropped
          'garbage' // dropped
        ]
      })
    ).toEqual([{ command: 'ok', sessionId: 's1', status: '', uptimeSeconds: 0 }])
  })

  test('non-object / missing processes → []', () => {
    expect(parseProcessList(null)).toEqual([])
    expect(parseProcessList({})).toEqual([])
    expect(parseProcessList({ processes: 'nope' })).toEqual([])
  })
})

describe('runningCount', () => {
  const procs: BackgroundProcess[] = [
    { command: 'a', sessionId: 's1', status: 'running', uptimeSeconds: 1 },
    { command: 'b', sessionId: 's2', status: 'exited', uptimeSeconds: 1 },
    { command: 'c', sessionId: 's3', status: 'Sleeping', uptimeSeconds: 1 }, // unknown → running (lenient)
    { command: 'd', sessionId: 's4', status: 'DONE', uptimeSeconds: 1 }, // case-insensitive terminal
    { command: 'e', sessionId: 's5', status: 'killed', uptimeSeconds: 1 }
  ]

  test('counts running-ish processes (lenient on unknown statuses)', () => {
    // running + Sleeping = 2 (exited/DONE/killed excluded)
    expect(runningCount(procs)).toBe(2)
  })

  test('empty input → 0', () => {
    expect(runningCount([])).toBe(0)
  })
})
