"""Gateway intentional-silence token behavior."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from gateway.response_filters import (
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _event():
    return MessageEvent(
        text="side chatter",
        source=_source(),
        message_id="msg-42",
    )


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-silent",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_blank_and_prose_mentions_are_not_silence():
    assert not is_intentional_silence_response("")
    assert not is_intentional_silence_response("Use NO_REPLY when no answer is needed.")
    assert not is_intentional_silence_response("The reply was [SILENT], intentionally.")


def test_failed_agent_result_never_counts_as_intentional_silence():
    assert is_intentional_silence_agent_result({"failed": False}, "NO_REPLY")
    assert not is_intentional_silence_agent_result({"failed": True}, "NO_REPLY")


@pytest.mark.asyncio
async def test_silence_token_suppresses_delivery_but_preserves_transcript(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "[SILENT]",
        "messages": [
            {"role": "user", "content": "side chatter"},
            {"role": "assistant", "content": "[SILENT]"},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
    })

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response == ""
    appended = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert {"role": "assistant", "content": "[SILENT]"}.items() <= appended[-1].items()
    assert [msg["role"] for msg in appended if msg.get("role") in {"user", "assistant"}] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_empty_success_still_gets_empty_response_warning(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": ""},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
    })

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "no response was generated" in response


@pytest.mark.asyncio
async def test_prose_mentioning_silence_token_is_delivered(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    text = "Use [SILENT] when no answer is needed."
    runner._run_agent = AsyncMock(return_value={
        "final_response": text,
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": text},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
    })

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response == text


# --------------------------------------------------------------------------
# Internal-event turns (bg-process completions, restore replays) resolve
# silence with the autonomous rule; human turns stay exact-marker.
# --------------------------------------------------------------------------

_NOTE_THEN_MARKER = (
    "That's the #176 CI watch confirming green — already merged on that "
    "result. No new information.\n\nNO_REPLY"
)


def _internal_event():
    return MessageEvent(
        text="[Background process proc_1 finished with exit code 0]",
        source=_source(),
        internal=True,
    )


def _agent_result(text, *, failed=False):
    return {
        "final_response": text,
        "messages": [
            {"role": "user", "content": "completion"},
            {"role": "assistant", "content": text},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": failed,
    }


def test_internal_flag_selects_autonomous_rule_and_failure_still_wins():
    ok = {"failed": False}
    assert is_intentional_silence_agent_result(ok, _NOTE_THEN_MARKER, internal=True)
    assert not is_intentional_silence_agent_result(ok, _NOTE_THEN_MARKER)
    assert not is_intentional_silence_agent_result(
        {"failed": True}, _NOTE_THEN_MARKER, internal=True,
    )
    # Exact markers are silence under both rules.
    assert is_intentional_silence_agent_result(ok, "NO_REPLY", internal=True)


@pytest.mark.asyncio
async def test_internal_event_note_plus_marker_is_suppressed(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value=_agent_result(_NOTE_THEN_MARKER))

    response = await runner._handle_message_with_agent(
        _internal_event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response == ""
    # Transcript still records the assistant turn (delivery-only decision).
    appended = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert appended[-1].get("content") == _NOTE_THEN_MARKER


@pytest.mark.asyncio
async def test_human_turn_note_plus_marker_is_delivered(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value=_agent_result(_NOTE_THEN_MARKER))

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response == _NOTE_THEN_MARKER


@pytest.mark.asyncio
async def test_internal_event_failed_turn_with_marker_is_delivered(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(
        return_value=_agent_result(_NOTE_THEN_MARKER, failed=True),
    )

    response = await runner._handle_message_with_agent(
        _internal_event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response and "confirming green" in response


@pytest.mark.asyncio
async def test_internal_event_marker_buried_mid_sentence_is_delivered(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    text = (
        "CI for #176 failed on slice 4: the NO_REPLY filter test regressed, "
        "see run 123 for the traceback."
    )
    runner._run_agent = AsyncMock(return_value=_agent_result(text))

    response = await runner._handle_message_with_agent(
        _internal_event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response == text


@pytest.mark.asyncio
async def test_agent_end_hook_includes_model_and_provider(monkeypatch, tmp_path):
    """Gateway hooks receive the actual model/provider for post-turn routing."""
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "done",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "done"},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
        "model": "gpt-5.6-terra",
        "provider": "openai-codex",
    })

    await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    end_context = next(
        call.args[1]
        for call in runner.hooks.emit.await_args_list
        if call.args[0] == "agent:end"
    )
    assert end_context["model"] == "gpt-5.6-terra"
    assert end_context["provider"] == "openai-codex"
