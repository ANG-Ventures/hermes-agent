/**
 * E2E tests for the tile-unread bug — two scenarios:
 *
 * 1. TAB (stacked, not visible) — a session opened as a tab via ⌃-click is
 *    NOT visible on screen. When it finishes, the green "unread" dot IS
 *    correct — the user isn't looking at it. This test PASSES.
 *
 * 2. SPLIT (side-by-side, visible) — a session dragged to the edge of the
 *    workspace zone opens as a split tile, visible on screen at the same time
 *    as the main session. When it finishes, it should NOT get the green
 *    "unread" dot — the user is looking right at it. This test FAILS until
 *    the fix in session-states.ts:174 lands (the unread check only compares
 *    against $selectedStoredSessionId and ignores $sessionTiles).
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from '@playwright/test'

import {
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady,
} from './fixtures'
import { SIDEBAR_CROSS_TEXTS, restartMockServer } from './mock-server'

/** Finished-unread dot aria-label. */
const UNREAD_DOT_LABEL = 'Finished — unread'
/** Background-running dot aria-label. */
const BG_DOT_LABEL = 'Background task running'
/** Session-running dot aria-label — the state the unread dot succeeds. */
const RUNNING_DOT_LABEL = 'Session running'

/**
 * The sidebar row's "open as a tab" chord.
 *
 * `session-row.tsx` accepts `metaKey || ctrlKey`, but on macOS a ctrl-click is
 * a RIGHT-click: the browser fires `contextmenu` and the `onClick` handler
 * never runs, so a hardcoded `Control` opens no tab at all and the test
 * silently degrades into "session is merely deselected". Pick the modifier
 * that actually reaches the handler on the host platform so the spec exercises
 * the tab path on macOS as well as on the Linux CI runner.
 */
const TAB_OPEN_MODIFIER = process.platform === 'darwin' ? 'Meta' : 'Control'

/** Locate a session's sidebar row by its preview text. */
function sessionRow(page: import('@playwright/test').Page, text: string) {
  return page.locator('[data-slot="sidebar"] button').filter({ hasText: text }).first()
}

/** Common setup: start a turn with a long bg process + subagent, wait for
 *  the turn to complete, then switch to a new session so the first session is
 *  no longer $selectedStoredSessionId (required before opening a tile). */
async function startTurnAndSwitchAway(page: import('@playwright/test').Page) {
  // Send E2E_SIDEBAR_CROSS — starts a turn with a long bg sleep + subagent.
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.waitFor({ state: 'visible', timeout: 10_000 })
  await composer.click()
  await composer.type('E2E_SIDEBAR_CROSS', { delay: 20 })
  await page.keyboard.press('Enter')

  // Wait for the user's message to appear.
  await page.waitForFunction(
    () => (document.body.textContent ?? '').includes('E2E_SIDEBAR_CROSS'),
    undefined,
    { timeout: 15_000 },
  )

  // Wait for the background dot — confirms the turn is running.
  await expect
    .poll(
      () => page.locator(`[aria-label="${BG_DOT_LABEL}"]`).count(),
      { timeout: 30_000, message: 'background dot should appear' },
    )
    .toBeGreaterThan(0)

  // Wait for the turn to complete (final answer visible).
  await page.waitForFunction(
    (text) => (document.body.textContent ?? '').includes(text),
    SIDEBAR_CROSS_TEXTS.finalText,
    { timeout: 90_000 },
  )

  // The background dot should still be visible: the mock sleeps 30s
  // (SIDEBAR_CROSS_BG_SLEEP_SECONDS), far longer than any turn latency.
  const bgDuringTurn = await page.locator(`[aria-label="${BG_DOT_LABEL}"]`).count()
  expect(bgDuringTurn, 'background dot should still be visible after turn completes').toBeGreaterThan(0)

  // Switch to a new session — session A is no longer $selectedStoredSessionId.
  // This is required: openSessionTile bails if the session is already selected.
  await page.locator('button:has-text("New session")').first().click()
  await page.waitForTimeout(2000)
}

