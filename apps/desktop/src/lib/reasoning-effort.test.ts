import { describe, expect, it } from 'vitest'

import {
  ENABLED_REASONING_EFFORTS,
  isEnabledReasoningEffort,
  isReasoningEffort,
  normalizeEnabledReasoningEffort,
  REASONING_COMMAND_HELP,
  REASONING_DISPLAY_VALUES,
  REASONING_EFFORTS
} from './reasoning-effort'

/**
 * These tests assert INVARIANTS between the exported ladders and their
 * predicates, not a frozen catalog of tier names.
 *
 * The previous version asserted `ENABLED_REASONING_EFFORTS` equals an exact
 * literal list. That is a change-detector: adding or reordering a valid
 * backend tier breaks the test even though the Desktop contract is still
 * correct, so the failure carries no information about the thing under test.
 *
 * The relationships below are what Desktop actually depends on, and they hold
 * for any tier ladder the backend chooses. They still fail on a real contract
 * break (e.g. a display value leaking into the effort ladder).
 */
describe('desktop reasoning effort contract', () => {
  it('derives the off-inclusive ladder from the enabled ladder', () => {
    expect(REASONING_EFFORTS).toEqual(['none', ...ENABLED_REASONING_EFFORTS])
    expect(ENABLED_REASONING_EFFORTS).toEqual(REASONING_EFFORTS.filter(value => value !== 'none'))
    // Order is a contract (the ladder is ranked), so relative order must survive.
    expect(REASONING_EFFORTS.indexOf('none')).toBe(0)
  })

  it('keeps both ladders non-empty and duplicate-free', () => {
    expect(ENABLED_REASONING_EFFORTS.length).toBeGreaterThan(0)
    expect(new Set(REASONING_EFFORTS).size).toBe(REASONING_EFFORTS.length)
    expect(new Set(REASONING_DISPLAY_VALUES).size).toBe(REASONING_DISPLAY_VALUES.length)
  })

  it('keeps the predicates consistent with the ladders they guard', () => {
    expect(REASONING_EFFORTS.every(isReasoningEffort)).toBe(true)
    expect(ENABLED_REASONING_EFFORTS.every(isEnabledReasoningEffort)).toBe(true)
    // Every enabled tier is also a valid effort; the reverse holds for all but 'none'.
    expect(ENABLED_REASONING_EFFORTS.every(isReasoningEffort)).toBe(true)
    expect(isReasoningEffort('none')).toBe(true)
    expect(isEnabledReasoningEffort('none')).toBe(false)
  })

  it('keeps display commands disjoint from effort values', () => {
    expect(REASONING_DISPLAY_VALUES.every(value => !isReasoningEffort(value))).toBe(true)
    expect(REASONING_EFFORTS.every(value => !REASONING_DISPLAY_VALUES.includes(value as never))).toBe(true)
  })

  it('derives the help string from both ladders rather than hand-writing it', () => {
    expect(REASONING_COMMAND_HELP).toBe([...REASONING_EFFORTS, ...REASONING_DISPLAY_VALUES].join('|'))
    expect(REASONING_COMMAND_HELP.split('|')).toHaveLength(
      REASONING_EFFORTS.length + REASONING_DISPLAY_VALUES.length
    )
  })

  // Behavioral coverage the review asked to KEEP: these tiers are the reason
  // the Desktop ladder was extended, so they are asserted by behavior
  // (accepted by the predicate) rather than by position in a frozen list.
  it('accepts the extended xhigh/max/ultra tiers', () => {
    for (const tier of ['xhigh', 'max', 'ultra']) {
      expect(isEnabledReasoningEffort(tier)).toBe(true)
      expect(isReasoningEffort(tier)).toBe(true)
    }
  })

  it('normalizes user input while keeping the strict predicate strict', () => {
    expect(isReasoningEffort(' ULTRA ')).toBe(false)
    expect(normalizeEnabledReasoningEffort(' ULTRA ')).toBe('ultra')
    expect(normalizeEnabledReasoningEffort('unknown')).toBe('medium')
    // Normalization must land inside the enabled ladder for every enabled tier,
    // including any future one, in both padded and upper-cased forms.
    for (const tier of ENABLED_REASONING_EFFORTS) {
      expect(normalizeEnabledReasoningEffort(` ${tier.toUpperCase()} `)).toBe(tier)
    }
  })

  it('falls back to a tier that is itself enabled', () => {
    const fallback = normalizeEnabledReasoningEffort('definitely-not-a-tier')
    expect(isEnabledReasoningEffort(fallback)).toBe(true)
  })
})
