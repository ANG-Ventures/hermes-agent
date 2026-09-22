import { describe, expect, it } from 'vitest'

import { toTranscriptMessages } from '../domain/messages.js'

/**
 * Reloaded-session rendering of the out-of-band confab notice (FleetReview P1
 * on hermes-agent PR #764: "Persisted notices are invisible in reloaded TUI
 * and desktop sessions").
 *
 * The gateway forwards `display_kind` / `display_metadata` on the assistant
 * row, but `toTranscriptMessages` used to ignore both and render the row as an
 * ordinary reply — so after a reload an operator could not tell the turn had a
 * confirmed catch, defeating the durable-triage purpose of the whole feature.
 *
 * The row still carries the real model reply, so the marker is an ADDITIONAL
 * event line, not a replacement for the text.
 */

const NOTICE = {
  grammar: 'inbound',
  kind: 'scaffold_confab_removed',
  request_id: '3b264082',
  scope: 'visible',
  version: 1
}

const noticeRow = (over: Record<string, unknown> = {}) => ({
  display_kind: 'confab_notice',
  display_metadata: { confab_notice: NOTICE },
  role: 'assistant',
  text: 'All good here.',
  ...over
})

const events = (rows: unknown[]) => toTranscriptMessages(rows).filter(m => m.kind === 'event')

describe('confab notice on reload', () => {
  it('renders an event marker for a genuine assistant notice row', () => {
    const out = toTranscriptMessages([{ role: 'user', text: 'hi' }, noticeRow()])
    const marker = out.filter(m => m.kind === 'event')

    expect(marker).toHaveLength(1)
    expect(marker[0]?.text).toContain('confabulation caught')
  })

  it('still renders the assistant reply itself', () => {
    // The catch is metadata ABOUT the reply — suppressing the reply would
    // lose real model output.
    const out = toTranscriptMessages([noticeRow()])
    const assistant = out.filter(m => m.role === 'assistant' && m.kind !== 'event')

    expect(assistant).toHaveLength(1)
    expect(assistant[0]?.text).toBe('All good here.')
  })

  it('orders the marker before the reply it describes', () => {
    const out = toTranscriptMessages([noticeRow()])

    expect(out[0]?.kind).toBe('event')
    expect(out[1]?.text).toBe('All good here.')
  })

  it('does not mutate the assistant text', () => {
    const out = toTranscriptMessages([noticeRow()])
    const assistant = out.find(m => m.role === 'assistant' && m.kind !== 'event')

    expect(assistant?.text).not.toContain('confabulation')
    expect(assistant?.text).not.toContain(NOTICE.request_id)
  })

  it('renders nothing extra for a clean assistant turn', () => {
    // Non-vacuity: the assertions above must be able to come out empty.
    const out = toTranscriptMessages([{ role: 'assistant', text: 'All good here.' }])

    expect(out.filter(m => m.kind === 'event')).toHaveLength(0)
    expect(out).toHaveLength(1)
  })
})

describe('confab notice gating on reload', () => {
  // `display_kind` is an open-ended string column any writer can populate.
  // Claiming a confirmed catch off that string alone puts a false accusation
  // on rows that never carried one (the sibling "False Notice" P1).

  it.each(['user', 'system'])('does not claim a catch on a reloaded %s row', role => {
    expect(events([noticeRow({ role })])).toHaveLength(0)
  })

  it('does not claim a catch when the metadata is missing', () => {
    expect(events([noticeRow({ display_metadata: undefined })])).toHaveLength(0)
  })

  it.each([
    ['unknown version', { confab_notice: { ...NOTICE, version: 99 } }],
    ['unknown kind', { confab_notice: { ...NOTICE, kind: 'totally_made_up' } }],
    ['empty request_id', { confab_notice: { ...NOTICE, request_id: '' } }],
    ['bad scope', { confab_notice: { ...NOTICE, scope: 'everything' } }],
    ['wrong key', { something_else: NOTICE }],
    ['primitive payload', { confab_notice: 'scaffold_confab_removed' }]
  ])('does not claim a catch when the metadata is invalid (%s)', (_label, display_metadata) => {
    expect(events([noticeRow({ display_metadata })])).toHaveLength(0)
  })

  it('still renders the reply when the notice metadata fails validation', () => {
    // Fail closed on the CLAIM, not on the content.
    const out = toTranscriptMessages([noticeRow({ display_metadata: { confab_notice: { version: 99 } } })])

    expect(out.filter(m => m.kind === 'event')).toHaveLength(0)
    expect(out.find(m => m.role === 'assistant')?.text).toBe('All good here.')
  })

  it('accepts display_metadata served as raw JSON text by an older backend', () => {
    const row = noticeRow({ display_metadata: JSON.stringify({ confab_notice: NOTICE }) })

    expect(events([row])).toHaveLength(1)
  })

  it('does not claim a catch on unparseable JSON metadata', () => {
    expect(events([noticeRow({ display_metadata: '{not json' })])).toHaveLength(0)
  })
})
