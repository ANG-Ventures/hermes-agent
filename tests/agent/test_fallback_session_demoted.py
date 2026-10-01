"""t_693aa2e5: the bridge's interactive-session demotion (HTTP 409
``tui_history_diverged``, claude-bpx bridge/src/tuiRunner.js) rendered
"unclassified error" (145 rows / 7 d, 3 reason variants).

The bridge withdrew this conversation's resident session, sticky until its idle
TTL, so the route is unavailable to this session: pool_pressure, not a bad
request and not quota. The rider names the bridge's stated reason.

Fixtures are the wire bodies from agent.log on 2026-09-30 (provider claude-btpr,
``openai.ConflictError: Error code: 409 - {'error': {...}}``).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.agent_runtime_helpers import extract_api_error_context

BASE = "the replayed history no longer matches this interactive session; it has been demoted"
REASONS = (
    "a held tool call expired before its result arrived",          # err_hash 4c24ebefe5, n=110
    "history diverged from the ledger",                            # err_hash 131496bd4f, n=33
    "the turn ended on an upstream error row after the session took it",  # 4550fc9d30, n=2
)


def _conflict(message: str) -> openai.ConflictError:
    data = {"error": {"type": "invalid_request_error", "code": "tui_history_diverged",
                      "message": message}}
    req = httpx.Request("POST", "http://127.0.0.1:18811/v1/chat/completions")
    resp = httpx.Response(409, json=data, request=req)
    # OpenAI SDK raises with body=data["error"] (the inner object).
    return openai.ConflictError(f"Error code: 409 - {data}", response=resp, body=data["error"])


def _failover_row(message: str) -> dict:
    agent = SimpleNamespace(session_id="s1", _current_turn_id="s1:1")
    err = _conflict(message)
    fbe.stash_api_error(agent, err, 409, extract_api_error_context(err))
    return fbe.build_row(agent, "failover", from_provider="claude-btpr",
                         from_model="claude-opus-5-5", to_provider="claude-bpr",
                         to_model="claude-opus-5-5", reason="format_error")


@pytest.mark.parametrize("reason", REASONS)
def test_each_sample_classifies_and_names_its_reason(reason):
    msg = f"{BASE} ({reason})"
    row = _failover_row(msg)
    assert row["trigger_class"] == "pool_pressure"
    assert row["http_status"] == 409
    cause = fp._cause_phrase(row)
    assert cause == f"interactive session demoted ({reason})"
    assert fp.head_label_override(row) == "session demoted"
    rider = fp.format_cause_rider(row)
    assert rider.startswith(f"interactive session demoted ({reason})")
    assert "unclassified error" not in rider


@pytest.mark.parametrize("reason", REASONS)
def test_classify_text_alone(reason):
    assert fbe.classify_text(f"{BASE} ({reason})", http_status=409) == "pool_pressure"


def test_truncated_head_keeps_the_reason_it_has():
    # err_head is capped at ERR_HEAD_MAX; a cut inside the parentheses still
    # names what survived instead of falling back to a bare phrase.
    msg = f"{BASE} ({'x' * 200})"
    row = {"trigger_class": "pool_pressure", "err_head": msg[:fbe.ERR_HEAD_MAX]}
    assert fp._cause_phrase(row).startswith("interactive session demoted (xxx")


def test_no_reason_renders_bare_phrase():
    row = {"trigger_class": "pool_pressure", "err_head": BASE}
    assert fp._cause_phrase(row) == fp.SESSION_DEMOTED_CAUSE


def test_other_pool_pressure_unchanged():
    row = {"trigger_class": "pool_pressure", "err_head": "pool at capacity"}
    assert fp._cause_phrase(row) == "pool at capacity"
    assert fp.head_label_override(row) is None
