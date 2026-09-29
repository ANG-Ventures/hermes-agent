import type { SessionInfo } from '@/types/hermes'

// ── Startup render cache (session list) ─────────────────────────────────────
// A cold launch paints the last-known session list for this window's backend
// scope before the backend answers, then the live refresh merges over it.
// Electron persists the entries and resolves the scope from the window's own
// connection route, so a read can only return what was written for that
// {connectionId, profile} (#94828: an unscoped key painted profile A's rows
// against profile B's backend). The renderer adds the second half of the
// ladder: each entry records the backend scope its rows were RESOLVED against,
// and a paint whose scope disagrees with the connection this boot actually
// resolved is retracted before the live list lands.
//
// Profile-keyed family: gateway switch wipes it (clearRenderCache), profile
// delete drops it (dropTilesForProfile), profile rename migrates it
// (migrateTilesForProfile). Every call is fail-open.

export interface RenderCacheScope {
  connectionId: string
  profile: string
}

export interface CachedSessionList {
  resolved: RenderCacheScope
  sessions: SessionInfo[]
}

type RenderCacheBridge = NonNullable<Window['hermesDesktop']['renderCache']>

function bridge(): null | RenderCacheBridge {
  try {
    return window.hermesDesktop?.renderCache ?? null
  } catch {
    return null
  }
}

const profileKey = (profile: null | string | undefined) => (profile ?? '').trim() || 'default'

/** The scope a resolved connection/profile pair names, or null when there is
 *  no connection identity to scope by (a legacy id-less remote). */
export function renderCacheScope(
  connectionId: null | string | undefined,
  profile: null | string | undefined
): null | RenderCacheScope {
  const id = (connectionId ?? '').trim()

  return id ? { connectionId: id, profile: profileKey(profile) } : null
}

export function sameRenderCacheScope(a: null | RenderCacheScope, b: null | RenderCacheScope): boolean {
  return Boolean(a && b && a.connectionId === b.connectionId && profileKey(a.profile) === profileKey(b.profile))
}

/** Rows that provably belong to `scope`: the row's profile tag matches and it
 *  carries no conflicting connection. Everything else never reaches a paint. */
export function sessionRowsForScope(rows: readonly SessionInfo[], scope: RenderCacheScope): SessionInfo[] {
  return rows.filter(
    row =>
      profileKey(row.profile) === scope.profile &&
      (!row.connection_id?.trim() || row.connection_id.trim() === scope.connectionId)
  )
}

/** The cached list for this window's scope, or null. */
export async function readCachedSessionList(): Promise<CachedSessionList | null> {
  try {
    const result = await bridge()?.read()
    const cached = result?.enabled ? (result.sessions as CachedSessionList | null) : null
    const resolved = cached ? renderCacheScope(cached.resolved?.connectionId, cached.resolved?.profile) : null

    if (!cached || !resolved || !Array.isArray(cached.sessions)) {
      return null
    }

    return { resolved, sessions: sessionRowsForScope(cached.sessions, resolved) }
  } catch {
    return null
  }
}

/** Persist the live list for this window's scope (debounced in Electron). */
export function persistSessionList(resolved: RenderCacheScope, rows: readonly SessionInfo[]): void {
  try {
    bridge()?.putSessions({ resolved, sessions: sessionRowsForScope(rows, resolved) })
  } catch {
    // fail-open
  }
}

/** Wipe every scope (connection/mode re-home). */
export function clearRenderCache(): void {
  try {
    bridge()?.clear()
  } catch {
    // fail-open
  }
}

export function dropRenderCacheForProfile(profile: string): void {
  try {
    bridge()?.dropProfile(profileKey(profile))
  } catch {
    // fail-open
  }
}

export function migrateRenderCacheForProfile(oldProfile: string, newProfile: string): void {
  try {
    bridge()?.migrateProfile(profileKey(oldProfile), profileKey(newProfile))
  } catch {
    // fail-open
  }
}

/** Rows that differ between the cached paint and the first live list:
 *  cached rows gone live, title/archived drift, and live rows the cache
 *  did not know. 0 means the paint matched live exactly. */
export function sessionListDivergence(cached: readonly SessionInfo[], live: readonly SessionInfo[]): number {
  const identity = (row: SessionInfo) => `${profileKey(row.profile)}::${row.id}`
  const liveById = new Map(live.map(row => [identity(row), row]))
  let divergent = 0

  for (const row of cached) {
    const match = liveById.get(identity(row))

    if (!match) {
      divergent += 1

      continue
    }

    if ((row.title ?? '') !== (match.title ?? '') || Boolean(row.archived) !== Boolean(match.archived)) {
      divergent += 1
    }

    liveById.delete(identity(row))
  }

  return divergent + liveById.size
}

export function reportRenderCacheDivergence(rows: number): void {
  try {
    bridge()?.reportDivergence(rows)
  } catch {
    // fail-open
  }
}