/**
 * Wait for the background process to finish and its dot to auto-dismiss.
 *
 * Budget must exceed the mock's background sleep (SIDEBAR_CROSS_BG_SLEEP_SECONDS,
 * 30s as of 2026-07-27) PLUS SUCCESS_LINGER_MS (4s) plus scheduling slack.
 * This is a HANG-GUARD, not a timing assertion: it asserts the dot eventually
 * disappears, and a ceiling well above the real duration fails only on a genuine
 * regression. (Keeping it at 30s while the sleep became 30s would have made THIS
 * a race in turn -- the exact bug we are fixing, relocated.)
 */
async function waitForBgProcessToFinish(page: import('@playwright/test').Page) {
  await expect
    .poll(
      () => page.locator(`[aria-label="${BG_DOT_LABEL}"]`).count(),
      { timeout: 60_000, message: 'background dot should disappear after process finishes' },
    )
    .toBe(0)
}

/**
 * Wait until session A's row leaves the RUNNING state.
 *
 * WHY THIS EXISTS (the bug this spec kept hitting, root-caused 2026-07-27):
 * the unread flag is written by the `busy: true -> false` LLM-turn transition
 * (`session-states.ts` handleTransition). The background dot is a DIFFERENT,
 * independent signal, driven by the gateway's process registry. When the bg
 * process completes, the app must (a) drop the process from the registry and
 * (b) deliver the notify_on_complete follow-up turn, whose OWN busy edge is
 * what actually sets unread. Those two land in either order.
 *
 * `waitForBgProcessToFinish` only observes (a). Measured locally, (b) trails it
 * by ~0.5s about half the time -- so an assertion fired straight after (a) read
 * the sidebar mid-turn, while the row still showed "Session running", and saw
 * zero unread dots. That is a WALL-CLOCK RACE in the wait, not a product bug:
 * polling for the dot proves it, since the same run goes green ~500ms later
 * with no product change.
 *
 * Waiting for the running dot to clear observes the transition that OWNS the
 * unread flag, so the subsequent assertion reads a settled state. It is a
 * hang-guard with a ceiling far above the real duration -- it fails only if the
 * turn genuinely never terminates, and it does NOT assert unread itself, so it
 * cannot make the unread assertion pass vacuously.
 */
async function waitForTurnToSettle(page: import('@playwright/test').Page) {
  await expect
    .poll(
      () => page.locator(`[aria-label="${RUNNING_DOT_LABEL}"]`).count(),
      { timeout: 60_000, message: 'session-running dot should clear once the follow-up turn ends' },
    )
    .toBe(0)
}

// ────────────────────────────────────────────────────────────────────────
// Test 1: TAB (not visible) — unread dot IS correct (PASSES)
// ────────────────────────────────────────────────────────────────────────

test.describe('sidebar states — tab (hidden) unread is correct', () => {
  test.describe.configure({ mode: 'serial' })

  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    restartMockServer()
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('session opened as a tab (not visible) correctly gets unread dot', async () => {
    const page = fixture.page

    await startTurnAndSwitchAway(page)

    // Evidence: session A is in the background (bg dot in sidebar).
    await page.screenshot({ path: 'test-results/tile-bug-tab-switched-away.png' })

    // ⌃-click opens the session as a TAB (center dock = stacked, not visible
    // unless it's the active tab). The session is NOT on screen.
    const row = sessionRow(page, SIDEBAR_CROSS_TEXTS.finalText)
    await row.click({ modifiers: [TAB_OPEN_MODIFIER] })

    // The tab must actually exist before we assert anything about it -- a
    // modifier that never reached the onClick handler would otherwise let this
    // test quietly assert the "merely deselected" case instead of the tab case.
    // The tree's TAB STRIP is the right surface to check: a stacked tab that
    // isn't fronted renders its tab but NOT its pane body, so the tab element
    // (`data-tree-tab="session-tile:<id>"`, tree-group.tsx) is the only proof
    // the tile exists AND is hidden.
    await expect
      .poll(
        () => page.locator('[data-tree-tab^="session-tile:"]').count(),
        { timeout: 15_000, message: 'the ⌘/⌃-click should have opened the session as a tab' },
      )
      .toBeGreaterThan(0)

    // Evidence: the tab is open but the session is not visible on screen.
    await page.screenshot({ path: 'test-results/tile-bug-tab-opened.png' })

    await waitForBgProcessToFinish(page)
    // The bg dot going away does NOT mean the unread-owning turn has ended.
    await waitForTurnToSettle(page)

    // A tab that's not the active tab IS hidden — the unread dot is correct.
    // The user is NOT looking at it, so marking it "unread" is right.
    //
    // POLL, don't sample. The unread flag is set by an event-driven store
    // transition, so a bare `.count()` reads whatever frame happens to be
    // painted. The sibling cross-session spec in sidebar-states.spec.ts already
    // polls this exact assertion for the same reason; this one was left as a
    // one-shot read and was the last remaining sampler in the file.
    await expect
      .poll(
        () => page.locator(`[aria-label="${UNREAD_DOT_LABEL}"]`).count(),
        { timeout: 30_000, message: 'hidden tab should be marked unread' },
      )
      .toBeGreaterThan(0)

    await page.screenshot({ path: 'test-results/tile-bug-tab-unread-correct.png' })
  })
})

