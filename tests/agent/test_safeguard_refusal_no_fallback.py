"""Claude bridge/relay safeguard refusals never switch model (t_997efe88).

Ace ruling 2026-10-02 21:28 + amendment 22:19: a turn the model's safeguards
refused on a bridge lane is never answered by another model, never retried
unchanged, and surfaces in the chat that owns the turn ("rephrase and retry").
A kanban worker's refused turn parks the card ``needs_input`` with the refusal
line as the reason, so the existing needs_input pager (#1612) posts it to the
card's origin chat.

The bridge egresses the refusal as HTTP 400 with the machine code
``safeguard_refusal`` (claude-bpx resultError.js / tuiRunner.js). The body
shapes below are the bridge's real ones (captured from claude-bpx
e2e-no-refusal-fallback S1, sdk lane; tui lane uses ``error.code``).
"""

from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
    is_safeguard_refusal,
)

SDK_BODY = {"error": {
    "message": "[safeguard_refusal: the request content was flagged; rephrase the request and retry on "
               "the SAME model (never switch model); model=claude-opus-5-5 category=cyber] Claude Code "
               "returned an error result: API Error: Opus 5.5's safeguards flagged this session",
    "type": "invalid_request_error",
    "error_code": "safeguard_refusal",
}}
TUI_BODY = {"error": {
    "type": "invalid_request_error",
    "code": "safeguard_refusal",
    "message": "the model safeguards flagged this turn; rephrase the request and retry on the same model",
}}


class _APIError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.response = SimpleNamespace(headers={})


@pytest.mark.parametrize("body", [SDK_BODY, TUI_BODY, SDK_BODY["error"], TUI_BODY["error"]],
                         ids=["sdk-error_code", "tui-code", "sdk-unwrapped", "tui-unwrapped"])
def test_bridge_safeguard_refusal_never_falls_back(body):
    # Prism r2: the unwrapped SDK body ({"type":..., "error_code":...}) is covered too.
    msg = (body.get("error") or body)["message"]
    err = _APIError(msg, status_code=400, body=body)
    c = classify_api_error(err, provider="claude-btpr", model="claude-opus-5-5")
    assert c.reason == FailoverReason.content_policy_blocked
    assert c.should_fallback is False, "a refused turn must never be answered by another model"
    assert c.retryable is False
    assert c.should_compress is False
    assert is_safeguard_refusal(c) is True


def test_plain_400_without_the_code_is_unchanged():
    # Negative control: the generic-400 path (format_error, fallback allowed) is
    # untouched for anything that does not carry the bridge's machine code.
    body = {"error": {"message": "bad request: something else", "type": "invalid_request_error"}}
    c = classify_api_error(_APIError(body["error"]["message"], status_code=400, body=body),
                           provider="claude-btpr", model="claude-opus-5-5")
    assert c.reason != FailoverReason.content_policy_blocked
    assert is_safeguard_refusal(c) is False


def test_other_content_policy_blocks_keep_their_fallback():
    # Non-bridge provider safety blocks keep today's contract (fallback allowed).
    e = Exception("This content was flagged for possible cybersecurity risk.")
    c = classify_api_error(e, provider="openai-codex", model="gpt-5.5")
    assert c.reason == FailoverReason.content_policy_blocked
    assert c.should_fallback is True
    assert is_safeguard_refusal(c) is False


def test_recovery_hint_names_same_model_retry():
    from agent import conversation_loop as cl
    assert "same model" in cl._SAFEGUARD_REFUSAL_RECOVERY_HINT
    assert "fallback" not in cl._SAFEGUARD_REFUSAL_RECOVERY_HINT.lower()
    assert "safeguard_refusal" in cl._SAFEGUARD_REFUSAL_RECOVERY_HINT


# ── kanban worker: refused turn -> needs_input with the refusal line ──────────

def _refused_result():
    return {
        "failed": True,
        "completed": False,
        "final_response": "⚠️ blocked",
        "failure_reason": "content_policy_blocked",
        "error_code": "safeguard_refusal",
        "error": "content_policy_blocked: HTTP 400: [safeguard_refusal: ... model=claude-opus-5-5 category=cyber] ...",
    }


