// Startup render cache, renderer half: the cached session list paints only
// into an empty sidebar, only for the scope this boot actually resolved, and is
// written back only while the window stays on that scope (#94828 class: one
// profile's rows must never paint against another profile's backend).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import {
  paintCachedSessionList,
  reportCachedSessionPaint,
  type SessionListStore,
  settleCachedSessionPaint,
  writeThroughSessionList
} from './render-cache-hydration'

const A = { connectionId: 'local', profile: 'alpha' }
const B = { connectionId: 'local', profile: 'beta' }

function row(id: string, profile: string, extra: Partial<SessionInfo> = {}): SessionInfo {
  return { id, profile, title: id, ...extra } as SessionInfo
}

function memoryStore(initial: SessionInfo[] = []) {
  let sessions = initial
  let loading = true

  const store: SessionListStore = {
    getSessions: () => sessions,
    setSessions: rows => {
      sessions = rows
    },
    setSessionsLoading: value => {
      loading = value
    }
  }

  return { store, sessions: () => sessions, loading: () => loading }
}

// Stand-in for Electron's scoped cache: one entry per window scope, read back
// for the window's CURRENT scope only (what main's renderCacheScopeFor does).
function installBridge(entries: Record<string, unknown>, windowScope: () => string) {
  const calls = {
    put: [] as unknown[],
    clear: 0,
    drop: [] as string[],
    migrate: [] as [string, string][],
    divergence: [] as number[]
  }

  const renderCache = {
    read: vi.fn(async () => ({ enabled: true, sessions: entries[windowScope()] ?? null })),
    putSessions: (data: unknown) => void calls.put.push(data),
    clear: () => void (calls.clear += 1),
    dropProfile: (profile: string) => void calls.drop.push(profile),
    migrateProfile: (from: string, to: string) => void calls.migrate.push([from, to]),
    reportDivergence: (rows: number) => void calls.divergence.push(rows)
  }

  ;(window as unknown as { hermesDesktop: Record<string, unknown> }).hermesDesktop = {
    ...(window as unknown as { hermesDesktop?: Record<string, unknown> }).hermesDesktop,
    renderCache
  }

  return { calls, renderCache }
}

afterEach(() => {
  vi.useRealTimers()
  delete (window as unknown as { hermesDesktop?: { renderCache?: unknown } }).hermesDesktop?.renderCache
})

describe('paintCachedSessionList', () => {
  it('paints the cached list into an empty sidebar and drops the skeletons', async () => {
    installBridge({ alpha: { resolved: A, sessions: [row('a1', 'alpha'), row('a2', 'alpha')] } }, () => 'alpha')
    const { store, sessions, loading } = memoryStore()

    const paint = await paintCachedSessionList(store)

    expect(sessions().map(s => s.id)).toEqual(['a1', 'a2'])
    expect(loading()).toBe(false)
    expect(paint?.resolved).toEqual(A)
  })

  it('never clobbers live rows, including rows that land during the read', async () => {
    installBridge({ alpha: { resolved: A, sessions: [row('cached', 'alpha')] } }, () => 'alpha')
    const live = memoryStore([row('live', 'alpha')])
    expect(await paintCachedSessionList(live.store)).toBeNull()
    expect(live.sessions().map(s => s.id)).toEqual(['live'])

    const racing = memoryStore()

    const read = async () => {
      racing.store.setSessions([row('landed', 'alpha')])

      return { resolved: A, sessions: [row('cached', 'alpha')] }
    }

    expect(await paintCachedSessionList(racing.store, read)).toBeNull()
    expect(racing.sessions().map(s => s.id)).toEqual(['landed'])
  })

  it("profile B never paints profile A's cached list", async () => {
    // Only profile A has an entry; the window is on profile B.
    const { renderCache } = installBridge({ alpha: { resolved: A, sessions: [row('a1', 'alpha')] } }, () => 'beta')

    const b = memoryStore()
    expect(await paintCachedSessionList(b.store)).toBeNull()
    expect(b.sessions()).toEqual([])
    expect(renderCache.read).toHaveBeenCalledTimes(1)
  })

  it("drops rows tagged with another profile or connection even inside the window's entry", async () => {
    installBridge(
      {
        beta: {
          resolved: B,
          sessions: [
            row('a-leak', 'alpha'),
            row('b1', 'beta'),
            row('b-remote', 'beta', { connection_id: 'conn-remote' }),
            row('b-local', 'beta', { connection_id: 'local' })
          ]
        }
      },
      () => 'beta'
    )

    const b = memoryStore()
    await paintCachedSessionList(b.store)

    expect(b.sessions().map(s => s.id)).toEqual(['b1', 'b-local'])
  })
})

