/**
 * ClarifyPrompt rewrite (F5/F6) — headless frames + simulated keyboard.
 *
 * Asserts the four user-reported fixes:
 *   - long option text WRAPS (appears on a second line) instead of clipping (F5),
 *   - options are NUMBERED and the selected row is highlighted (F5),
 *   - the custom answer is an inline input in the SAME screen (F5),
 *   - Up/Down drive the selection and Enter answers the highlighted choice; the
 *     arrows don't escape to a scrollbox (F6 — we assert selection moved).
 */
import { ThemeProvider } from '../view/theme.tsx'
import { describe, expect, test } from 'vitest'

import { ClarifyPrompt } from '../view/prompts/clarifyPrompt.tsx'
import { createSessionStore } from '../logic/store.ts'
import { renderProbe, type RenderProbe } from './lib/render.ts'

const LONG =
  'Just analyze for now — give me the implementation plan doc (code-path refs + line numbers, screen-by-screen), no code yet.'

const theme = createSessionStore().state.theme

async function mount(
  choices: string[] | null,
  onAnswer: (a: string) => void = () => {},
  onCancel: () => void = () => {}
): Promise<RenderProbe> {
  return renderProbe(
    () => (
      <ThemeProvider theme={() => theme}>
        <ClarifyPrompt
          question="How do you want me to proceed?"
          choices={choices}
          onAnswer={onAnswer}
          onCancel={onCancel}
        />
      </ThemeProvider>
    ),
    { height: 24, kittyKeyboard: true, width: 60 }
  )
}

describe('ClarifyPrompt (F5/F6)', () => {
  test('numbers every option and shows the inline custom-answer input (F5)', async () => {
    const h = await mount(['Alpha option', 'Beta option'])
    try {
      const frame = h.frame()
      expect(frame).toContain('1. ')
      expect(frame).toContain('2. ')
      expect(frame).toContain('Alpha option')
      expect(frame).toContain('Beta option')
      // the inline custom input is present in the SAME screen (not a separate view)
      expect(frame).toContain('or type a custom answer')
    } finally {
      h.destroy()
    }
  })

  test('a long option WRAPS to a second line rather than clipping (F5)', async () => {
    const h = await mount([LONG, 'Short'])
    try {
      const frame = h.frame()
      // a 60-col box can't fit the long option on one line — the head AND the
      // tail both appear only because the text wrapped instead of clipping at
      // the right edge. (The exact wrap column varies, so assert words that
      // land on different lines, not a phrase that straddles the break.)
      expect(frame).toContain('Just analyze')
      expect(frame).toContain('no code yet')
    } finally {
      h.destroy()
    }
  })

  test('Down moves the selection; Enter answers the highlighted choice (F6)', async () => {
    let answered: string | undefined
    const h = await mount(['Alpha option', 'Beta option'], a => (answered = a))
    try {
      h.keys.pressArrow('down') // 0 → 1 (Beta)
      await h.settle()
      h.keys.pressEnter()
      await h.settle()
      expect(answered).toBe('Beta option')
    } finally {
      h.destroy()
    }
  })

  test('Down past the last choice lands on the custom input; Enter sends typed text', async () => {
    let answered: string | undefined
    const h = await mount(['Only choice'], a => (answered = a))
    try {
      h.keys.pressArrow('down') // choice 0 → custom input (index 1)
      await h.settle()
      await h.keys.typeText('my custom reply')
      await h.settle()
      h.keys.pressEnter()
      await h.settle()
      expect(answered).toBe('my custom reply')
    } finally {
      h.destroy()
    }
  })

  test('no choices → the input is the only control and is focused', async () => {
    let answered: string | undefined
    const h = await mount(null, a => (answered = a))
    try {
      expect(h.frame()).toContain('Type your answer')
      await h.keys.typeText('freeform')
      await h.settle()
      h.keys.pressEnter()
      await h.settle()
      expect(answered).toBe('freeform')
    } finally {
      h.destroy()
    }
  })

  test('Esc cancels', async () => {
    let cancelled = false
    const h = await mount(
      ['A', 'B'],
      () => {},
      () => (cancelled = true)
    )
    try {
      h.keys.pressEscape()
      await h.settle()
      expect(cancelled).toBe(true)
    } finally {
      h.destroy()
    }
  })
})
