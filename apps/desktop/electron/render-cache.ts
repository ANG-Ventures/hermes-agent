/**
 * Startup render cache: the last-known session list for a window's backend
 * scope, persisted by the main process so a cold launch can paint the sidebar
 * before the backend answers, then reconcile against the live list.
 *
 * Scope (apps/desktop/AGENTS.md: persisted state declares its scope in its
 * key): every entry belongs to one {connectionId, profile} scope, the same
 * shape the durable transcript-tail cache uses (#94828). Stored session ids
 * are only unique within one profile's state.db, so an entry keyed by gateway
 * URL alone could paint profile A's list against profile B's backend. The
 * scope is part of the file identity AND is re-checked from the envelope on
 * read, so a read for B can never return A's entry.
 *
 * Invariants:
 *  - fail-open: missing/corrupt/mismatched files read as null; nothing here
 *    throws out to a caller.
 *  - same trust domain as state.db: files are 0600 in a 0700 dir.
 *  - debounced (>=5s) atomic writes (tmp + rename); `flush()` is synchronous
 *    so quit can persist the last window before the backend shutdown runs.
 *  - bounded: at most MAX_SESSION_ROWS rows per scope.
 *
 * No Electron imports: main.ts supplies the directory and app version.
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

export const RENDER_CACHE_SCHEMA = 2
export const DEFAULT_DEBOUNCE_MS = 5_000
export const MAX_SESSION_ROWS = 500

const FILE_PREFIX = 'sessions-v2-'

export interface RenderCacheScope {
  connectionId: string
  profile: string
}

/** What the renderer persists for a scope: the rows plus the backend scope
 *  they were resolved against, which the renderer re-validates after boot. */
export interface CachedSessionList {
  resolved: RenderCacheScope
  sessions: unknown[]
}

interface Envelope {
  schema: number
  appVersion: string
  scope: RenderCacheScope
  savedAt: string
  data: CachedSessionList
}

export interface RenderCacheOptions {
  dir: string
  appVersion: string
  debounceMs?: number
  now?: () => number
  log?: (line: string) => void
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** Canonical scope: trimmed; an empty profile is the 'default' profile. */
export function normalizeRenderCacheScope(scope: unknown): RenderCacheScope | null {
  if (!scope || typeof scope !== 'object') {
    return null
  }

  const record = scope as Record<string, unknown>
  const connectionId = clean(record.connectionId)

  if (!connectionId) {
    return null
  }

  return { connectionId, profile: clean(record.profile) || 'default' }
}

export function sameRenderCacheScope(a: unknown, b: unknown): boolean {
  const left = normalizeRenderCacheScope(a)
  const right = normalizeRenderCacheScope(b)

  return Boolean(left && right && left.connectionId === right.connectionId && left.profile === right.profile)
}

function fileForScope(scope: RenderCacheScope): string {
  const digest = crypto
    .createHash('sha256')
    .update(JSON.stringify([scope.connectionId, scope.profile]))
    .digest('hex')
    .slice(0, 32)

  return `${FILE_PREFIX}${digest}.json`
}

function normalizeList(data: unknown): CachedSessionList | null {
  if (!data || typeof data !== 'object') {
    return null
  }

  const record = data as Record<string, unknown>
  const resolved = normalizeRenderCacheScope(record.resolved)

  if (!resolved || !Array.isArray(record.sessions)) {
    return null
  }

  return { resolved, sessions: record.sessions.slice(0, MAX_SESSION_ROWS) }
}

export class RenderCache {
  private readonly dir: string
  private readonly appVersion: string
  private readonly debounceMs: number
  private readonly now: () => number
  private readonly log: (line: string) => void

  private pending = new Map<string, { scope: RenderCacheScope; data: CachedSessionList }>()
  private timer: ReturnType<typeof setTimeout> | null = null
  private legacySwept = false

  constructor(opts: RenderCacheOptions) {
    this.dir = opts.dir
    this.appVersion = String(opts.appVersion || '')
    this.debounceMs = opts.debounceMs ?? DEFAULT_DEBOUNCE_MS
    this.now = opts.now ?? Date.now
    this.log = opts.log ?? (() => undefined)
  }

  /** Queue a scope's session list for a debounced write. */
  putSessions(scope: unknown, data: unknown): void {
    const key = normalizeRenderCacheScope(scope)
    const list = normalizeList(data)

    if (!key || !list) {
      return
    }

    this.pending.set(fileForScope(key), { scope: key, data: list })

    if (this.timer) {
      return
    }

    this.timer = setTimeout(() => {
      this.timer = null
      this.flush()
    }, this.debounceMs)
    this.timer.unref?.()
  }

  /** The scope's cached list, or null. Pending writes win over disk. */
  readSessions(scope: unknown): CachedSessionList | null {
    const key = normalizeRenderCacheScope(scope)

    if (!key) {
      return null
    }

    this.sweepLegacy()
    const file = fileForScope(key)
    const queued = this.pending.get(file)

    if (queued) {
      return queued.data
    }

    const envelope = this.readEnvelope(file)

    // The envelope's own scope is authoritative: a hash collision or a copied
    // file must still never answer for another scope.
    return envelope && sameRenderCacheScope(envelope.scope, key) ? envelope.data : null
  }

