/**
 * promptHistory — pure logic for the Esc+Esc session prompt viewer (Epic 5).
 *
 * Model: free-code's rewind dialog (`useDoublePress.ts`, `MessageSelector.tsx`)
 * — 800ms double-press window, only-when-input-empty trigger, 7 visible rows
 * newest-first with a centered window, Enter → confirm step.
 *
 * Semantics (spec Epic 5, RESOLVED block):
 *   - Entries are THIS session's user prompts from the store transcript
 *     (NOT the per-dir JSONL composer history), newest first. Empty → no modal.
 *   - Undo = conversation layer (`/undo` removes the LAST user/assistant
 *     exchange; files kept) → offered ONLY for the most recent prompt. We never
 *     fake arbitrary-depth conversation rewind.
 *   - Rollback = filesystem layer (`/rollback` checkpoints; conversation kept).
 *     Prompt→checkpoint mapping isn't feasible client-side (neither store
 *     messages nor `session.history` carry timestamps to correlate against
 *     `rollback.list`'s checkpoint timestamps), so the honest action is plain
 *     `/rollback`: the gateway's own checkpoint list lands in the transcript
 *     and the user picks `/rollback <n>` from real data.
 */

/** Double-press window (free-code `DOUBLE_PRESS_TIMEOUT_MS`). */
export const DOUBLE_PRESS_WINDOW_MS = 800

/** Max visible prompt rows before the list windows (free-code `MAX_VISIBLE_MESSAGES`). */
export const MAX_VISIBLE = 7

/**
 * Double-press detector (pure state machine; the free-code hook without React).
 * `press(now)` returns true on the SECOND press within the window — and then
 * disarms, so a third press starts a fresh cycle. `reset()` disarms (call it on
 * any intervening key, and never call `press` for an Esc something else
 * consumed — that's what keeps a dropdown-dismiss Esc from arming).
 */
export interface DoublePress {
  press(now?: number): boolean
  reset(): void
}

export function createDoublePress(windowMs: number = DOUBLE_PRESS_WINDOW_MS): DoublePress {
  let armedAt: number | undefined
  return {
    // performance.now() is monotonic (Node) — an NTP/wall-clock jump between
    // two presses can't break or spuriously satisfy the window (review finding).
    press(now: number = performance.now()): boolean {
      if (armedAt !== undefined && now - armedAt <= windowMs) {
        armedAt = undefined
        return true
      }
      armedAt = now
      return false
    },
    reset(): void {
      armedAt = undefined
    }
  }
}

/** One viewer row: a user prompt of THIS session. `index` is its position in
 *  the source transcript (stable identity across renders). */
export interface PromptEntry {
  readonly index: number
  readonly text: string
}

/**
 * Source the viewer entries from the store transcript: USER prompts only,
 * non-empty, NEWEST FIRST. Session-only by construction (the store holds only
 * this session's messages). Empty session → [] (the trigger shows nothing).
 */
export function promptHistoryEntries(messages: ReadonlyArray<{ readonly role: string; text: string }>): PromptEntry[] {
  const entries: PromptEntry[] = []
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m && m.role === 'user' && m.text.trim() !== '') entries.push({ index: i, text: m.text })
  }
  return entries
}

/** A confirm-step action. */
export type HistoryAction = 'undo' | 'rollback'

export interface ConfirmOption {
  readonly action: HistoryAction
  readonly label: string
}

/** The exact signed-off confirm labels (spec Epic 5). */
export const UNDO_LABEL = 'Undo — rewind the conversation (files kept)'
export const ROLLBACK_LABEL = 'Rollback — restore files from checkpoint (conversation kept)'

/**
 * The confirm-step options for a selected entry. `/undo` only removes the LAST
 * exchange, so Undo is offered ONLY for the most recent prompt (`isLatest`) —
 * an option the gateway can't honor is hidden, never a dead button. Rollback
 * (filesystem checkpoints) applies regardless of the selected depth.
 */
export function confirmOptions(isLatest: boolean): ConfirmOption[] {
  const options: ConfirmOption[] = []
  if (isLatest) options.push({ action: 'undo', label: UNDO_LABEL })
  options.push({ action: 'rollback', label: ROLLBACK_LABEL })
  return options
}

/** The slash command an action dispatches — through the SAME command path the
 *  composer uses (`dispatchSlash` → `slash.exec`/`command.dispatch`). */
export function actionCommand(action: HistoryAction): string {
  return action === 'undo' ? '/undo' : '/rollback'
}

/**
 * First visible row index for a list window: keep the selection centered until
 * the window hits either end (free-code `firstVisibleIndex`). Total ≤ visible
 * → 0 (everything shows).
 */
export function windowStart(selected: number, total: number, visible: number = MAX_VISIBLE): number {
  return Math.max(0, Math.min(selected - Math.floor(visible / 2), total - visible))
}