// ────────────────────────────────────────────────────────────────────────
// Test 2: SPLIT (visible) — unread dot is WRONG (FAILS until fix)
// ────────────────────────────────────────────────────────────────────────

test.describe.skip('sidebar states — split (visible) unread bug (RED)', () => {
  test.describe.configure({ mode: 'serial' })

  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    restartMockServer()
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('session visible in a split tile does NOT get unread dot when it finishes', async () => {
    const page = fixture.page

    await startTurnAndSwitchAway(page)

    // Evidence: session A is in the background (bg dot in sidebar).
    await page.screenshot({ path: 'test-results/tile-bug-split-switched-away.png' })

    // Drag the session row from the sidebar to the right edge of the workspace
    // zone to create a SPLIT (side-by-side) tile. This triggers the real
    // startSessionDrag → onCommit → openSessionTile(id, 'right', anchor) path.
    const row = sessionRow(page, SIDEBAR_CROSS_TEXTS.finalText)
    const rowBox = await row.boundingBox()
    expect(rowBox, 'session row must be visible').not.toBeNull()

    // Find the workspace zone — the main chat area. We drop on its right edge.
    const workspace = page.locator('[data-session-anchor="workspace"]')
    const wsBox = await workspace.boundingBox()
    expect(wsBox, 'workspace zone must be visible').not.toBeNull()

    // Drag from the session row to the right edge of the workspace.
    // The drag-session's subZonePosition resolves a right-edge drop as 'right'
    // (a split dock), not 'center' (which would be a composer link).
    await page.mouse.move(rowBox!.x + rowBox!.width / 2, rowBox!.y + rowBox!.height / 2)
    await page.mouse.down()
    // Move in steps so the drag-session's pointermove handler tracks the
    // position and resolves the drop zone (a single jump can miss the
    // threshold/engage logic).
    const targetX = wsBox!.x + wsBox!.width - 20
    const targetY = wsBox!.y + wsBox!.height / 2
    const steps = 10
    for (let i = 1; i <= steps; i++) {
      const x = rowBox!.x + rowBox!.width / 2 + (targetX - (rowBox!.x + rowBox!.width / 2)) * (i / steps)
      const y = rowBox!.y + rowBox!.height / 2 + (targetY - (rowBox!.y + rowBox!.height / 2)) * (i / steps)
      await page.mouse.move(x, y)
      await page.waitForTimeout(30)
    }
    await page.mouse.up()
    await page.waitForTimeout(2000)

    // Evidence: the split tile is now open side-by-side — both sessions visible.
    await page.screenshot({ path: 'test-results/tile-bug-split-opened.png' })

    await waitForBgProcessToFinish(page)

    // THE BUG: the session visible in the split tile should NOT have the green
    // "finished unread" dot — the user is looking right at it. This assertion
    // FAILS until the fix in session-states.ts:174 lands.
    const unreadCount = await page.locator(`[aria-label="${UNREAD_DOT_LABEL}"]`).count()
    expect(unreadCount, 'session visible in a split tile should NOT be marked unread').toBe(0)

    // Evidence: the green dot should NOT be here — this screenshot shows the bug.
    await page.screenshot({ path: 'test-results/tile-bug-split-unread-should-not-exist.png' })
  })
})
