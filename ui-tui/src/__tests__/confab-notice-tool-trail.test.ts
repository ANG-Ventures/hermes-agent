import { describe, expect, it } from 'vitest'

import { toTranscriptMessages } from '../domain/messages.js'

/**
 * Regression: a confab-notice turn must keep the tool trail of the reply it
 * annotates.
 *
 * `toTranscriptMessages` accumulates `role: 'tool'` rows into `pending` and
 * attaches them to the NEXT assistant row. The four display-event branches
 * above the confab branch each `continue` — they REPLACE the row, so clearing
 * `pending` there is correct. The confab branch deliberately falls through
 * (the marker is additional, the reply is still rendered), so it must NOT
 * clear `pending`: the very next statement reads it for `tools`.
 *
 * A confabulation catch is precisely the turn an operator reloads to audit,
 * and the tool calls are the evidence of what the model actually did — losing
 * them silently on exactly the flagged turns defeats the durable-triage
 * purpose of the feature.
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

const toolRow = (name: string, context: string) => ({ context, name, role: 'tool' })

const assistantOf = (rows: unknown[]) =>
  toTranscriptMessages(rows).find(m => m.role === 'assistant' && m.kind !== 'event')

describe('confab notice preserves the tool trail', () => {
  it('keeps the tools of the annotated reply', () => {
    const assistant = assistantOf([toolRow('terminal', 'ls -la'), noticeRow()])

    expect(assistant?.tools).toBeDefined()
    expect(assistant?.tools).toHaveLength(1)
    expect(assistant?.tools?.[0]).toContain('ls -la')
  })

  it('keeps every tool of a multi-tool flagged turn', () => {
    const assistant = assistantOf([toolRow('terminal', 'ls -la'), toolRow('read_file', 'notes.md'), noticeRow()])

    expect(assistant?.tools).toHaveLength(2)
  })

  it('still emits the marker alongside the preserved trail', () => {
    const out = toTranscriptMessages([toolRow('terminal', 'ls -la'), noticeRow()])

    expect(out.filter(m => m.kind === 'event')).toHaveLength(1)
    expect(out.find(m => m.role === 'assistant' && m.kind !== 'event')?.tools).toHaveLength(1)
  })

  it('matches the trail a clean assistant turn would get', () => {
    // Non-vacuity + parity: the notice tag must not change the tool trail at
    // all, so the flagged row carries exactly what an untagged one does.
    const clean = assistantOf([toolRow('terminal', 'ls -la'), { role: 'assistant', text: 'All good here.' }])
    const flagged = assistantOf([toolRow('terminal', 'ls -la'), noticeRow()])

    expect(flagged?.tools).toEqual(clean?.tools)
  })

  it('does not leak the trail past the flagged reply', () => {
    // `pending` must still be consumed (not left dangling) by the fall-through
    // row, or the NEXT assistant turn would inherit tools it never made.
    const out = toTranscriptMessages([
      toolRow('terminal', 'ls -la'),
      noticeRow(),
      { role: 'assistant', text: 'Second reply.' }
    ])

    const assistants = out.filter(m => m.role === 'assistant' && m.kind !== 'event')

    expect(assistants).toHaveLength(2)
    expect(assistants[1]?.tools).toBeUndefined()
  })

  it('does not attach a trail to a rejected (non-assistant) notice row', () => {
    // The gate still fails closed: a user row tagged confab_notice gets no
    // marker, and `pending` is cleared by the user branch as before.
    const out = toTranscriptMessages([
      toolRow('terminal', 'ls -la'),
      noticeRow({ role: 'user' }),
      { role: 'assistant', text: 'Second reply.' }
    ])

    expect(out.filter(m => m.kind === 'event')).toHaveLength(0)
    expect(out.find(m => m.role === 'assistant')?.tools).toBeUndefined()
  })
})