describe('settleCachedSessionPaint', () => {
  it('retracts a paint whose rows were resolved for profile A when this boot resolved profile B', async () => {
    // The window's pre-dial route named an entry whose rows were resolved
    // against A (e.g. the default changed while the app was closed).
    installBridge({ launch: { resolved: A, sessions: [row('a1', 'alpha')] } }, () => 'launch')
    const b = memoryStore()
    const paint = await paintCachedSessionList(b.store)
    expect(b.sessions().map(s => s.id)).toEqual(['a1'])

    expect(settleCachedSessionPaint(paint, B, b.store)).toBe(false)
    expect(b.sessions()).toEqual([])
    expect(b.loading()).toBe(true)
  })

  it('retracts when the boot resolved no scope (legacy id-less remote)', async () => {
    installBridge({ launch: { resolved: A, sessions: [row('a1', 'alpha')] } }, () => 'launch')
    const m = memoryStore()
    const paint = await paintCachedSessionList(m.store)

    expect(settleCachedSessionPaint(paint, null, m.store)).toBe(false)
    expect(m.sessions()).toEqual([])
  })

  it('keeps a same-scope paint, and never wipes rows the live list already replaced', async () => {
    installBridge({ alpha: { resolved: A, sessions: [row('a1', 'alpha')] } }, () => 'alpha')
    const m = memoryStore()
    const paint = await paintCachedSessionList(m.store)

    expect(settleCachedSessionPaint(paint, { connectionId: 'local', profile: 'alpha' }, m.store)).toBe(true)
    expect(m.sessions().map(s => s.id)).toEqual(['a1'])

    m.store.setSessions([row('live', 'beta')])
    expect(settleCachedSessionPaint(paint, B, m.store)).toBe(false)
    expect(m.sessions().map(s => s.id)).toEqual(['live'])
  })
})

describe('reportCachedSessionPaint', () => {
  it('reports how many rows the paint differed from the first live list', async () => {
    const { calls } = installBridge(
      { alpha: { resolved: A, sessions: [row('a1', 'alpha'), row('gone', 'alpha')] } },
      () => 'alpha'
    )

    const m = memoryStore()
    const paint = await paintCachedSessionList(m.store)

    reportCachedSessionPaint(paint, [row('a1', 'alpha'), row('new', 'alpha')])
    reportCachedSessionPaint(null, [])

    expect(calls.divergence).toEqual([2])
  })
})

describe('writeThroughSessionList', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('persists the latest list, coalesced, only while the active scope is the resolved one', () => {
    let active: null | typeof A = A
    let listener: (rows: readonly SessionInfo[]) => void = () => undefined
    const persisted: (readonly SessionInfo[])[] = []

    const stop = writeThroughSessionList({
      resolved: A,
      activeScope: () => active,
      subscribe: next => {
        listener = next

        return () => undefined
      },
      delayMs: 1_000,
      persist: (_scope, rows) => void persisted.push(rows)
    })

    listener([row('a1', 'alpha')])
    listener([row('a1', 'alpha'), row('a2', 'alpha')])
    vi.advanceTimersByTime(1_000)
    expect(persisted.map(rows => rows.map(s => s.id))).toEqual([['a1', 'a2']])

    // Live profile swap to B: B's rows are never filed under A's scope.
    active = B
    listener([row('b1', 'beta')])
    vi.advanceTimersByTime(1_000)
    expect(persisted).toHaveLength(1)

    active = A
    listener([row('a3', 'alpha')])
    stop()
    vi.advanceTimersByTime(1_000)
    expect(persisted).toHaveLength(1)
  })

  it('the default persist filters rows to the resolved scope', () => {
    const { calls } = installBridge({}, () => 'alpha')
    let listener: (rows: readonly SessionInfo[]) => void = () => undefined

    writeThroughSessionList({
      resolved: A,
      activeScope: () => A,
      subscribe: next => {
        listener = next

        return () => undefined
      }
    })

    listener([row('a1', 'alpha'), row('b1', 'beta')])
    vi.advanceTimersByTime(1_000)

    expect(calls.put).toEqual([{ resolved: A, sessions: [row('a1', 'alpha')] }])
  })
})

describe('profile-keyed family wiring', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('profile rename migrates and profile delete drops the cached list', async () => {
    const { calls } = installBridge({}, () => 'alpha')
    const states = await import('@/store/session-states')

    states.migrateTilesForProfile('alpha', 'gamma')
    states.dropTilesForProfile('beta')

    expect(calls.migrate).toEqual([['alpha', 'gamma']])
    expect(calls.drop).toEqual(['beta'])
  })
})
