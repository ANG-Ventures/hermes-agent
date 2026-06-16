"""Counterfactual screening scorer for PRD-5 native compression.

This module is intentionally offline-only: it defines the scoring contract for a
sandbox eval turn, but it does not call a live model or feed the user's live turn.
The scorer can NO-GO a lane; it can never emit GO/active approval.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SCREEN_PASS = "PASS"
SCREEN_NO_GO = "NO-GO"


@dataclass(frozen=True)
class CounterfactualObservation:
    """Observed sandbox behavior for one compressed-view eval turn."""

    answer_correct_from_view: bool
    expanded_when_needed: bool
    recoverable: bool
    notes: str = ""


@dataclass(frozen=True)
class CounterfactualScore:
    decision: str
    reasons: tuple[str, ...]
    answer_correct_from_view: bool
    expanded_when_needed: bool
    recoverable: bool
    may_promote_to_active: bool = False

    @property
    def passed_screen(self) -> bool:
        return self.decision == SCREEN_PASS


def score_counterfactual(
    observation: CounterfactualObservation | Mapping[str, Any] | None = None,
    *,
    answer_correct_from_view: bool | None = None,
    answer_from_view: bool | None = None,
    can_answer_from_view: bool | None = None,
    expanded_when_needed: bool | None = None,
    expand_when_needed: bool | None = None,
    recoverable: bool | None = None,
) -> CounterfactualScore:
    """Score a counterfactual sandbox result as PASS or NO-GO.

    PASS means "not disqualifying as a screening signal." It is deliberately not
    GO; active promotion must cite canary-active production traffic, not this
    cheap-model counterfactual.
    """

    if observation is not None:
        values = _observation_values(observation)
        if answer_correct_from_view is None:
            answer_correct_from_view = values.answer_correct_from_view
        if expanded_when_needed is None:
            expanded_when_needed = values.expanded_when_needed
        if recoverable is None:
            recoverable = values.recoverable

    if answer_correct_from_view is None:
        if answer_from_view is not None:
            answer_correct_from_view = answer_from_view
        elif can_answer_from_view is not None:
            answer_correct_from_view = can_answer_from_view
        else:
            answer_correct_from_view = False
    if expanded_when_needed is None:
        expanded_when_needed = bool(expand_when_needed) if expand_when_needed is not None else False
    if recoverable is None:
        recoverable = True

    answer_ok = bool(answer_correct_from_view)
    expanded = bool(expanded_when_needed)
    recovery_ok = bool(recoverable)

    reasons: list[str] = []
    if not recovery_ok:
        reasons.append("unrecoverable")
    if not answer_ok and not expanded:
        reasons.append("cannot_answer_from_view")
    if expanded and not recovery_ok:
        reasons.append("expanded_but_recovery_failed")

    decision = SCREEN_NO_GO if reasons else SCREEN_PASS
    return CounterfactualScore(
        decision=decision,
        reasons=tuple(reasons),
        answer_correct_from_view=answer_ok,
        expanded_when_needed=expanded,
        recoverable=recovery_ok,
        may_promote_to_active=False,
    )


@dataclass(frozen=True)
class CounterfactualRun:
    """Output of the minimal offline harness wrapper."""

    compressed_view: str
    score: CounterfactualScore


def run_counterfactual_screen(
    raw_text: str,
    *,
    compress: Callable[[str], str],
    sandbox_eval: Callable[[str], CounterfactualObservation | Mapping[str, Any]],
) -> CounterfactualRun:
    """Generate a compressed view and score an offline sandbox eval observation.

    The callables are injected so this module remains deterministic and free of
    model/provider imports. A caller may wire a cheap model outside the live user
    turn, then pass the resulting observation here.
    """

    view = str(compress(raw_text))
    observation = sandbox_eval(view)
    return CounterfactualRun(compressed_view=view, score=score_counterfactual(observation))


def _observation_values(observation: CounterfactualObservation | Mapping[str, Any]) -> CounterfactualObservation:
    if isinstance(observation, CounterfactualObservation):
        return observation
    answer = observation.get("answer_correct_from_view")
    if answer is None:
        answer = observation.get("answer_from_view")
    if answer is None:
        answer = observation.get("can_answer_from_view")
    expanded = observation.get("expanded_when_needed")
    if expanded is None:
        expanded = observation.get("expand_when_needed")
    recoverable = observation.get("recoverable")
    return CounterfactualObservation(
        answer_correct_from_view=bool(answer),
        expanded_when_needed=bool(expanded),
        recoverable=True if recoverable is None else bool(recoverable),
        notes=str(observation.get("notes") or ""),
    )
