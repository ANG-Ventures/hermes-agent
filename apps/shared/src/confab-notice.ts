/**
 * Reader-side gate for the out-of-band `hermes_confab_notice` record.
 *
 * The Python half (`agent/confab_notice.py`) validates the provider payload on
 * the wire and stamps the surviving assistant row with
 * `display_kind = 'confab_notice'` plus `display_metadata.confab_notice`.
 * Every client that renders reloaded history has to answer the same question
 * — "did this turn really carry a confirmed catch?" — so the answer lives here
 * once instead of being re-derived (and re-broken) per surface.
 *
 * `display_kind` alone is NOT sufficient. It is an open-ended string column
 * that an importer, a migration, or a malformed record can populate on any
 * row. Presenting the confirmed-confabulation claim off that string would put
 * a false accusation on a user or system turn. So a row must clear two gates:
 *
 * 1. `role === 'assistant'` — only a model reply can carry a catch;
 * 2. `display_metadata.confab_notice` re-validates against the same
 *    fail-closed v1 schema the wire payload had to pass.
 *
 * Mirrors `notice_from_display_row` in `agent/confab_notice.py`.
 */

/** `display_kind` stamped on an assistant row carrying a notice. */
export const CONFAB_NOTICE_DISPLAY_KIND = 'confab_notice'

/** Key under which the validated notice sits inside `display_metadata`. */
export const CONFAB_NOTICE_KEY = 'confab_notice'

/** The only schema version this consumer understands. */
export const CONFAB_NOTICE_VERSION = 1

/** The only catch kind defined by v1 of the contract. */
export const CONFAB_NOTICE_KIND = 'scaffold_confab_removed'

/** Allowed `scope` values. */
export const CONFAB_NOTICE_SCOPES = ['visible', 'intermediate', 'both'] as const

/** Event-line label. Out of band, but never invisible. */
export const CONFAB_NOTICE_EVENT_TEXT = 'confabulation caught — scaffold text removed'

// Defensive bound — `request_id` and `grammar` are short opaque labels.
const MAX_LABEL_LEN = 256

export interface ConfabNotice {
  grammar: null | string
  kind: string
  request_id: string
  scope: string
  version: number
}

/** A history row as any of the surfaces model it. */
export interface ConfabNoticeRow {
  display_kind?: string
  display_metadata?: unknown
  role?: string
}

function isShortLabel(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= MAX_LABEL_LEN
}

/**
 * Validate a raw notice payload. Returns the notice, or `null` on ANY
 * deviation from the v1 contract — same fail-closed posture as the producer
 * gate in Python. Never throws.
 */
export function validateConfabNotice(raw: unknown): ConfabNotice | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null
  }

  const candidate = raw as Record<string, unknown>

  if (candidate.version !== CONFAB_NOTICE_VERSION) {
    return null
  }

  if (candidate.kind !== CONFAB_NOTICE_KIND) {
    return null
  }

  if (!isShortLabel(candidate.request_id)) {
    return null
  }

  if (typeof candidate.scope !== 'string' || !CONFAB_NOTICE_SCOPES.includes(candidate.scope as never)) {
    return null
  }

  // `grammar` is the detector's bounded label, or null when several catches
  // cannot be represented by one label. Absent is treated as null.
  const grammar = candidate.grammar

  if (grammar !== undefined && grammar !== null && !isShortLabel(grammar)) {
    return null
  }

  return {
    grammar: grammar === undefined || grammar === null ? null : grammar,
    kind: CONFAB_NOTICE_KIND,
    request_id: candidate.request_id,
    scope: candidate.scope,
    version: CONFAB_NOTICE_VERSION
  }
}

/**
 * Return the validated notice a persisted row genuinely carries, else `null`.
 *
 * A remote backend older than the client can serve `display_metadata` as raw
 * JSON text, so parse a string form before reading into it.
 */
export function confabNoticeFromRow(row: ConfabNoticeRow | null | undefined): ConfabNotice | null {
  if (!row || row.role !== 'assistant' || row.display_kind !== CONFAB_NOTICE_DISPLAY_KIND) {
    return null
  }

  let metadata: unknown = row.display_metadata

  if (typeof metadata === 'string') {
    try {
      metadata = JSON.parse(metadata)
    } catch {
      return null
    }
  }

  if (!metadata || typeof metadata !== 'object') {
    return null
  }

  return validateConfabNotice((metadata as Record<string, unknown>)[CONFAB_NOTICE_KEY])
}
