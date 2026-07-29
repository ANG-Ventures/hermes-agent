/**
 * Regression: the app icon handed to NATIVE Electron APIs must be a real
 * on-disk file, never a path inside the asar archive.
 *
 * The desktop app failed to launch on macOS with an uncaught main-process
 * exception:
 *
 *     A JavaScript error occurred in the main process, Uncaught Exception:
 *     Error: Failed to load image from path
 *       '/Applications/Hermes.app/Contents/Resources/app.asar/public/apple-touch-icon.png'
 *       at createWindow (.../electron-main.mjs)
 *
 * The mechanism is a genuine asymmetry that is easy to reintroduce:
 *
 *   - `fs.statSync()` CAN read paths inside `app.asar` (Electron shims fs), so
 *     an existence probe happily accepts `<APP_ROOT>/public/apple-touch-icon.png`.
 *   - `app.dock.setIcon()` / BrowserWindow's `icon` are NATIVE calls that CANNOT
 *     read inside an asar, so the same path throws.
 *
 * `getAppIconPath()` picks the first *existing* candidate, so ordering decides
 * whether the app starts. `dist/**` is in `build.asarUnpack`, so the unpacked
 * copy is the only candidate guaranteed to be natively readable in a packaged
 * app — it must come first.
 *
 * Two contracts are locked here:
 *   1. the asar.unpacked candidate is ordered before any in-asar candidate, and
 *   2. the `setIcon()` call is wrapped so a cosmetic icon failure can never
 *      abort `createWindow()` and leave the app with no window.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

const DESKTOP_ROOT = path.resolve(__dirname, '..')
const MAIN_TS = path.join(DESKTOP_ROOT, 'electron', 'main.ts')
const DESKTOP_PKG = path.join(DESKTOP_ROOT, 'package.json')

function mainSource(): string {
  assert.ok(fs.existsSync(MAIN_TS), `missing ${MAIN_TS}`)

  return fs.readFileSync(MAIN_TS, 'utf-8')
}

test('APP_ICON_PATHS prefers the asar.unpacked copy over in-asar paths', () => {
  const src = mainSource()
  const block = /const APP_ICON_PATHS = \[([\s\S]*?)\]/.exec(src)
  assert.ok(block, 'APP_ICON_PATHS array not found in electron/main.ts')

  const entries = block[1]
    .split('\n')
    .map(line => line.trim())
    .filter(line => line.startsWith('path.join('))

  assert.ok(entries.length > 0, 'APP_ICON_PATHS has no path.join() candidates')

  const unpackedIndex = entries.findIndex(entry => entry.includes('unpackedPathFor('))
  assert.notEqual(
    unpackedIndex,
    -1,
    'APP_ICON_PATHS must include an unpackedPathFor() candidate; native ' +
      'setIcon() cannot read a file inside app.asar.'
  )

  assert.equal(
    unpackedIndex,
    0,
    'the unpackedPathFor() candidate must come FIRST. getAppIconPath() returns ' +
      'the first candidate that fs.statSync() finds, and fs CAN see inside the ' +
      'asar while native setIcon() cannot — an in-asar path ordered first is ' +
      'selected and then throws at startup.'
  )
})

test('dock setIcon failures cannot abort createWindow', () => {
  const src = mainSource()
  const call = src.indexOf('app.dock?.setIcon(')
  assert.notEqual(call, -1, 'app.dock?.setIcon( call not found in electron/main.ts')

  // Look backwards a short window for the guarding try {.
  const preceding = src.slice(Math.max(0, call - 400), call)
  assert.match(
    preceding,
    /try\s*\{[^}]*$/,
    'app.dock?.setIcon() must be wrapped in try/catch — it is a native call ' +
      'that throws on an unreadable path, and an uncaught throw here aborts ' +
      'createWindow() so the app starts with no window at all.'
  )
})

test('dist/ is unpacked from the asar so the icon candidate exists on disk', () => {
  const pkg = JSON.parse(fs.readFileSync(DESKTOP_PKG, 'utf-8')) as {
    build?: { asarUnpack?: string[] }
  }

  const asarUnpack = pkg.build?.asarUnpack ?? []

  assert.ok(
    asarUnpack.some(pattern => pattern.startsWith('dist/')),
    'build.asarUnpack must unpack dist/** — the APP_ICON_PATHS unpacked ' +
      'candidate resolves to <app.asar.unpacked>/dist/apple-touch-icon.png, ' +
      `which only exists if dist is unpacked. Got: ${JSON.stringify(asarUnpack)}`
  )
})
