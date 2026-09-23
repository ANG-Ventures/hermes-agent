import { describe, expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import { toChatMessages } from './chat-messages'

/**
 * Desktop rendering of the out-of-band confab notice (FleetReview P1s on
 * hermes-agent PR #764: "Desktop clients never surface the confabulation
 * notice" and "Persisted notices are invisible in reloaded TUI and desktop
 * sessions").
 *
 * Adding the discriminator to the `SessionMessage` type did not make the
 * notice visible: `toChatMessages` only special-cased model-switch,
 * delegation, auto-continue and personality-switch rows, so a `confab_notice`
 * row hydrated as an ordinary assistant reply and both presentation fields
 * were discarded.
 *
 * The row still carries the real model reply, so the marker is a SEPARATE
 * system row rather than a content replacement — unlike the other display
 * kinds, whose content IS the event label.
 */

const NOTICE = {
  grammar: 'inbound',
  kind: 'scaffold_confab_removed',
  request_id: '3b264082',
  scope: 'visible',
  version: 1
}

const noticeRow = (over: Record<string, unknown> = {}): SessionMessage =>
  ({
    content: 'All good here.',
    display_kind: 'confab_notice',
    display_metadata: { confab_notice: NOTICE },
    role: 'assistant',
    timestamp: 1_000,
    ...over
  }) as unknown as SessionMessage

const texts = (messages: SessionMessage[]) =>
  toChatMessages(messages).map(m => ({
    role: m.role,
    text: m.parts
      .filter((p): p is Extract<typeof p, { type: 'text' }> => p.type === 'text')
      .map(p => p.text)
      .join('')
  }))

const markers = (messages: SessionMessage[]) => texts(messages).filter(m => m.text.includes('confabulation caught'))

describe('desktop hydration of a confab notice row', () => {
  it('surfaces a system marker for a genuine assistant notice row', () => {
    expect(markers([noticeRow()])).toHaveLength(1)
    expect(markers([noticeRow()])[0]?.role).toBe('system')
  })

  it('still renders the assistant reply itself', () => {
    const out = texts([noticeRow()])
    const assistant = out.filter(m => m.role === 'assistant')

    expect(assistant).toHaveLength(1)
    expect(assistant[0]?.text).toBe('All good here.')
  })

  it('does not replace the reply content with the event label', () => {
    // This is what makes confab_notice different from model_switch et al:
    // that row's content IS the label; this row's content is real output.
    const assistant = texts([noticeRow()]).find(m => m.role === 'assistant')

    expect(assistant?.text).not.toContain('confabulation')
    expect(assistant?.text).not.toContain(NOTICE.request_id)
  })

  it('orders the marker before the reply it describes', () => {
    const out = texts([noticeRow()])

    expect(out[0]?.role).toBe('system')
    expect(out[0]?.text).toContain('confabulation caught')
    expect(out[1]?.text).toBe('All good here.')
  })

  it('renders nothing extra for a clean assistant turn', () => {
    // Non-vacuity guard.
    const clean = [{ content: 'All good here.', role: 'assistant', timestamp: 1 }] as SessionMessage[]

    expect(markers(clean)).toHaveLength(0)
    expect(texts(clean)).toHaveLength(1)
  })
})

describe('desktop hydration gating', () => {
  // `display_kind` is an open-ended string column. A reloaded user/system row
  // carrying it — imported or malformed history — must not be presented as a
  // confirmed catch (the sibling "False Notice" P1).

  it.each(['user', 'system'])('does not claim a catch on a reloaded %s row', role => {
    expect(markers([noticeRow({ role })])).toHaveLength(0)
  })

  it('does not claim a catch when the metadata is missing', () => {
    expect(markers([noticeRow({ display_metadata: undefined })])).toHaveLength(0)
  })

  it.each([
    ['unknown version', { confab_notice: { ...NOTICE, version: 99 } }],
    ['unknown kind', { confab_notice: { ...NOTICE, kind: 'totally_made_up' } }],
    ['empty request_id', { confab_notice: { ...NOTICE, request_id: '' } }],
    ['bad scope', { confab_notice: { ...NOTICE, scope: 'everything' } }],
    ['wrong key', { something_else: NOTICE }]
  ])('does not claim a catch when the metadata is invalid (%s)', (_label, display_metadata) => {
    expect(markers([noticeRow({ display_metadata })])).toHaveLength(0)
  })

  it('still renders the reply when the notice metadata fails validation', () => {
    const row = noticeRow({ display_metadata: { confab_notice: { version: 99 } } })

    expect(markers([row])).toHaveLength(0)
    expect(texts([row]).find(m => m.role === 'assistant')?.text).toBe('All good here.')
  })

  it('accepts display_metadata served as raw JSON text by an older backend', () => {
    // A remote backend older than this app serves the column as JSON text.
    const row = noticeRow({ display_metadata: JSON.stringify({ confab_notice: NOTICE }) })

    expect(markers([row])).toHaveLength(1)
  })

  it('does not claim a catch on unparseable JSON metadata', () => {
    expect(markers([noticeRow({ display_metadata: '{not json' })])).toHaveLength(0)
  })
})
