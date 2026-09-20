import { CONFAB_NOTICE_EVENT_TEXT, confabNoticeFromRow } from '@hermes/shared/confab-notice'

import { LONG_MSG } from '../config/limits.js'
import { buildToolTrailLine } from '../lib/text.js'
import type { Msg, SessionInfo } from '../types.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return `${prefix || '(message)'} [long message]`
}

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const out: Msg[] = []
  let pending: string[] = []

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const { context, display_kind, name, role, text, timestamp } = row as TranscriptRow

    const createdAt =
      typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : undefined

    if (role === 'tool') {
      pending.push(buildToolTrailLine(name ?? 'tool', context ?? ''))

      continue
    }

    if (typeof text !== 'string' || !text.trim()) {
      continue
    }

    // Display-only timeline events: render as dim ◈ markers instead of
    // opaque user messages. Hidden compaction handoffs are skipped entirely.
    if (display_kind === 'hidden') {
      continue
    }

    if (display_kind === 'model_switch') {
      out.push({ kind: 'event', role: 'system', text: 'model changed' })
      pending = []

      continue
    }

    if (display_kind === 'auto_continue') {
      out.push({ kind: 'event', role: 'system', text: 'resumed interrupted turn' })
      pending = []

      continue
    }

    if (display_kind === 'personality_switch') {
      out.push({ kind: 'event', role: 'system', text: 'personality changed' })
      pending = []

      continue
    }

    if (display_kind === 'async_delegation_complete') {
      const meta = (row as TranscriptRow).display_metadata
      const count = meta && typeof meta.task_count === 'number' ? meta.task_count : undefined

      const label =
        count === undefined
          ? 'background agent work finished'
          : `${count} background agent${count === 1 ? '' : 's'} finished`

      out.push({ kind: 'event', role: 'system', text: label })
      pending = []

      continue
    }

    // A confirmed out-of-band confabulation catch. The row still carries the
    // real model reply, so emit the event marker AND fall through so the
    // reply is rendered normally — without this, a reloaded session is
    // indistinguishable from a clean one and the durable triage record is
    // invisible to the operator.
    //
    // Gated on role + re-validated metadata: display_kind is an open string
    // column, so a malformed or imported non-assistant row carrying it must
    // not be presented as a confirmed catch.
    //
    // Unlike the branches above, this one must NOT clear `pending`: they each
    // `continue` and replace the row, while this one falls through to the
    // assistant push below, which reads `pending` for the reply's tool trail.
    // Clearing it here would silently erase the tool evidence on exactly the
    // turns an operator reloads history to audit.
    if (display_kind === 'confab_notice' && confabNoticeFromRow(row as TranscriptRow)) {
      out.push({ kind: 'event', role: 'system', text: CONFAB_NOTICE_EVENT_TEXT })
    }

    if (role === 'assistant') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }), ...(pending.length && { tools: pending }) })
      pending = []
    } else if (role === 'user' || role === 'system') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }) })
      pending = []
    }
  }

  return out
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface TranscriptRow {
  context?: string
  display_kind?: string
  display_metadata?: { task_count?: number; [key: string]: unknown }
  name?: string
  role?: string
  text?: string
  timestamp?: number
}
