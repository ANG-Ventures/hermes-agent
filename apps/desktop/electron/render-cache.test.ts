import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test, vi } from 'vitest'

import { MAX_SESSION_ROWS, RenderCache, sameRenderCacheScope } from './render-cache'

const A = { connectionId: 'local', profile: 'alpha' }
const B = { connectionId: 'local', profile: 'beta' }
const REMOTE_A = { connectionId: 'conn-remote', profile: 'alpha' }

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'render-cache-test-'))
}

function cache(dir: string, debounceMs = 5_000): RenderCache {
  return new RenderCache({ dir, appVersion: '1.0.0', debounceMs })
}

function list(scope: { connectionId: string; profile: string }, ids: string[]) {
  return { resolved: scope, sessions: ids.map(id => ({ id, profile: scope.profile })) }
}

afterEach(() => {
  vi.useRealTimers()
})

test('round-trips a scope through disk with 0600 files', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['s1', 's2']))
  writer.flush()

  const files = fs.readdirSync(dir)
  assert.equal(files.length, 1)

  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(path.join(dir, files[0])).mode & 0o777, 0o600)
  }

  const read = cache(dir).readSessions(A)
  assert.deepEqual(read, list(A, ['s1', 's2']))
})

test("profile B never reads profile A's cached list (same connection or not)", () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a-only']))

  // Pending (pre-flush) and on-disk reads are both scoped.
  assert.equal(writer.readSessions(B), null)
  assert.equal(writer.readSessions(REMOTE_A), null)
  writer.flush()

  const reader = cache(dir)
  assert.equal(reader.readSessions(B), null)
  assert.equal(reader.readSessions(REMOTE_A), null)
  assert.deepEqual(reader.readSessions(A)?.sessions, [{ id: 'a-only', profile: 'alpha' }])
})

test("a file copied under another scope's name is rejected by its envelope scope", () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a']))
  writer.putSessions(B, list(B, ['b']))
  writer.flush()

  const [first, second] = fs.readdirSync(dir)
  const firstBody = fs.readFileSync(path.join(dir, first))
  fs.writeFileSync(path.join(dir, second), firstBody)

  const reader = cache(dir)
  const results = [reader.readSessions(A), reader.readSessions(B)]
  // Exactly one scope still resolves (the one whose file is genuine); the
  // overwritten one reads null instead of the other scope's rows.
  assert.equal(results.filter(Boolean).length, 1)
})

test('fail-open: corrupt, schema-mismatched and invalid-scope reads are null', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a']))
  writer.flush()

  const [file] = fs.readdirSync(dir)
  const target = path.join(dir, file)
  const envelope = JSON.parse(fs.readFileSync(target, 'utf8'))

  fs.writeFileSync(target, JSON.stringify({ ...envelope, schema: 1 }))
  assert.equal(cache(dir).readSessions(A), null)

  fs.writeFileSync(target, '{not json')
  assert.equal(cache(dir).readSessions(A), null)

  assert.equal(cache(dir).readSessions({ connectionId: '', profile: 'alpha' }), null)
  assert.equal(cache(dir).readSessions(null), null)
})

test('writes are debounced and never happen before the window elapses', () => {
  vi.useFakeTimers()
  const dir = tmpDir()
  const writer = cache(dir, 5_000)
  writer.putSessions(A, list(A, ['a']))
  writer.putSessions(A, list(A, ['a', 'b']))

  vi.advanceTimersByTime(4_999)
  assert.deepEqual(fs.readdirSync(dir), [])

  vi.advanceTimersByTime(1)
  assert.equal(fs.readdirSync(dir).length, 1)
  assert.deepEqual(
    cache(dir)
      .readSessions(A)
      ?.sessions.map(row => (row as { id: string }).id),
    ['a', 'b']
  )
})

test('rows are capped', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  const ids = Array.from({ length: MAX_SESSION_ROWS + 25 }, (_, index) => `s${index}`)
  writer.putSessions(A, list(A, ids))
  writer.flush()
  assert.equal(cache(dir).readSessions(A)?.sessions.length, MAX_SESSION_ROWS)
})

test('clear wipes every scope, pending included', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a']))
  writer.flush()
  writer.putSessions(B, list(B, ['b']))
  writer.clear()
  writer.flush()

  assert.deepEqual(fs.readdirSync(dir), [])
  assert.equal(writer.readSessions(A), null)
  assert.equal(writer.readSessions(B), null)
})

test('dropProfile removes only the deleted profile, on every connection', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a']))
  writer.putSessions(REMOTE_A, list(REMOTE_A, ['ra']))
  writer.putSessions(B, list(B, ['b']))
  writer.flush()

  writer.dropProfile('alpha')

  assert.equal(writer.readSessions(A), null)
  assert.equal(writer.readSessions(REMOTE_A), null)
  assert.deepEqual(writer.readSessions(B), list(B, ['b']))
})

test('migrateProfile re-keys local entries and leaves remote same-named profiles alone', () => {
  const dir = tmpDir()
  const writer = cache(dir)
  writer.putSessions(A, list(A, ['a']))
  writer.putSessions(REMOTE_A, list(REMOTE_A, ['ra']))

  writer.migrateProfile('alpha', 'gamma')

  const renamed = { connectionId: 'local', profile: 'gamma' }
  assert.equal(writer.readSessions(A), null)
  assert.deepEqual(writer.readSessions(renamed), {
    resolved: renamed,
    sessions: [{ id: 'a', profile: 'gamma' }]
  })
  assert.deepEqual(writer.readSessions(REMOTE_A), list(REMOTE_A, ['ra']))
})

test('legacy unscoped v1 files are swept on first read', () => {
  const dir = tmpDir()

  for (const name of ['sessions.json', 'status.json', 'transcript-abc.json']) {
    fs.writeFileSync(path.join(dir, name), '{}')
  }

  assert.equal(cache(dir).readSessions(A), null)
  assert.deepEqual(fs.readdirSync(dir), [])
})

test('sameRenderCacheScope normalizes the default profile and rejects empty connections', () => {
  assert.equal(
    sameRenderCacheScope({ connectionId: 'local', profile: '' }, { connectionId: 'local', profile: 'default' }),
    true
  )
  assert.equal(sameRenderCacheScope(A, B), false)
  assert.equal(sameRenderCacheScope({ connectionId: '', profile: 'x' }, { connectionId: '', profile: 'x' }), false)
})
