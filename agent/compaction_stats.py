"""``CompactionStats`` — the single, typed, self-checking source of truth for a
context-compaction's before/after composition, shared by every announce path
(session-hygiene, in-turn LCM, in-turn built-in, overflow, manual /compress).

Design (from the approved spec, 7 Opus review passes):

- ONE typed object carries the whole breakdown; the formatter renders from it.
- ``validate()`` returns ``(ok, reason)`` and **never raises** — and is NOT
  called from ``__post_init__``. Live paths build a stats object inside
  try/except and degrade to the two-line announce on ``not ok``; a reconcile
  failure can never reach the user's reply.
- ``assert_reconciles()`` raises — for tests/CI ONLY.
- The MESSAGE axis is EXACT; the TOKEN axis allows a small estimator tolerance.
- Every ``*_tokens`` field is an ``estimate_messages_tokens_rough`` output over
  its row subset — NEVER the model's live ``prompt_tokens`` (same-estimator
  contract), so the additive identities are real cross-checks, not noise.

Bucket model::

    pre_messages  = cleared + folded + kept           (every removed/folded/kept row)
    eligible      = kept + folded                       (the hygiene-filter survivors / engine pop)
    cleared       = pre - eligible                      (filtered-out: tool + system + contentless-asst)
    post_messages = kept + summary_messages + anchor    (what the model sees next)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Token axis tolerance: the rough estimator is additive over disjoint row sets,
# so a clean partition reconciles within a couple tokens. Keep small — a real
# bucketing bug moves far more than this.
_TOKEN_TOL = 8


@dataclass
class CompactionStats:
    # ── message axis (EXACT) ──
    pre_messages: int
    post_messages: int
    eligible_count: int
    kept_messages: int
    summary_messages: int
    anchor_messages: int
    cleared_count: int
    folded_count: int
    # ── token axis (±estimator tolerance; all from estimate_messages_tokens_rough) ──
    pre_tokens: int
    post_tokens: int
    kept_tokens: int
    summary_tokens: int
    anchor_tokens: int
    cleared_tokens: int
    folded_tokens: int
    # ── optional sub-split of `cleared` (only when Phase-0 attribution is clean) ──
    cleared_tool_count: Optional[int] = None
    cleared_tool_tokens: Optional[int] = None
    cleared_other_count: Optional[int] = None
    cleared_other_tokens: Optional[int] = None

    # NOTE: deliberately NO validation in __post_init__ (keeps any raise off the
    # hot path; callers invoke validate()/assert_reconciles() explicitly).

    @property
    def freed_tokens(self) -> int:
        return self.pre_tokens - self.post_tokens

    @property
    def freed_pct(self) -> Optional[int]:
        if self.pre_tokens <= 0:
            return None
        return max(0, min(100, round(self.freed_tokens / self.pre_tokens * 100)))

    def validate(self) -> Tuple[bool, str]:
        """Return ``(ok, reason)``. Never raises. ``reason`` empty when ok."""
        # ── message axis: EXACT ──
        if self.cleared_count + self.folded_count + self.kept_messages != self.pre_messages:
            return False, (
                f"msg axis: cleared {self.cleared_count} + folded {self.folded_count} "
                f"+ kept {self.kept_messages} != pre {self.pre_messages}"
            )
        if self.cleared_count != self.pre_messages - self.eligible_count:
            return False, (
                f"eligible: cleared {self.cleared_count} != pre {self.pre_messages} "
                f"- eligible {self.eligible_count}"
            )
        if self.kept_messages + self.folded_count != self.eligible_count:
            return False, (
                f"eligible: kept {self.kept_messages} + folded {self.folded_count} "
                f"!= eligible {self.eligible_count}"
            )
        if self.post_messages != self.kept_messages + self.summary_messages + self.anchor_messages:
            return False, (
                f"post msg: kept {self.kept_messages} + summary {self.summary_messages} "
                f"+ anchor {self.anchor_messages} != post {self.post_messages}"
            )
        # ── zero-fold first-class (the literal 222→222 shape) ──
        if self.folded_count == 0:
            if self.summary_messages != 0 or self.summary_tokens != 0:
                return False, (
                    f"zero-fold: folded==0 requires summary_messages==0 and "
                    f"summary_tokens==0 (got {self.summary_messages}/{self.summary_tokens})"
                )
            if self.kept_messages != self.eligible_count:
                return False, (
                    f"zero-fold: folded==0 requires kept==eligible "
                    f"({self.kept_messages} != {self.eligible_count})"
                )
        # ── token axis: ±tolerance ──
        if self.pre_tokens <= 0:
            return False, f"pre_tokens must be > 0 (got {self.pre_tokens})"
        if abs((self.cleared_tokens + self.folded_tokens + self.kept_tokens) - self.pre_tokens) > _TOKEN_TOL:
            return False, (
                f"token pre: cleared {self.cleared_tokens} + folded {self.folded_tokens} "
                f"+ kept {self.kept_tokens} != pre {self.pre_tokens} (tol {_TOKEN_TOL})"
            )
        if abs((self.kept_tokens + self.summary_tokens + self.anchor_tokens) - self.post_tokens) > _TOKEN_TOL:
            return False, (
                f"token post: kept {self.kept_tokens} + summary {self.summary_tokens} "
                f"+ anchor {self.anchor_tokens} != post {self.post_tokens} (tol {_TOKEN_TOL})"
            )
        # freed identity with the anchor term (Pass-2 blocker fix):
        # cleared + folded - summary - anchor == freed
        freed_check = (
            self.cleared_tokens + self.folded_tokens
            - self.summary_tokens - self.anchor_tokens
        )
        if abs(freed_check - self.freed_tokens) > _TOKEN_TOL:
            return False, (
                f"freed: cleared {self.cleared_tokens} + folded {self.folded_tokens} "
                f"- summary {self.summary_tokens} - anchor {self.anchor_tokens} "
                f"= {freed_check} != freed {self.freed_tokens} (tol {_TOKEN_TOL})"
            )
        # ── optional sub-split must sum to cleared ──
        if self.cleared_tool_count is not None or self.cleared_other_count is not None:
            t = self.cleared_tool_count or 0
            o = self.cleared_other_count or 0
            if t + o != self.cleared_count:
                return False, (
                    f"sub-split: tool {t} + other {o} != cleared {self.cleared_count}"
                )
        return True, ""

    def assert_reconciles(self) -> None:
        """Raise ``ValueError`` if not reconciling. TESTS/CI ONLY — never on the hot path."""
        ok, reason = self.validate()
        if not ok:
            raise ValueError(f"CompactionStats does not reconcile: {reason}")
