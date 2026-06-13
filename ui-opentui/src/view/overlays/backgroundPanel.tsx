/**
 * BackgroundPanel — the OS-level "background processes" overlay. Mirrors
 * `agentsDashboard.tsx`'s shell (bordered full-height root, header, scrollable
 * list, footer hint, `useCloseLayer` + `useKeyboard` + onMount focus).
 *
 * The gateway only exposes a single STOP-ALL (`kill_all`), NOT per-process kill,
 * so the only action is `x` → stop all (no per-row kill). `r` refreshes; the
 * controller wires both to the gateway.
 */
import { type BoxRenderable } from '@opentui/core'
import { useKeyboard } from '@opentui/solid'
import { For, type JSXElement, onMount, Show } from 'solid-js'

import { procIsRunning, type BackgroundProcess } from '../../logic/backgroundActivity.ts'
import { truncRight } from '../../logic/truncate.ts'
import { useDimensions } from '../dimensions.tsx'
import { useCloseLayer } from '../keymap.tsx'
import { useTheme } from '../theme.tsx'

function statusColor(status: string, theme: ReturnType<typeof useTheme>): string {
  const c = theme().color
  const s = status.toLowerCase()
  if (s === 'failed' || s.includes('error')) return c.error
  if (s === 'exited' || s === 'complete' || s === 'done') return c.ok
  if (procIsRunning(status)) return c.accent
  return c.muted
}

/** Compact inline uptime: <60 → 'Ns', <3600 → 'Nm', else 'Hh MMm'. */
function fmtUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${String(m).padStart(2, '0')}m`
}

export function BackgroundPanel(props: {
  processes: BackgroundProcess[]
  onRefresh: () => void
  onStopAll: () => void
  onClose: () => void
}): JSXElement {
  const theme = useTheme()
  const dims = useDimensions()
  let rootRef: BoxRenderable | undefined

  const running = () => props.processes.filter(p => procIsRunning(p.status)).length

  // Focus the root so the focus-within close layer is active; refresh once on
  // open so the list is fresh.
  onMount(() => {
    rootRef?.focus()
    props.onRefresh()
  })
  useCloseLayer(
    () => rootRef,
    () => props.onClose()
  )

  useKeyboard(key => {
    // Esc/Ctrl+C close via the keymap (useCloseLayer); the rest here.
    if (key.name === 'q') return props.onClose()
    if (key.name === 'r') return props.onRefresh()
    if (key.name === 'x') return props.onStopAll()
  })

  return (
    <box
      ref={el => (rootRef = el)}
      focusable
      style={{ borderColor: theme().color.accent, flexDirection: 'column', flexGrow: 1, minHeight: 0 }}
      border
    >
      <box style={{ flexShrink: 0, paddingLeft: 1 }}>
        <text fg={theme().color.accent} selectable={false}>
          <b>▦ Background processes · {running()} running</b>
        </text>
      </box>

      <box style={{ flexGrow: 1, minHeight: 0, paddingLeft: 1, flexDirection: 'column' }}>
        <Show
          when={props.processes.length > 0}
          fallback={
            <text fg={theme().color.muted} selectable={false}>
              No background processes running.
            </text>
          }
        >
          <scrollbox style={{ flexGrow: 1, minHeight: 0 }} stickyScroll stickyStart="top">
            <For each={props.processes}>
              {proc => {
                // Budget the command into the leftover row width so it never
                // wraps: total − border/pad − glyph(2) − ` · <uptime>  <status>` tail.
                const tail = () => ` · ${fmtUptime(proc.uptimeSeconds)}  ${proc.status}`
                const cmdMax = () => Math.max(8, dims().width - 4 - 2 - tail().length)
                return (
                  <text selectable={false}>
                    <span style={{ fg: statusColor(proc.status, theme) }}>● </span>
                    <span style={{ fg: theme().color.text }}>{truncRight(proc.command, cmdMax())}</span>
                    <span style={{ fg: theme().color.muted }}>{` · ${fmtUptime(proc.uptimeSeconds)}  `}</span>
                    <span style={{ fg: statusColor(proc.status, theme) }}>{proc.status}</span>
                  </text>
                )
              }}
            </For>
          </scrollbox>
        </Show>
      </box>

      <box style={{ flexShrink: 0, paddingLeft: 1 }}>
        <text fg={theme().color.muted} selectable={false}>
          Esc/q close · r refresh · x stop all
        </text>
      </box>
    </box>
  )
}
