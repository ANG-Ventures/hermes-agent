"""Background-review forks must never fail the parent's kanban task.

Regression test for the misattribution found 2026-08-07 on parity card
``t_c348a9f0``.

The background-review fork is constructed with a hardcoded
``max_iterations=16`` (``agent/background_review.py``). It inherits the parent
process environment, including ``HERMES_KANBAN_TASK``. When the *fork*
exhausted its own 16-iteration budget, ``finalize_turn`` read
``HERMES_KANBAN_TASK`` from the environment and called
``_record_task_failure(outcome="timed_out")`` against the **parent** card.

Observed damage: the parent worker was healthy and mid-merge, but the card
accrued consecutive failures and tripped its failure circuit with
``"Iteration budget exhausted (16/16)"`` — a number matching neither
``agent.max_turns`` (300), ``goals.max_turns`` (20), nor the profile's
``delegation.max_iterations`` (100). That mismatch is exactly what made it so
hard to trace: the ceiling belonged to a different agent entirely.

The fork is already distinguishable — ``background_review`` stamps
``_memory_write_origin = "background_review"`` on the review agent.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.turn_finalizer import finalize_turn


class _BudgetAgent:
    """Minimal agent that lands in the budget-exhausted branch."""

    def __init__(self, *, max_iterations, memory_write_origin=None):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=0, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        # Only the review fork carries this attribute.
        if memory_write_origin is not None:
            self._memory_write_origin = memory_write_origin

    def _handle_max_iterations(self, messages, api_call_count):
        return "summary from extra call"

    def _emit_status(self, *_a, **_kw):
        pass

    def _safe_print(self, *_a, **_kw):
        pass

    def _save_trajectory(self, *_a, **_kw):
        pass

    def _cleanup_task_resources(self, *_a, **_kw):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _format_turn_completion_explanation(self, _reason):
        return ""

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kw):
        pass


def _finalize(agent, *, api_call_count):
    return finalize_turn(
        agent,
        final_response=None,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
        _pending_verification_response=None,
    )


@pytest.fixture(autouse=True)
def _kanban_task_env(monkeypatch):
    """Both cases run with a parent kanban task in the environment."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent123")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])


def test_background_review_fork_does_not_fail_parent_task():
    """The 16-iteration review fork must not touch the parent's card."""
    agent = _BudgetAgent(max_iterations=16, memory_write_origin="background_review")

    with patch("hermes_cli.kanban_db._record_task_failure") as rec, \
         patch("hermes_cli.kanban_db.connect"):
        _finalize(agent, api_call_count=16)

    assert rec.call_count == 0, (
        "background-review fork recorded a failure against the parent kanban "
        f"task: {rec.call_args_list}"
    )


def test_real_worker_still_records_failure():
    """Negative control: a genuine worker must still trip the circuit."""
    agent = _BudgetAgent(max_iterations=300)

    with patch("hermes_cli.kanban_db._record_task_failure") as rec, \
         patch("hermes_cli.kanban_db.connect"):
        _finalize(agent, api_call_count=300)

    assert rec.call_count == 1, (
        "real worker budget exhaustion must still advance the dispatcher's "
        "consecutive-failure circuit"
    )
    assert rec.call_args.kwargs.get("outcome") == "timed_out"
