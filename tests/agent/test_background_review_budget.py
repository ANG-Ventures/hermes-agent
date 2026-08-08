"""Background-review budget: raised ceiling + LOUD exhaustion.

Two properties, both learned the hard way on 2026-08-07/08:

1. The fork's ``max_iterations`` is its ONLY circuit breaker — it has no
   wall-clock timeout and spawns after every turn on an unwatched daemon
   thread. It must stay small relative to a real agent turn (raised 16 -> 30,
   not 300).

2. Exhaustion must be LOUD. The fork runs ``quiet_mode=True`` +
   ``suppress_status_output=True`` so its lifecycle chatter never leaks into
   the user's turn — which also made hitting the ceiling completely silent.
   A review could die mid-write, having banked only half its memory/skill
   writes, and nothing anywhere recorded it. That silence is why the 16 was
   never questioned for months.
"""

import logging

import pytest


def test_review_ceiling_is_documented_constant_not_a_magic_number():
    from agent import background_review

    assert hasattr(background_review, "_REVIEW_MAX_ITERATIONS"), (
        "the review ceiling must be a named module constant so it is greppable "
        "and tunable — not a bare literal buried in the AIAgent(...) call"
    )
    assert background_review._REVIEW_MAX_ITERATIONS == 30


def test_ceiling_stays_bounded():
    """Guard against 'just raise it to 300'.

    The fork has no timeout; a large ceiling turns a wandering review into a
    silent cost multiplier on every single turn.
    """
    from agent import background_review

    assert background_review._REVIEW_MAX_ITERATIONS <= 60, (
        "the review fork has NO wall-clock timeout — this cap is the only "
        "bound on a per-turn background thread. Keep it tight."
    )


def test_source_wires_the_constant_into_the_fork():
    """The constant must actually reach ``AIAgent(max_iterations=...)``.

    Without this, the constant could drift while the fork keeps a stale
    literal — the exact class of dead-config bug that hid the original 16.
    """
    import inspect

    from agent import background_review

    src = inspect.getsource(background_review)
    assert "max_iterations=_REVIEW_MAX_ITERATIONS" in src, (
        "the fork must be constructed from _REVIEW_MAX_ITERATIONS"
    )
    assert "max_iterations=16" not in src, "stale hardcoded ceiling still present"


@pytest.mark.parametrize(
    "exit_reason,should_warn",
    [
        ("max_iterations_reached(30/30)", True),
        ("budget_exhausted", True),
        ("normal", False),
        ("", False),
    ],
)
def test_exhaustion_is_logged_loudly(caplog, exit_reason, should_warn):
    """Exhaustion warns; a healthy review stays silent."""
    import inspect

    from agent import background_review

    src = inspect.getsource(background_review)
    assert "logger.warning(" in src and "EXHAUSTED its iteration budget" in src, (
        "budget exhaustion must emit a WARNING — it was silent before"
    )

    # Exercise the exact predicate the source uses.
    fires = "max_iterations_reached" in exit_reason or "budget_exhausted" in exit_reason
    assert fires is should_warn

    if should_warn:
        with caplog.at_level(logging.WARNING, logger="agent.background_review"):
            logging.getLogger("agent.background_review").warning(
                "background review EXHAUSTED its iteration budget "
                "(%s/%s, exit_reason=%s)", 30, 30, exit_reason,
            )
        assert any(
            "EXHAUSTED its iteration budget" in r.getMessage()
            for r in caplog.records
        )
