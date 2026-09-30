"""Clarify prompt-send TIMEOUT must not tear down the registration.

Sibling of test_approval_send_timeout_ambiguity.py, same boundary rule, same
live physics: send_clarify's scheduling future can hit its 15s deadline while
the clarify card HAS already posted (late connector ack). The old caller
treated any exception — including the timeout — as a definitive failure and
ran clear_session(), so the user answered a rendered card whose registration
was already gone.

Contract under test: TimeoutError is AMBIGUOUS (possibly delivered) — the
registration must stay armed (clear_session NOT called) and the caller must
proceed to the bounded wait (disposition None). A definitive error
(SendResult success=False, non-timeout exception, or no future) keeps
today's teardown + sentinel behavior.
"""

import concurrent.futures
from unittest.mock import MagicMock

from gateway.run import _clarify_send_disposition, _clarify_send_then_wait

SENTINEL = "[clarify prompt could not be delivered]"


class _Result:
    def __init__(self, success, error=None):
        self.success = success
        self.error = error


def test_timeout_keeps_registration_armed_and_proceeds_to_wait():
    fut = MagicMock()
    fut.result.side_effect = concurrent.futures.TimeoutError()
    clarify_mod = MagicMock()
    disposition = _clarify_send_disposition(
        fut, session_key="sk", clarify_mod=clarify_mod
    )
    assert disposition is None, (
        "a send timeout aborted the clarify wait — this is the "
        "cleared-session-under-a-rendered-card bug (card posted, ack late); "
        "ambiguous must fall through to wait_for_response"
    )
    clarify_mod.clear_session.assert_not_called()


def test_successful_send_proceeds_to_wait():
    fut = MagicMock()
    fut.result.return_value = _Result(True)
    clarify_mod = MagicMock()
    assert (
        _clarify_send_disposition(fut, session_key="sk", clarify_mod=clarify_mod)
        is None
    )
    clarify_mod.clear_session.assert_not_called()


def test_definitive_error_result_tears_down_and_aborts():
    fut = MagicMock()
    fut.result.return_value = _Result(False, "relay prompt op unavailable")
    clarify_mod = MagicMock()
    assert (
        _clarify_send_disposition(fut, session_key="sk", clarify_mod=clarify_mod)
        == SENTINEL
    )
    clarify_mod.clear_session.assert_called_once_with("sk")


def test_non_timeout_exception_tears_down_and_aborts():
    fut = MagicMock()
    fut.result.side_effect = RuntimeError("loop unavailable")
    clarify_mod = MagicMock()
    assert (
        _clarify_send_disposition(fut, session_key="sk", clarify_mod=clarify_mod)
        == SENTINEL
    )
    clarify_mod.clear_session.assert_called_once_with("sk")


def test_missing_future_tears_down_and_aborts():
    clarify_mod = MagicMock()
    assert (
        _clarify_send_disposition(None, session_key="sk", clarify_mod=clarify_mod)
        == SENTINEL
    )
    clarify_mod.clear_session.assert_called_once_with("sk")


# --- Caller-path contract: the disposition feeds the bounded wait ---------


def test_ambiguous_send_reaches_wait_for_response():
    """The full caller contract, not just the classifier: on a send timeout
    the flow must proceed to wait_for_response with the generated clarify_id
    and the configured timeout — the late reply to the (probably rendered)
    card resolves through that wait."""
    fut = MagicMock()
    fut.result.side_effect = concurrent.futures.TimeoutError()
    clarify_mod = MagicMock()
    clarify_mod.get_clarify_timeout.return_value = 600
    clarify_mod.wait_for_response.return_value = "user picked B"

    out = _clarify_send_then_wait(
        fut, clarify_id="cid123", session_key="sk", clarify_mod=clarify_mod
    )

    assert out == "user picked B"
    clarify_mod.clear_session.assert_not_called()
    clarify_mod.wait_for_response.assert_called_once_with("cid123", timeout=600.0)


def test_sent_reaches_wait_for_response():
    fut = MagicMock()
    fut.result.return_value = _Result(True)
    clarify_mod = MagicMock()
    clarify_mod.get_clarify_timeout.return_value = 600
    clarify_mod.wait_for_response.return_value = "answer"

    assert (
        _clarify_send_then_wait(
            fut, clarify_id="cid123", session_key="sk", clarify_mod=clarify_mod
        )
        == "answer"
    )
    clarify_mod.wait_for_response.assert_called_once_with("cid123", timeout=600.0)


