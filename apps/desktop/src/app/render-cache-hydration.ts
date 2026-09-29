import {
  persistSessionList,
  readCachedSessionList,
  type RenderCacheScope,
  reportRenderCacheDivergence,
  sameRenderCacheScope,
  sessionListDivergence
} from '@/store/render-cache'
import type { SessionInfo } from '@/types/hermes'

// Boot-time use of the startup render cache (store/render-cache.ts): paint the
// cached list into an EMPTY sidebar before the backend answers, validate it
// against the scope this boot actually resolved, and write the live list back
// while the window stays on that scope. The live refresh merges over a kept
// paint exactly as it merges over any earlier page.

export interface SessionListStore {
  getSessions: () => SessionInfo[]
  setSessions: (rows: SessionInfo[]) => void
  setSessionsLoading: (loading: boolean) => void
}

export interface CachedSessionPaint {
  /** Scope the cached rows were resolved against when they were written. */
  resolved: RenderCacheScope
  /** The exact array painted (identity marks "still the cached paint"). */
  rows: SessionInfo[]
}

/** Paint the cached list, only into an empty store. Never clobbers live rows. */
export async function paintCachedSessionList(
  store: SessionListStore,
  read = readCachedSessionList
): Promise<CachedSessionPaint | null> {
  if (store.getSessions().length > 0) {
    return null
  }

  const cached = await read()

  // Re-check after the await: a live page may have landed meanwhile.
  if (!cached || cached.sessions.length === 0 || store.getSessions().length > 0) {
    return null
  }

  store.setSessions(cached.sessions)
  store.setSessionsLoading(false)

  return { resolved: cached.resolved, rows: cached.sessions }
}

/**
 * Validate a paint against the scope this boot resolved. A mismatch (the
 * window resolved another profile or connection than the rows were written
 * for, or no scope at all) retracts the paint back to the loading state, so
 * one scope's rows never stand in for another's. Returns whether it was kept.
 */
export function settleCachedSessionPaint(
  paint: CachedSessionPaint | null,
  resolved: null | RenderCacheScope,
  store: SessionListStore
): boolean {
  if (!paint) {
    return true
  }

  if (sameRenderCacheScope(paint.resolved, resolved)) {
    return true
  }

  if (store.getSessions() === paint.rows) {
    store.setSessions([])
    store.setSessionsLoading(true)
  }

  return false
}

/** Log how far the paint was from the first live list (desktop.log). */
export function reportCachedSessionPaint(paint: CachedSessionPaint | null, live: readonly SessionInfo[]): void {
  if (paint) {
    reportRenderCacheDivergence(sessionListDivergence(paint.rows, live))
  }
}

/**
 * Persist the live list (trailing, coalesced) while the window's active scope
 * is still the one this boot resolved; a live profile swap or connection
 * change stops writes rather than filing another scope's rows under this one.
 */
export function writeThroughSessionList(opts: {
  resolved: RenderCacheScope
  activeScope: () => null | RenderCacheScope
  subscribe: (listener: (rows: readonly SessionInfo[]) => void) => () => void
  delayMs?: number
  persist?: (resolved: RenderCacheScope, rows: readonly SessionInfo[]) => void
}): () => void {
  const persist = opts.persist ?? persistSessionList
  let timer: null | ReturnType<typeof setTimeout> = null
  let latest: readonly SessionInfo[] = []

  const off = opts.subscribe(rows => {
    latest = rows

    if (timer) {
      return
    }

    timer = setTimeout(() => {
      timer = null

      if (sameRenderCacheScope(opts.activeScope(), opts.resolved)) {
        persist(opts.resolved, latest)
      }
    }, opts.delayMs ?? 1_000)
  })

  return () => {
    off()

    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }
}