  /** Write all pending entries now. Synchronous by design (quit path). */
  flush(): void {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }

    const entries = [...this.pending.entries()]
    this.pending.clear()

    for (const [file, entry] of entries) {
      this.writeEnvelope(file, entry.scope, entry.data)
    }
  }

  /** Wipe every scope (connection/mode re-home). */
  clear(): void {
    this.flushTimerOnly()
    this.pending.clear()

    for (const file of this.listFiles()) {
      this.remove(file)
    }
  }

  /** Drop every entry owned by a deleted profile, on any connection. Over-
   *  dropping only costs one cold paint; under-dropping would let a later
   *  profile of the same name paint the deleted one's list. */
  dropProfile(profile: string): void {
    const name = clean(profile) || 'default'

    const owned = (scope: RenderCacheScope, data: CachedSessionList) =>
      scope.profile === name || data.resolved.profile === name

    for (const [file, entry] of [...this.pending.entries()]) {
      if (owned(entry.scope, entry.data)) {
        this.pending.delete(file)
      }
    }

    for (const file of this.listFiles()) {
      const envelope = this.readEnvelope(file)

      if (!envelope || owned(envelope.scope, envelope.data)) {
        this.remove(file)
      }
    }
  }

  /** Re-key local-connection entries owned by a renamed profile (the
   *  sessions still exist under the new name). A same-named profile on a
   *  remote connection was not renamed and is left alone. */
  migrateProfile(oldProfile: string, newProfile: string): void {
    const from = clean(oldProfile) || 'default'
    const to = clean(newProfile) || 'default'

    if (from === to) {
      return
    }

    this.flush()

    for (const file of this.listFiles()) {
      const envelope = this.readEnvelope(file)

      if (!envelope || envelope.scope.connectionId !== 'local') {
        continue
      }

      const scopeMoves = envelope.scope.profile === from
      const resolvedMoves = envelope.data.resolved.connectionId === 'local' && envelope.data.resolved.profile === from

      if (!scopeMoves && !resolvedMoves) {
        continue
      }

      const scope = scopeMoves ? { ...envelope.scope, profile: to } : envelope.scope
      const resolved = resolvedMoves ? { ...envelope.data.resolved, profile: to } : envelope.data.resolved

      // Rows carry their owning profile; re-tag them so the renderer's
      // per-row profile filter keeps them under the new name.
      const sessions = envelope.data.sessions.map(row =>
        row && typeof row === 'object' && clean((row as Record<string, unknown>).profile) === from
          ? { ...(row as Record<string, unknown>), profile: to }
          : row
      )

      this.remove(file)
      this.writeEnvelope(fileForScope(scope), scope, { resolved, sessions })
    }
  }

  private flushTimerOnly(): void {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private readEnvelope(file: string): Envelope | null {
    try {
      const parsed = JSON.parse(fs.readFileSync(path.join(this.dir, file), 'utf8')) as Envelope
      const scope = normalizeRenderCacheScope(parsed?.scope)
      const data = normalizeList(parsed?.data)

      if (!parsed || parsed.schema !== RENDER_CACHE_SCHEMA || !scope || !data) {
        return null
      }

      return { ...parsed, scope, data }
    } catch {
      return null
    }
  }

  private writeEnvelope(file: string, scope: RenderCacheScope, data: CachedSessionList): void {
    try {
      fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 })

      const envelope: Envelope = {
        schema: RENDER_CACHE_SCHEMA,
        appVersion: this.appVersion,
        scope,
        savedAt: new Date(this.now()).toISOString(),
        data
      }

      const target = path.join(this.dir, file)
      const tmp = `${target}.tmp-${process.pid}`
      fs.writeFileSync(tmp, JSON.stringify(envelope), { mode: 0o600 })
      fs.renameSync(tmp, target)
      fs.chmodSync(target, 0o600)
    } catch (error) {
      this.log(`[render-cache] write failed for ${file}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  private listFiles(): string[] {
    try {
      return fs.readdirSync(this.dir).filter(name => name.startsWith(FILE_PREFIX) && name.endsWith('.json'))
    } catch {
      return []
    }
  }

  private remove(file: string): void {
    try {
      fs.rmSync(path.join(this.dir, file), { force: true })
    } catch {
      // best effort
    }
  }

  /** Files from the unscoped v1 cache (gatewayUrl-keyed sessions/status and
   *  per-session transcripts) can never be attributed to a profile: remove
   *  them once so they are never read again. */
  private sweepLegacy(): void {
    if (this.legacySwept) {
      return
    }

    this.legacySwept = true

    try {
      for (const name of fs.readdirSync(this.dir)) {
        if (name === 'sessions.json' || name === 'status.json' || /^transcript-.*\.json$/.test(name)) {
          this.remove(name)
        }
      }
    } catch {
      // no dir yet
    }
  }
}