def test_definitive_failure_never_waits():
    fut = MagicMock()
    fut.result.return_value = _Result(False, "relay prompt op unavailable")
    clarify_mod = MagicMock()

    assert (
        _clarify_send_then_wait(
            fut, clarify_id="cid123", session_key="sk", clarify_mod=clarify_mod
        )
        == SENTINEL
    )
    clarify_mod.wait_for_response.assert_not_called()
    clarify_mod.clear_session.assert_called_once_with("sk")


def test_no_response_returns_timeout_sentinel():
    fut = MagicMock()
    fut.result.return_value = _Result(True)
    clarify_mod = MagicMock()
    clarify_mod.get_clarify_timeout.return_value = 600
    clarify_mod.wait_for_response.return_value = None

    assert (
        _clarify_send_then_wait(
            fut, clarify_id="cid123", session_key="sk", clarify_mod=clarify_mod
        )
        == "[user did not respond within 10m]"
    )


# --- Definitive failures keep their diagnostic detail in the log ----------


def test_failed_send_exception_detail_is_logged(caplog):
    fut = MagicMock()
    fut.result.side_effect = RuntimeError("loop unavailable")
    clarify_mod = MagicMock()
    with caplog.at_level("WARNING", logger="gateway.run"):
        _clarify_send_disposition(fut, session_key="sk", clarify_mod=clarify_mod)
    assert "loop unavailable" in caplog.text


def test_failed_send_result_error_detail_is_logged(caplog):
    fut = MagicMock()
    fut.result.return_value = _Result(False, "relay prompt op unavailable")
    clarify_mod = MagicMock()
    with caplog.at_level("WARNING", logger="gateway.run"):
        _clarify_send_disposition(fut, session_key="sk", clarify_mod=clarify_mod)
    assert "relay prompt op unavailable" in caplog.text


def _gateway_legacy_callback(calls, *, send_ok=True):
    """A legacy (per-question) clarify callback driven by the REAL gateway
    producer, the shape gateway/run.py's _clarify_callback_sync has."""
    def cb(question, choices, multi_select=False):
        calls.append(question)
        fut = MagicMock()
        fut.result.return_value = _Result(send_ok, None if send_ok else "down")
        clarify_mod = MagicMock()
        clarify_mod.get_clarify_timeout.return_value = 600
        clarify_mod.wait_for_response.return_value = None  # user never answered
        return _clarify_send_then_wait(
            fut, clarify_id="cid", session_key="sk", clarify_mod=clarify_mod
        )
    return cb


def test_batch_stops_on_first_gateway_timeout():
    """t_d3750098: a 5-question form on Telegram timed out on EVERY question
    in turn (5 x 600 s = 3003 s with the turn blocked) because the batch loop
    only recognised the CLI's TIMEOUT_RESPONSE, not the gateway's
    "[user did not respond within Nm]". One timeout must end the batch."""
    import json
    from tools.clarify_tool import clarify_tool

    calls = []
    result = json.loads(clarify_tool(
        "", questions=[{"question": f"Q{i}?"} for i in range(5)],
        callback=_gateway_legacy_callback(calls),
    ))
    assert calls == ["Q0?"]
    assert result["timed_out"] is True
    assert [r["user_response"] for r in result["responses"]] == [""] * 5


def test_batch_stops_on_undeliverable_prompt():
    import json
    from tools.clarify_tool import clarify_tool

    calls = []
    result = json.loads(clarify_tool(
        "", questions=[{"question": "A?"}, {"question": "B?"}],
        callback=_gateway_legacy_callback(calls, send_ok=False),
    ))
    assert calls == ["A?"]
    assert result["timed_out"] is True


def test_batch_keeps_answer_that_merely_starts_like_a_sentinel():
    """Only the exact producer sentinels end the batch. A typed answer that
    begins with the same words is an answer (Prism P1 on #1510)."""
    import json
    from tools.clarify_tool import clarify_tool

    answers = iter([
        "The user did not provide a response to my email; send a reminder",
        "[user did not respond within 10m] is what the old bot said",
    ])
    calls = []

    def cb(question, choices, multi_select=False):
        calls.append(question)
        return next(answers)

    result = json.loads(clarify_tool(
        "", questions=[{"question": "A?"}, {"question": "B?"}], callback=cb,
    ))
    assert calls == ["A?", "B?"]
    assert "timed_out" not in result
    assert [r["user_response"] for r in result["responses"]] == [
        "The user did not provide a response to my email; send a reminder",
        "[user did not respond within 10m] is what the old bot said",
    ]

