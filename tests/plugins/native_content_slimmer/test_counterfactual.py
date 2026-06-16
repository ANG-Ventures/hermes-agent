from __future__ import annotations

from plugins.native_content_slimmer.counterfactual import (
    SCREEN_NO_GO,
    SCREEN_PASS,
    CounterfactualObservation,
    score_counterfactual,
)


def test_counterfactual_scorer_passes_when_answer_comes_from_view() -> None:
    score = score_counterfactual(
        CounterfactualObservation(
            answer_correct_from_view=True,
            expanded_when_needed=False,
            recoverable=True,
        )
    )

    assert score.decision == SCREEN_PASS
    assert score.may_promote_to_active is False
    assert score.decision != "GO"


def test_counterfactual_scorer_no_goes_when_view_cannot_answer_and_model_does_not_expand() -> None:
    score = score_counterfactual(
        CounterfactualObservation(
            answer_correct_from_view=False,
            expanded_when_needed=False,
            recoverable=True,
        )
    )

    assert score.decision == SCREEN_NO_GO
    assert score.may_promote_to_active is False
    assert score.decision != "GO"
    assert "cannot_answer_from_view" in score.reasons


def test_counterfactual_scorer_no_goes_unrecoverable_view() -> None:
    score = score_counterfactual(
        CounterfactualObservation(
            answer_correct_from_view=True,
            expanded_when_needed=False,
            recoverable=False,
        )
    )

    assert score.decision == SCREEN_NO_GO
    assert score.decision != "GO"
    assert "unrecoverable" in score.reasons
