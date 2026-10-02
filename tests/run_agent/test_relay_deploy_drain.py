"""A relay DEPLOY-DRAIN 503 (claude-pool deploy-drain, t_826861ab) must be
waited out on the SAME model and provider, not answered with a model swap on
the same draining relay.

Wire shape (agent.log 2026-09-30 13:03:28, claude-bpr :18811):
    Error code: 503 - {'error': 'draining-for-deploy', 'retry_after': 15}
    Retry-After: 15
Before the fix: classified ``overloaded`` -> 1 retry -> failover to
claude-bpr/claude-opus-5-5 (same draining relay) -> same 503; the announce read
"(provider overloaded) ... unclassified error (hop unknown, sub unknown)".

Contract:
  * the drain 503 classifies ``relay_draining`` (exact body key match);
    any other 503 body keeps its old class;
  * drain ends inside the wait window -> same model retried, no fallback;
  * drain outlives the window -> failover SKIPS same-provider chain entries;
  * if a failover does happen, the announce names the deploy, at the relay.
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.error_classifier import FailoverReason, classify_api_error
from run_agent import AIAgent

BASE = "http://127.0.0.1:18811/v1"
DRAIN_BODY = {"error": "draining-for-deploy", "retry_after": 15}


def _status_error(status: int, body_json, *, retry_after: str | None = "15",
                  base: str = BASE) -> openai.APIStatusError:
    """Build the error the way the OpenAI SDK does from the relay's response:
    ``_make_status_error`` passes ``body=data.get("error", data)``, i.e. the
    bare string ``"draining-for-deploy"``, and the message embeds the dict."""
    req = httpx.Request("POST", f"{base}/chat/completions")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp = httpx.Response(status, request=req, json=body_json, headers=headers)
    msg = f"Error code: {status} - {body_json}"
    sdk_body = body_json.get("error", body_json) if isinstance(body_json, dict) else body_json
    cls = openai.InternalServerError if status >= 500 else openai.APIStatusError
    return cls(msg, response=resp, body=sdk_body)


def _drain_error(retry_after: str = "15") -> openai.APIStatusError:
    return _status_error(503, DRAIN_BODY, retry_after=retry_after)


# ── 1. classification ─────────────────────────────────────────────────────

def test_drain_503_classifies_relay_draining():
    r = classify_api_error(_drain_error(), provider="claude-bpr", model="claude-fable-5-1")
    assert r.reason is FailoverReason.relay_draining
    assert r.retryable is True
    assert r.should_rotate_credential is False


def test_drain_match_is_exact_key_not_substring():
    # A 503 whose free text merely mentions draining keeps the old class.
    r = classify_api_error(
        _status_error(503, {"error": {"message": "backend draining-for-deploy soon"}}),
        provider="claude-bpr", model="claude-fable-5-1")
    assert r.reason is not FailoverReason.relay_draining


@pytest.mark.parametrize("body,expected", [
    ({"error": "service unavailable"}, FailoverReason.overloaded),
    ({"error": "no eligible sub"}, FailoverReason.pool_exhausted),
])
def test_other_503_bodies_unchanged(body, expected):
    r = classify_api_error(_status_error(503, body), provider="claude-bpr",
                           model="claude-fable-5-1")
    assert r.reason is expected


# ── 2/3. the retry loop ───────────────────────────────────────────────────

def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="m", usage=None)


FB_CHAIN = [
    # Same relay, different model: a MODEL fallback. Useless for a drain.
    {"provider": "custom", "model": "claude-opus-5-5", "base_url": BASE},
    {"provider": "openrouter", "model": "fallback-model",
     "base_url": "https://openrouter.ai/api/v1"},
]


def _make_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k-abcdef123456",
            base_url=BASE,
            provider="custom",
            model="claude-fable-5-1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=FB_CHAIN,
        )
        agent.client = MagicMock()
        agent._api_max_retries = 3
        return agent


def _run(agent, fake_api_call, *, drain_wait_s):
    fb_client = MagicMock()
    fb_client.api_key = "k-abcdef123456"
    fb_client._custom_headers = None
    fb_client.default_headers = None

    def _resolve(provider, model=None, **kw):
        fb_client.base_url = kw.get("explicit_base_url") or BASE
        return fb_client, model

    activate = MagicMock(wraps=agent._try_activate_fallback)
    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_try_activate_fallback", activate),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        # create=True so the contract runs (RED) on a tree without the fix.
        patch("agent.fallback_wiring.relay_drain_wait_s", return_value=drain_wait_s,
              create=True),
        patch("agent.retry_utils.jittered_backoff", return_value=0.05),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
        patch("hermes_cli.model_normalize.normalize_model_for_provider",
              side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")
    return result, activate


def test_drain_that_ends_inside_window_retries_same_model_no_fallback():
    """13:03 shape: drain for a few attempts, then the relay serves again."""
    agent = _make_agent()
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if len(calls) <= 4:  # more drain 503s than the generic 3-attempt budget
            raise _drain_error(retry_after="0.05")
        return _mock_response("served by fable after the deploy")

    result, activate = _run(agent, fake_api_call, drain_wait_s=30.0)
    assert result["completed"] is True
    assert result["final_response"] == "served by fable after the deploy"
    assert calls == [("custom", "claude-fable-5-1")] * 5
    activate.assert_not_called()
    assert agent._fallback_activated is False


def test_drain_outliving_window_skips_same_provider_fallback():
    """Bound spent -> failover, but never to another model on the SAME relay."""
    agent = _make_agent()
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.provider == "custom":
            raise _drain_error(retry_after="0.05")
        return _mock_response("served by openrouter")

    result, activate = _run(agent, fake_api_call, drain_wait_s=0.3)
    assert result["final_response"] == "served by openrouter"
    assert ("custom", "claude-opus-5-5") not in calls
    assert all(c == ("custom", "claude-fable-5-1") for c in calls[:-1])
    assert calls[-1] == ("openrouter", "fallback-model")
    assert activate.call_args_list[0].kwargs.get("reason") is FailoverReason.relay_draining


# ── 4. rider + head label ─────────────────────────────────────────────────

def test_rider_classifies_drain_as_pool_pressure():
    text = str(_drain_error())
    assert fbe.classify_text(text, http_status=503) == "pool_pressure"


def _drain_ledger_row(from_provider="claude-bpr"):
    agent = SimpleNamespace(_pending_fallback_error=None, _current_turn_id=None,
                            session_id=None)
    fbe.stash_api_error(agent, _drain_error(), 503)
    return fbe.build_row(agent, "failover", from_provider=from_provider,
                         from_model="claude-fable-5-1", to_provider="openai-codex",
                         to_model="gpt-6-sol", reason=FailoverReason.relay_draining)


def test_rider_renders_relay_draining_for_deploy_at_the_relay():
    row = _drain_ledger_row()
    tz = _dt.timezone.utc
    row["ts"] = _dt.datetime(2026, 9, 30, 20, 3, 33, tzinfo=tz).timestamp()
    rider = fp.format_cause_rider(row, tz=tz)
    assert rider == "relay draining for deploy (at the relay), 20:03:33"
    for bad in ("hop unknown", "sub unknown", "unclassified", "overloaded"):
        assert bad not in rider


def _announce(reason, ledger_row):
    from agent.chat_completion_helpers import _emit_fallback_announce

    out = []
    ag = SimpleNamespace(
        _pending_quota_window=None, _pending_pool_scope=None,
        _pending_stream_error_reason=None, _last_fallback_announced=None,
        _last_fallback_event=None, context_compressor=None,
        _emit_status=out.append,
    )
    _emit_fallback_announce(ag, "claude-fable-5-1", "gpt-6-sol", "openai-codex",
                            old_provider="claude-bpr", record_event=False,
                            reason=reason, ledger_row=ledger_row)
    return out


def test_announce_end_to_end_after_bound_spent():
    row = _drain_ledger_row()
    tz = _dt.timezone.utc
    row["ts"] = _dt.datetime(2026, 9, 30, 20, 3, 33, tzinfo=tz).timestamp()
    with patch("agent.fallback_policy._hms",
               side_effect=lambda ts, _tz: _dt.datetime.fromtimestamp(float(ts), tz)):
        out = _announce(FailoverReason.relay_draining, row)
    assert out == [
        "🔄 Model fallback (relay deploying): claude-bpr/claude-fable-5-1 → "
        "openai-codex/gpt-6-sol — relay draining for deploy (at the relay), 20:03:33"
    ]
    for bad in ("hop unknown", "sub unknown", "provider overloaded", "unclassified"):
        assert bad not in out[0]


def test_head_label_even_when_relay_states_pool_pressure():
    """Sibling relay card stamps x-relay-error-class; the head still names the deploy."""
    row = _drain_ledger_row()
    row["class_source"] = "relay_header"
    row["hop"] = "relay"
    assert fp.head_label_override(row) == "relay deploying"
