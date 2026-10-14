"""A sub-pool relay 503 "no eligible sub" (the model is capped on every sub) must reach the
auxiliary fallback chain instead of failing the aux task outright (t_c55512d2: the goal judge
on claude-alr failed five turns in a row and auto-paused the goal while cpa was healthy)."""

from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as aux
import agent.auxiliary_wire  # noqa: F401  (openai import at collection, not inside the I/O guard)
from agent.auxiliary_health import fallback_candidate_quarantine_ttl


class _Relay503(Exception):
    status_code = 503


_CAPPED = (
    "Error code: 503 - {'error': \"no eligible sub for the requested model; this model's budget "
    "is capped on every subscription while other models are unaffected\"}"
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)
    aux._reset_aux_unhealthy_cache()
    yield
    aux._reset_aux_unhealthy_cache()


def test_pool_exhausted_classification():
    assert aux._is_pool_exhausted_error(_Relay503(_CAPPED))
    assert aux._is_pool_exhausted_error(Exception(_CAPPED))  # status lost on the exception
    assert not aux._is_pool_exhausted_error(_Relay503("Error code: 503 - upstream overloaded"))

    class _Relay400(Exception):
        status_code = 400

    assert not aux._is_pool_exhausted_error(_Relay400(_CAPPED))


def test_pool_exhausted_503_walks_main_fallback_chain():
    primary = MagicMock()
    primary.chat.completions.create.side_effect = _Relay503(_CAPPED)
    fallback = MagicMock()
    fallback.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"done": true, "reason": "ok"}'))])

    with patch("agent.auxiliary_client._get_cached_client", return_value=(primary, "claude-fable-5-1")), \
         patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("auto", "claude-fable-5-1", None, None, None)), \
         patch("agent.auxiliary_client._try_configured_fallback_chain", return_value=(None, None, "")), \
         patch("agent.auxiliary_client._try_main_fallback_chain",
               return_value=(fallback, "gpt-6.1-sol", "fallback_providers[3](cpa)")) as main_chain:
        result = aux.call_llm(task="goal_judge", messages=[{"role": "user", "content": "judge"}])

    assert result.choices[0].message.content.startswith('{"done": true')
    assert main_chain.call_args.kwargs["reason"] == "sub pool exhausted"
    # Model-scoped cap: only the failed model is skipped, not the whole provider.
    assert main_chain.call_args.kwargs["failed_model"] == "claude-fable-5-1"


def test_pool_exhausted_candidate_hold_is_short():
    # The cap is per model but the quarantine is per endpoint: a 10-minute hold would hide
    # the lane's other (healthy) models from every aux task.
    assert fallback_candidate_quarantine_ttl("sub pool exhausted") == 60.0
