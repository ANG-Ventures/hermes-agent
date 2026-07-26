import { describe, expect, it } from 'vitest'

import {
  desktopSlashUnavailableMessage,
  isDesktopSlashCommand,
  resolveDesktopCommand
} from '@/lib/desktop-slash-commands'
import { REASONING_COMMAND_HELP, REASONING_DISPLAY_VALUES, REASONING_EFFORTS } from '@/lib/reasoning-effort'

describe('/reasoning desktop surface', () => {
  it('is a known desktop command', () => {
    expect(isDesktopSlashCommand('reasoning')).toBe(true)
  })

  it('resolves to a local action rather than slash.exec', () => {
    const resolved = resolveDesktopCommand('reasoning')

    expect(resolved?.surface.kind).toBe('action')
    expect(resolved?.surface).toMatchObject({ action: 'reasoning' })
  })

  it('no longer reports itself as unavailable on desktop', () => {
    // Regression: /reasoning used to sit in NO_DESKTOP_SURFACE.advanced, so
    // the palette told users it had no desktop surface while the TUI had one.
    expect(desktopSlashUnavailableMessage('reasoning')).toBeFalsy()
  })
})

describe('reasoning command help', () => {
  it('is derived from both ladders, not hand-written', () => {
    // Contract, not a snapshot: whatever the ladders hold, the help string
    // must offer exactly those values — so adding a backend tier can never
    // leave the help text silently stale.
    for (const effort of REASONING_EFFORTS) {
      expect(REASONING_COMMAND_HELP).toContain(effort)
    }

    for (const display of REASONING_DISPLAY_VALUES) {
      expect(REASONING_COMMAND_HELP).toContain(display)
    }

    expect(REASONING_COMMAND_HELP).toContain('none')
  })

  it('keeps effort levels and display modes disjoint', () => {
    // The handler branches on isReasoningEffort() to decide whether to move
    // the composer pill; an overlap would make that branch ambiguous.
    const efforts = new Set<string>(REASONING_EFFORTS)

    for (const display of REASONING_DISPLAY_VALUES) {
      expect(efforts.has(display)).toBe(false)
    }
  })

  it('offers every display mode the handler can receive', () => {
    expect(REASONING_DISPLAY_VALUES.length).toBeGreaterThan(0)
    expect(new Set(REASONING_DISPLAY_VALUES).size).toBe(REASONING_DISPLAY_VALUES.length)
  })
})
