import { confabNoticeFromRow, validateConfabNotice } from '@hermes/shared/confab-notice'
import { describe, expect, it } from 'vitest'

/**
 * The SHARED reader-side gate for the out-of-band confab notice
 * (`apps/shared/src/confab-notice.ts`).
 *
 * It lives in shared so the TUI and the desktop cannot drift on the question
 * "did this turn really carry a confirmed catch?", and it mirrors
 * `notice_from_display_row` / `validate_confab_notice` in
 * `agent/confab_notice.py` with the same fail-closed posture.
 *
 * The suite lives HERE rather than beside the module because `apps/shared`
 * has no test runner of its own — verified by running vitest against a spec
 * in that directory from both the desktop and TUI projects: 0 tests
 * collected. A spec placed there would never execute.
 */

const VALID = {
  grammar: 'inbound',
  kind: 'scaffold_confab_removed',
  request_id: '3b264082',
  scope: 'visible',
  version: 1
}

describe('validateConfabNotice', () => {
  it('round-trips a valid v1 notice', () => {
    expect(validateConfabNotice({ ...VALID })).toEqual(VALID)
  })

  it.each(['visible', 'intermediate', 'both'])('accepts the contract scope %s', scope => {
    expect(validateConfabNotice({ ...VALID, scope })?.scope).toBe(scope)
  })

  it('treats an absent grammar as null', () => {
    const { grammar: _dropped, ...withoutGrammar } = VALID

    expect(validateConfabNotice(withoutGrammar)?.grammar).toBeNull()
  })

  it('allows an explicit null grammar (several catches, no single label)', () => {
    expect(validateConfabNotice({ ...VALID, grammar: null })?.grammar).toBeNull()
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'scaffold_confab_removed'],
    ['a number', 123],
    ['an array', []],
    ['an empty object', {}],
    ['version 2', { ...VALID, version: 2 }],
    ['a string version', { ...VALID, version: '1' }],
    ['an unknown kind', { ...VALID, kind: 'totally_made_up' }],
    ['a null kind', { ...VALID, kind: null }],
    ['an empty request_id', { ...VALID, request_id: '' }],
    ['a blank request_id', { ...VALID, request_id: '   ' }],
    ['a numeric request_id', { ...VALID, request_id: 3264082 }],
    ['an over-long request_id', { ...VALID, request_id: 'x'.repeat(257) }],
    ['an invalid scope', { ...VALID, scope: 'everything' }],
    ['a null scope', { ...VALID, scope: null }],
    ['an empty grammar', { ...VALID, grammar: '' }],
    ['a numeric grammar', { ...VALID, grammar: 7 }],
    ['an over-long grammar', { ...VALID, grammar: 'g'.repeat(257) }]
  ])('fails closed on %s', (_label, raw) => {
    expect(validateConfabNotice(raw)).toBeNull()
  })

  it('does not carry provider-supplied extra fields through', () => {
    // A provider must not be able to smuggle keys into display metadata.
    const out = validateConfabNotice({ ...VALID, content: 'secret', evil: { b: 1 } })

    expect(Object.keys(out ?? {}).sort()).toEqual(['grammar', 'kind', 'request_id', 'scope', 'version'])
  })
})

describe('confabNoticeFromRow', () => {
  const row = (over: Record<string, unknown> = {}) => ({
    display_kind: 'confab_notice',
    display_metadata: { confab_notice: { ...VALID } },
    role: 'assistant',
    ...over
  })

  it('accepts a genuine assistant notice row', () => {
    expect(confabNoticeFromRow(row())).toEqual(VALID)
  })

  it.each(['user', 'system', 'tool', '', undefined])('rejects a %s row', role => {
    // display_kind is an open string column — role is the first gate.
    expect(confabNoticeFromRow(row({ role }))).toBeNull()
  })

  it.each(['model_switch', 'auto_continue', 'hidden', '', undefined])('rejects display_kind %s', display_kind => {
    expect(confabNoticeFromRow(row({ display_kind }))).toBeNull()
  })

  it('rejects a row whose metadata does not re-validate', () => {
    expect(confabNoticeFromRow(row({ display_metadata: { confab_notice: { version: 99 } } }))).toBeNull()
  })

  it('rejects a row with no metadata at all', () => {
    expect(confabNoticeFromRow(row({ display_metadata: undefined }))).toBeNull()
    expect(confabNoticeFromRow(row({ display_metadata: {} }))).toBeNull()
  })

  it('parses metadata served as raw JSON text by an older backend', () => {
    expect(confabNoticeFromRow(row({ display_metadata: JSON.stringify({ confab_notice: VALID }) }))).toEqual(VALID)
  })

  it('rejects unparseable JSON metadata instead of throwing', () => {
    expect(confabNoticeFromRow(row({ display_metadata: '{not json' }))).toBeNull()
  })

  it('rejects a null or undefined row', () => {
    expect(confabNoticeFromRow(null)).toBeNull()
    expect(confabNoticeFromRow(undefined)).toBeNull()
  })

  it('returns the notice so callers can key off kind', () => {
    // Future notice kinds must be distinguishable, not all labelled alike.
    expect(confabNoticeFromRow(row())?.kind).toBe('scaffold_confab_removed')
    expect(confabNoticeFromRow(row())?.request_id).toBe('3b264082')
  })
})
