import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RpcEvent } from '@/types/hermes'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

/**
 * The LIVE half of the confab-notice contract on desktop (FleetReview P1 on
 * hermes-agent PR #764: "Desktop clients never surface the confabulation
 * notice").
 *
 * `_emit_status` sends the warning through `status_callback` with kind
 * `lifecycle`, and this handler renders nothing for generic lifecycle text —
 * so on the turn the catch actually happens, the desktop was silent. The
 * gateway now re-tags the notice to its own `confab_notice` kind (see
 * `tests/tui_gateway/test_confab_notice_status_kind.py`); this is the
 * consumer that turns that kind into something a user can see.
 *
 * A persistent system row, not a toast: the signal is load-bearing for triage
 * and must not be missable.
 */

const SID = 'session-1'

const NOTICE_TEXT =
  '⚠️ Confabulation caught: the provider detected and removed self-fabricated scaffold text from this reply.'

let stream: MessageStreamHarness

function emit(payload: RpcEvent['payload']) {
  act(() => stream.handleEvent({ payload, session_id: SID, type: 'status.update' }))
}

const messageTexts = () =>
  stream.state(SID).messages.map(m => ({
    role: m.role,
    text: m.parts
      .filter((p): p is Extract<typeof p, { type: 'text' }> => p.type === 'text')
      .map(p => p.text)
      .join('')
  }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('desktop live confab notice', () => {
  it('surfaces a confab_notice status as a system message', () => {
    stream = renderMessageStream(SID)

    emit({ kind: 'confab_notice', text: NOTICE_TEXT })

    const rows = messageTexts()

    expect(rows).toHaveLength(1)
    expect(rows[0]?.role).toBe('system')
    expect(rows[0]?.text).toContain('Confabulation caught')
  })

  it('keeps the warning text intact', () => {
    // A driver that dropped the text would render an empty row — visible but
    // useless for triage.
    stream = renderMessageStream(SID)

    emit({ kind: 'confab_notice', text: NOTICE_TEXT })

    expect(messageTexts()[0]?.text).toBe(NOTICE_TEXT)
  })

  it('renders one row per notice, not a replacing toast', () => {
    stream = renderMessageStream(SID)

    emit({ kind: 'confab_notice', text: NOTICE_TEXT })
    emit({ kind: 'confab_notice', text: NOTICE_TEXT })

    expect(messageTexts()).toHaveLength(2)
  })

  it('ignores an empty notice body', () => {
    stream = renderMessageStream(SID)

    emit({ kind: 'confab_notice', text: '   ' })

    expect(messageTexts()).toHaveLength(0)
  })

  it('does not surface an ordinary lifecycle status', () => {
    // Non-vacuity: the handler must react to the re-tagged kind specifically,
    // not to every status that carries text.
    stream = renderMessageStream(SID)

    emit({ kind: 'lifecycle', text: 'switching to fallback model' })

    expect(messageTexts()).toHaveLength(0)
  })

  it('leaves the other status kinds alone', () => {
    stream = renderMessageStream(SID)

    emit({ kind: 'compacting' })
    emit({ kind: 'compacted' })

    expect(messageTexts()).toHaveLength(0)
  })
})
