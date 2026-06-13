/**
 * notificationOsc — every notification shows an inline card; this only decides
 * whether one ALSO fires a desktop OSC (error/warn level, or a completion-ish kind).
 */
import { describe, expect, test } from 'vitest'

import type { ActivityNotification } from '../logic/backgroundActivity.ts'
import { notificationOsc } from '../logic/notificationDispatcher.ts'

function notif(over: Partial<ActivityNotification>): ActivityNotification {
  return { id: 'n', kind: '', level: 'info', text: 'something happened', ...over }
}

describe('notificationOsc', () => {
  test('plain info → no osc', () => {
    expect(notificationOsc(notif({ level: 'info', kind: 'progress' }))).toBeUndefined()
    expect(notificationOsc(notif({ kind: 'started' }))).toBeUndefined()
  })

  test('error and warn levels fire osc', () => {
    expect(notificationOsc(notif({ level: 'error' }))).toBeDefined()
    expect(notificationOsc(notif({ level: 'warn' }))).toBeDefined()
  })

  test('completion-ish kinds fire osc, case-insensitive (complete/done/finish, substring)', () => {
    expect(notificationOsc(notif({ kind: 'task.complete' }))).toBeDefined()
    expect(notificationOsc(notif({ kind: 'JOB_DONE' }))).toBeDefined()
    expect(notificationOsc(notif({ kind: 'Finished' }))).toBeDefined()
    expect(notificationOsc(notif({ kind: 'agent.run.completed' }))).toBeDefined()
  })

  test('osc body == text, title == "Hermes"', () => {
    expect(notificationOsc(notif({ level: 'error', text: 'build broke' }))).toEqual({
      body: 'build broke',
      title: 'Hermes'
    })
  })
})
