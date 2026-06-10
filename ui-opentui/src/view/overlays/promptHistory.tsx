/**
 * PromptHistory — the Esc+Esc session prompt viewer → Rollback/Undo confirm
 * (Epic 5; the interaction model is free-code's rewind dialog, MessageSelector).
 *
 * Two steps, replacing the composer while open:
 *   1. LIST — this session's user prompts, NEWEST FIRST, ≤7 visible with a
 *      centered window, `▶` pointer + themed highlight on the selection;
 *      ↑↓ navigate, Enter → confirm, Esc closes.
 *   2. CONFIRM — the selected prompt quoted, with exactly the actions the
 *      gateway can honor (logic/promptHistory.confirmOptions): Undo (latest
 *      entry ONLY — `/undo` rewinds just the last exchange) and Rollback
 *      (dispatches `/rollback`; the gateway's checkpoint list lands in the
 *      transcript — prompt→checkpoint mapping isn't possible client-side, see
 *      logic/promptHistory.ts). Enter dispatches via the SAME slash path the
 *      composer uses; Esc backs out to the list.
 *
 * Keys are handled by a global useKeyboard (picker pattern — nothing inside is
 * natively focusable, so a focus-within close layer would never activate).
 */
import { useKeyboard } from '@opentui/solid'
import { createMemo, createSignal, For, Index, Show } from 'solid-js'

import {
  confirmOptions,
  MAX_VISIBLE,
  windowStart,
  type HistoryAction,
  type PromptEntry
} from '../../logic/promptHistory.ts'
import { useTheme } from '../theme.tsx'

/** One line of preview per row (flattened, ellipsized). */
function preview(text: string, max = 72): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

export function PromptHistory(props: {
  /** This session's user prompts, newest first (logic/promptHistory.entries). */
  entries: PromptEntry[]
  /** Dispatch the confirmed action (the App routes it through the slash path). */
  onAction: (action: HistoryAction) => void
  onClose: () => void
}) {
  const theme = useTheme()
  const [sel, setSel] = createSignal(0)
  // undefined = list step; an index into entries = confirm step for that entry.
  const [confirming, setConfirming] = createSignal<number | undefined>(undefined)
  const [actionSel, setActionSel] = createSignal(0)

  const count = () => props.entries.length
  const first = createMemo(() => windowStart(sel(), count()))
  const visible = createMemo(() => props.entries.slice(first(), first() + MAX_VISIBLE))

  const confirmEntry = () => {
    const i = confirming()
    return i === undefined ? undefined : props.entries[i]
  }
  // entries are newest-first → index 0 IS the most recent prompt (Undo-eligible).
  const options = createMemo(() => confirmOptions(confirming() === 0))

  useKeyboard(key => {
    if (key.eventType === 'release') return
    if (key.name === 'escape' || (key.ctrl && key.name === 'c')) {
      // Esc backs out one level: confirm → list, list → closed. Consumed —
      // a closing Esc must never reach later global handlers (review finding).
      key.preventDefault()
      if (confirming() !== undefined) setConfirming(undefined)
      else props.onClose()
      return
    }
    const inConfirm = confirming() !== undefined
    const max = inConfirm ? options().length : count()
    if (key.name === 'up') {
      key.preventDefault()
      if (inConfirm) setActionSel(s => Math.max(0, s - 1))
      else setSel(s => Math.max(0, s - 1))
      return
    }
    if (key.name === 'down') {
      key.preventDefault()
      if (inConfirm) setActionSel(s => Math.min(max - 1, s + 1))
      else setSel(s => Math.min(max - 1, s + 1))
      return
    }
    if (key.name === 'return') {
      key.preventDefault()
      if (!inConfirm) {
        if (count() > 0) {
          setActionSel(0)
          setConfirming(sel())
        }
        return
      }
      const option = options()[actionSel()]
      if (option) {
        props.onAction(option.action)
        props.onClose()
      }
    }
  })

  return (
    <box
      style={{ borderColor: theme().color.border, flexDirection: 'column', flexShrink: 0, marginTop: 1, padding: 1 }}
      border
    >
      <text fg={theme().color.accent}>
        <b>⟲ Rewind</b>
      </text>
      <Show
        when={confirmEntry()}
        fallback={
          <>
            <text fg={theme().color.muted}>This session's prompts, newest first — pick a point:</text>
            <Show when={first() > 0}>
              <text fg={theme().color.muted}>{`  ↑ ${first()} more`}</text>
            </Show>
            <Index each={visible()}>
              {(entry, i) => {
                const selected = () => first() + i === sel()
                return (
                  <box style={{ backgroundColor: selected() ? theme().color.selectionBg : 'transparent' }}>
                    <text selectable={false}>
                      <span style={{ fg: selected() ? theme().color.accent : theme().color.muted }}>
                        {selected() ? '▶ ' : '  '}
                      </span>
                      <span style={{ fg: selected() ? theme().color.text : theme().color.muted }}>
                        {preview(entry().text)}
                      </span>
                      <Show when={first() + i === 0}>
                        <span style={{ fg: theme().color.muted }}> (latest)</span>
                      </Show>
                    </text>
                  </box>
                )
              }}
            </Index>
            <Show when={first() + MAX_VISIBLE < count()}>
              <text fg={theme().color.muted}>{`  ↓ ${count() - first() - MAX_VISIBLE} more`}</text>
            </Show>
            <text fg={theme().color.muted}>↑↓ select · Enter continue · Esc close</text>
          </>
        }
      >
        {entry => (
          <>
            <text fg={theme().color.muted}>Rewind to the point you sent:</text>
            <text fg={theme().color.text}>{`  ❝ ${preview(entry().text)} ❞`}</text>
            <For each={options()}>
              {(option, i) => (
                <box style={{ backgroundColor: i() === actionSel() ? theme().color.selectionBg : 'transparent' }}>
                  <text selectable={false}>
                    <span style={{ fg: i() === actionSel() ? theme().color.accent : theme().color.muted }}>
                      {i() === actionSel() ? '▶ ' : '  '}
                    </span>
                    <span style={{ fg: i() === actionSel() ? theme().color.text : theme().color.muted }}>
                      {option.label}
                    </span>
                  </text>
                </box>
              )}
            </For>
            <text fg={theme().color.muted}>↑↓ select · Enter confirm · Esc back</text>
          </>
        )}
      </Show>
    </box>
  )
}
