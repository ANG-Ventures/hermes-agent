"""The transcript-persistence skip log names the real failure class (t_438a9c74).

A bridge ``safeguard_refusal`` comes back as HTTP 400; on a long session the gateway's overflow
verdict (400 + history > 50) also fires, so the skip is taken. The log line must say it was a
safeguard refusal, not a context overflow, and the persistence verdict must stay unchanged.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from gateway.run_turn import GatewayTurnMixin


def _classify(agent_result, history_len):
    entry = SimpleNamespace(session_id="sess-1")
    return GatewayTurnMixin._hmwa_classify_turn_failure(None, agent_result, [{}] * history_len, entry)


def test_safeguard_refusal_skip_log_says_refusal_not_overflow(caplog):
    refusal = {
        "failed": True, "completed": False,
        "error": "content_policy_blocked: HTTP 400: [safeguard_refusal: request refused model=x category=y]",
        "failure_reason": "content_policy_blocked", "error_code": "safeguard_refusal",
    }
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        verdict = _classify(refusal, history_len=120)
    # Persistence behaviour is unchanged: the overflow verdict still skips the transcript write.
    assert verdict == (True, False, True)
    lines = [r.getMessage() for r in caplog.records if "Skipping transcript persistence" in r.getMessage()]
    assert len(lines) == 1
    assert "safeguard-refusal" in lines[0] and "context-overflow failure" not in lines[0]

    caplog.clear()
    overflow = {"failed": True, "error": "HTTP 400: prompt is too long", "failure_reason": "context_overflow"}
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        assert _classify(overflow, history_len=120) == (True, False, True)
    assert any("context-overflow failure" in r.getMessage() for r in caplog.records)