def test_worker_refusal_blocks_card_needs_input(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_exit as kwe

    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="refusal probe card", assignee="daedalus")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    monkeypatch.setattr("agent.delegation_context.owns_kanban_worker_authority", lambda: True)

    assert kwe.block_on_safeguard_refusal(_refused_result()) is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        reason = kb._latest_block_reason(conn, tid)
    finally:
        conn.close()
    assert reason.startswith("safeguard_refusal:")
    assert "model=claude-opus-5-5 category=cyber" in reason
    assert "Rephrase" in reason


def test_worker_refusal_hook_ignores_other_failures(monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setattr("agent.delegation_context.owns_kanban_worker_authority", lambda: True)
    other = dict(_refused_result())
    other.pop("error_code")
    assert kwe.block_on_safeguard_refusal(other) is False
    assert kwe.block_on_safeguard_refusal({"failed": False}) is False
    assert kwe.block_on_safeguard_refusal(None) is False


def test_worker_refusal_hook_needs_worker_authority(monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setattr("agent.delegation_context.owns_kanban_worker_authority", lambda: False)
    assert kwe.block_on_safeguard_refusal(_refused_result()) is False


def test_worker_refusal_hook_needs_a_run_id(monkeypatch):
    # Prism P1 (round 1): block_task's run CAS is disabled when expected_run_id is
    # None, so no run id = no mutation.
    from hermes_cli import kanban_worker_exit as kwe
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setattr("agent.delegation_context.owns_kanban_worker_authority", lambda: True)
    called = []
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", lambda *a, **k: called.append(k) or True)
    for bad in (None, "", "not-a-number"):
        if bad is None:
            monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        else:
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", bad)
        assert kwe.block_on_safeguard_refusal(_refused_result()) is False, bad
    assert called == []


def test_worker_refusal_hook_stale_run_cannot_park_newer_run(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_exit as kwe
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="stale run probe", assignee="daedalus")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id + 999))  # not the live run
    monkeypatch.setattr("agent.delegation_context.owns_kanban_worker_authority", lambda: True)
    assert kwe.block_on_safeguard_refusal(_refused_result()) is False
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_partial_stream_safeguard_refusal_is_reraised_not_stubbed(monkeypatch):
    # Prism P1 (round 1): after deltas were sent, a stream error used to become a
    # finish_reason=length stub (-> content-filter fallback / continuation). A
    # safeguard refusal must re-raise so the exception path ends the turn. Driven
    # through the real streaming call (harness of test_partial_stream_finish_reason).
    from unittest.mock import MagicMock, patch
    from tests.run_agent.test_partial_stream_finish_reason import (
        _make_agent, _make_stream_chunk, PARTIAL_STREAM_STUB_ID,
    )

    class _Refused(Exception):
        def __init__(self):
            super().__init__(SDK_BODY["error"]["message"])
            self.status_code = 400
            self.body = SDK_BODY
            self.response = SimpleNamespace(headers={})

    def _stream(refusal):
        yield _make_stream_chunk(content="Looking at the ")
        raise refusal

    for make_err, expect_reraise in ((_Refused, True), (lambda: RuntimeError("peer closed connection"), False)):
        err = make_err()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, err=err, **kw: _stream(err)
        with patch("run_agent.AIAgent._create_request_openai_client", return_value=mock_client), \
                patch("run_agent.AIAgent._close_request_openai_client"):
            agent = _make_agent()
            agent._fire_stream_delta = lambda text: None
            agent._current_streamed_assistant_text = "Looking at the "
            monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
            if expect_reraise:
                with pytest.raises(_Refused):
                    agent._interruptible_streaming_api_call({})
            else:
                # Negative control: an ordinary mid-stream drop still becomes the stub.
                assert agent._interruptible_streaming_api_call({}).id == PARTIAL_STREAM_STUB_ID
