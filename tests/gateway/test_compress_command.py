"""Tests for gateway /compress user-facing messaging."""

import asyncio
import threading
from collections import OrderedDict
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str = "/compress") -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]


def _make_runner(history: list[dict[str, str]]):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = history
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store._save = MagicMock()
    runner._session_db = None
    # Merged runtime-resolution path (_resolve_session_agent_runtime) consults
    # per-session /model overrides before falling back to _resolve_gateway_model.
    # With no override present the tests' patched _resolve_gateway_model wins;
    # stub the override machinery so the real GatewayRunner instance
    # (object.__new__, no __init__) doesn't AttributeError / return a mock model.
    runner._session_model_overrides = {}
    runner._rehydrate_session_model_override = MagicMock(return_value=None)
    runner._session_key_for_source = MagicMock(
        return_value=build_session_key(_make_source())
    )
    return runner


@pytest.mark.asyncio
async def test_compress_command_works_when_auto_compaction_disabled():
    """compression.enabled: false disables *automatic* compaction only.

    The gateway /compress handler has never gated on the flag — pin that
    contract (every manual-compress surface must allow manual compression
    regardless of the auto toggle, #64438) and the force=True cooldown
    bypass that manual compression relies on."""
    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "compressed summary"},
        history[-1],
    ]
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.compression_enabled = False
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    # Explicit non-lock-skip: MagicMock getattr would return a truthy mock.
    agent_instance._compression_skipped_due_to_lock = False

    def _estimate(messages, **_kwargs):
        return 100 if messages == history else 60

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "disabled" not in result.lower()
    assert "Compressed:" in result
    agent_instance._compress_context.assert_called_once()
    assert agent_instance._compress_context.call_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_compress_command_surfaces_aux_model_failure_even_when_recovered():
    """When the user's configured ``auxiliary.compression.model`` errors out
    but compression recovers by retrying on the main model, /compress must
    STILL inform the user.  Silent recovery hides broken config the user
    needs to fix."""
    history = _make_history()
    # Compressed transcript — normal successful compression, no placeholder.
    compressed = [
        history[0],
        {"role": "assistant", "content": "summary via main model"},
        history[-1],
    ]
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    # Fallback placeholder was NOT used — recovery succeeded.
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_fallback_used = False
    agent_instance.context_compressor._last_summary_dropped_count = 0
    agent_instance.context_compressor._last_summary_error = None
    # But the configured aux model DID fail before the retry succeeded.
    agent_instance.context_compressor._last_aux_model_failure_model = (
        "gemini-3-flash-preview"
    )
    agent_instance.context_compressor._last_aux_model_failure_error = (
        "404 model not found: gemini-3-flash-preview"
    )
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    agent_instance._compression_skipped_due_to_lock = False

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 60
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_estimate),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Compression succeeded
    assert "Compressed:" in result
    # No ⚠️ warning (that's reserved for dropped-turns case)
    assert "⚠️" not in result
    # But there IS an info note about the broken aux model
    assert "ℹ️" in result
    assert "gemini-3-flash-preview" in result
    assert "404" in result
    assert "auxiliary.compression.model" in result
    # The user's context is explicitly called out as intact
    assert "intact" in result
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_in_place_skips_destructive_rewrite():
    """In-place compaction (compression.in_place / #38763) persists via
    archive_and_compact() inside _compress_context — the previous active rows
    are soft-archived and the compacted set inserted. Calling
    rewrite_transcript() afterwards would invoke
    replace_messages(active_only=False), DELETEing the just-archived rows
    (silent data loss, #61145). The handler must skip the rewrite and still
    report success."""
    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "compacted summary"},
        history[-1],
    ]
    runner = _make_runner(history)
    runner._session_db = object()
    session_entry = runner.session_store.get_or_create_session.return_value
    runner.session_store.rewrite_transcript = MagicMock()

    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    # In-place compaction: session_id is UNCHANGED but marked as a success.
    agent_instance._last_compaction_in_place = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    agent_instance._compression_skipped_due_to_lock = False

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 60
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Compressed:" in result
    # The destructive rewrite must NOT run — archive_and_compact() already
    # persisted, and rewrite_transcript would wipe the archived rows.
    runner.session_store.rewrite_transcript.assert_not_called()
    assert session_entry.session_id == "sess-1"
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_preserves_platform_and_gateway_session_key():
    """The temporary compression agent must carry the originating source's
    platform and stable gateway session key, matching a normal gateway turn.
    Without them ``_session_source_for_agent`` falls back to a default "cli"
    host source, so an external context engine misattributes the retained
    transcript tail and later duplicates it on resume (#50422)."""
    history = _make_history()
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = False

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance) as mock_agent,
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())

    assert mock_agent.call_count == 1
    _, kwargs = mock_agent.call_args
    # Platform preserved as the live turn's config key (TELEGRAM -> "telegram"),
    # not the unbound "cli"/"local" fallback.
    assert kwargs.get("platform") == "telegram"
    # Stable gateway session key preserved, identical to a normal gateway turn.
    assert kwargs.get("gateway_session_key") == runner._session_key_for_source(_make_source())
    assert kwargs["gateway_session_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resident_state", "expect_live_route"),
    (
        ("cached", True),
        ("running", True),
        ("absent", False),
        ("raises", False),
        ("incomplete", False),
        ("missing-key", False),
    ),
)
async def test_compress_command_uses_complete_resident_route_or_config_fallback(
    resident_state,
    expect_live_route,
):
    """The one-shot compressor follows a complete live route, atomically.

    A fallback can leave the resident agent on a healthy provider/model while
    persisted config still names the congested primary. Building the temporary
    compression agent from config crosses back to that stale route. Provider,
    model, endpoint, and credential must move as one live-runtime unit; absent,
    unreadable, or incomplete resident state keeps the resolved config pair.
    """
    history = _make_history()
    runner = _make_runner(history)
    session_key = runner._session_key_for_source(_make_source())

    live_runtime = {
        "provider": "claude-apx-7",
        "model": "claude-fable-5",
        "base_url": "http://127.0.0.1:18807/v1",
        "api_key": "live-key",
        "api_mode": "anthropic_messages",
    }
    resident_agent = MagicMock()
    resident_agent.max_tokens = 8192
    if resident_state == "raises":
        resident_agent._current_main_runtime.side_effect = RuntimeError("stale agent")
    elif resident_state == "incomplete":
        resident_agent._current_main_runtime.return_value = {
            **live_runtime,
            "model": "",
        }
    elif resident_state == "missing-key":
        resident_agent._current_main_runtime.return_value = {
            **live_runtime,
            "api_key": "",
        }
    else:
        resident_agent._current_main_runtime.return_value = dict(live_runtime)
    runner._running_agents = {}
    runner._agent_cache = OrderedDict()
    if resident_state == "running":
        runner._running_agents[session_key] = resident_agent
    elif resident_state != "absent":
        runner._agent_cache[session_key] = (
            resident_agent,
            ("live-signature",),
            len(history),
            "sess-1",
        )
    runner._agent_cache_lock = threading.Lock()
    runner._evict_cached_agent = MagicMock()

    temp_agent = MagicMock()
    temp_agent.shutdown_memory_provider = MagicMock()
    temp_agent.close = MagicMock()
    temp_agent._cached_system_prompt = ""
    temp_agent.tools = None
    temp_agent.context_compressor.has_content_to_compress.return_value = True
    temp_agent.context_compressor._last_compress_aborted = False
    temp_agent.context_compressor._last_summary_fallback_used = False
    temp_agent.context_compressor._last_summary_dropped_count = 0
    temp_agent.context_compressor._last_summary_error = None
    temp_agent.context_compressor._last_aux_model_failure_model = None
    temp_agent.context_compressor._last_aux_model_failure_error = None
    temp_agent.session_id = "sess-1"
    temp_agent._compress_context.return_value = (list(history), "")
    temp_agent._compression_skipped_due_to_lock = False
    temp_agent._last_compaction_in_place = False
    temp_agent._last_compaction_persist_failed = False

    stale_credential_pool = MagicMock(name="stale_config_credential_pool")
    stale_route_fields = {
        "requested_provider": "claude-apr",
        "command": "stale-config-command",
        "args": ["--stale-config-arg"],
        "credential_pool": stale_credential_pool,
    }
    config_runtime = {
        "provider": "claude-apr",
        "base_url": "http://127.0.0.1:18801/v1",
        "api_key": "config-key",
        "api_mode": "anthropic_messages",
        "max_tokens": 4096,
        **stale_route_fields,
    }
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=config_runtime),
        patch("gateway.run._resolve_gateway_model", return_value="claude-opus-5"),
        patch("run_agent.AIAgent", return_value=temp_agent) as mock_agent,
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())

    _, kwargs = mock_agent.call_args
    expected_runtime = live_runtime if expect_live_route else {
        **config_runtime,
        "model": "claude-opus-5",
    }
    assert (kwargs["provider"], kwargs["model"]) == (
        expected_runtime["provider"],
        expected_runtime["model"],
    )
    assert kwargs["base_url"] == expected_runtime["base_url"]
    assert kwargs["api_key"] == expected_runtime["api_key"]
    assert kwargs["api_mode"] == expected_runtime["api_mode"]
    assert kwargs["max_tokens"] == (8192 if expect_live_route else 4096)
    for field, value in stale_route_fields.items():
        if expect_live_route:
            assert field not in kwargs
        else:
            assert kwargs[field] == value


@pytest.mark.asyncio
async def test_compress_command_passes_tool_messages_to_compressor():
    """Tool results must reach _compress_context (#3854).

    Filtering the transcript to user/assistant-only starved the
    compressor's tool-result pruning — tool messages are usually the bulk
    of the context.
    """
    history = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "BIG RESULT " * 50, "tool_call_id": "t1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "np"},
    ]
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())

    args, _kwargs = agent_instance._compress_context.call_args
    passed = args[0]
    roles = [m.get("role") for m in passed]
    assert "tool" in roles, f"tool messages filtered out: {roles}"
    # Assistant tool_calls stubs (content=None) must survive too, or the
    # tool message would dangle without its call.
    assert any(m.get("tool_calls") for m in passed), "assistant tool_calls stub dropped"




@pytest.mark.asyncio
async def test_compress_command_multiplexed_runs_under_profile_secret_scope(tmp_path):
    """Manual /compress must install the source profile's secret scope.

    Multiplexed gateways resolve credentials fail-closed (Workstream A):
    ``get_secret`` raises ``UnscopedSecretError`` on any read outside a
    ``set_secret_scope`` block. The agent turn is scoped by ``_run_agent``'s
    wrapper, but slash-command dispatch is not — manual /compress reached the
    compressor's provider resolution unscoped and died with
    ``get_secret('OPENROUTER_BASE_URL') called with no profile secret scope
    active``. The credential read happens inside the executor hop, so this
    also pins that the handler uses the contextvar-preserving executor
    (``_run_in_executor_with_context``), not a bare ``run_in_executor``.
    """
    from agent import secret_scope as ss

    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "compressed summary"},
        history[-1],
    ]
    runner = _make_runner(history)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        multiplex_profiles=True,
    )
    profile_home = tmp_path / "profiles" / "milo"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "OPENROUTER_BASE_URL=https://scoped.example/v1\n"
    )
    runner._resolve_profile_home_for_source = MagicMock(return_value=profile_home)

    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_fallback_used = False
    agent_instance.context_compressor._last_summary_dropped_count = 0
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.session_id = "sess-1"
    agent_instance._compression_skipped_due_to_lock = False

    seen: dict[str, str | None] = {}

    def _compress(*_args, **_kwargs):
        # Runs in the executor thread — exactly where the aux client
        # resolves provider credentials. Fail-closed get_secret raises
        # here unless the profile scope survived the thread hop.
        seen["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (compressed, "")

    agent_instance._compress_context.side_effect = _compress

    ss.set_multiplex_active(True)
    try:
        with (
            patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
            patch("gateway.run._resolve_gateway_model", return_value="test-model"),
            patch("run_agent.AIAgent", return_value=agent_instance),
            patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
        ):
            result = await runner._handle_compress_command(_make_event())
    finally:
        ss.set_multiplex_active(False)
        runner._shutdown_executor()

    assert "failed" not in result.lower(), result
    assert seen["base_url"] == "https://scoped.example/v1"
    runner._resolve_profile_home_for_source.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_single_profile_skips_profile_resolution():
    """Multiplexing off → the scope wrapper is a transparent pass-through.

    Single-profile gateways must not pay the profile-resolution path (and
    ``_resolve_profile_home_for_source`` assumes multiplex config exists) —
    mirrors the gating contract of ``_run_agent``'s wrapper.
    """
    history = _make_history()
    runner = _make_runner(history)
    runner._resolve_profile_home_for_source = MagicMock()
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = False

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())

    runner._resolve_profile_home_for_source.assert_not_called()
    runner._shutdown_executor()


@pytest.mark.asyncio
async def test_compress_command_cleanup_does_not_block_event_loop():
    """Manual /compress must not run agent teardown on the gateway event loop.

    #53175 offloaded session-expiry, hygiene, and shutdown cleanup, but the
    manual /compress finally still called ``_cleanup_agent_resources`` inline.
    A slow ``agent.close()`` there freezes the whole loop and stops the
    runtime-status heartbeat from advancing — the same wedge class as the
    original incident.

    Observation must happen from a side thread: if cleanup blocks the event
    loop, an ``await``-based waiter cannot sample ticks until close returns,
    which falsely looks healthy after the block ends.
    """
    import time

    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "compressed summary"},
        history[-1],
    ]
    runner = _make_runner(history)

    close_started = threading.Event()
    release_close = threading.Event()

    def slow_close():
        close_started.set()
        release_close.wait(timeout=5)

    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = slow_close
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_fallback_used = False
    agent_instance.context_compressor._last_summary_dropped_count = 0
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    agent_instance._compression_skipped_due_to_lock = False
    agent_instance._session_messages = None

    ticks = {"n": 0}
    stop = threading.Event()
    observed = {}

    async def _heartbeat():
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.005)

    def _observer():
        # threading.Event wait does not need the event loop. Sample ticks
        # while close() is still held so an on-loop teardown is visible.
        if not close_started.wait(timeout=5):
            observed["error"] = "close() never started"
            release_close.set()
            return
        baseline = ticks["n"]
        time.sleep(0.12)
        observed["ticks_during_block"] = ticks["n"] - baseline
        release_close.set()

    hb = asyncio.create_task(_heartbeat())
    observer = threading.Thread(target=_observer, name="compress-cleanup-observer", daemon=True)
    observer.start()

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        result = await runner._handle_compress_command(_make_event())

    observer.join(timeout=5)
    stop.set()
    await hb
    runner._shutdown_executor()

    assert "Compressed:" in result
    assert "error" not in observed, observed.get("error")
    assert observed.get("ticks_during_block", 0) >= 5, (
        "event loop was blocked during manual /compress cleanup: only "
        f"{observed.get('ticks_during_block')} ticks while agent.close() was running"
    )


# --------------------------------------------------------------------------
# fork-only coverage (parity merge 2026-08-07)
# Kept verbatim from the fork blob: the other side's suite-wide
# prune removed these, but they guard fork-owned behavior.
# --------------------------------------------------------------------------


def _ack_agent(history):
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = False
    return agent_instance


def _make_large_history(n: int):
    """n alternating rows — used to cross the progress-ack size threshold."""
    rows = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        rows.append({"role": role, "content": f"msg {i}"})
    return rows


def _make_tool_heavy_history() -> list[dict]:
    """A stored transcript shaped like a real tool-heavy session: chat turns
    plus tool-result rows and a contentless assistant tool-call turn — the
    rows the gateway /compress chat-only projection excludes."""
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "content": "BIG TOOL OUTPUT " * 50, "tool_call_id": "t1"},
        {"role": "tool", "content": "MORE TOOL OUTPUT " * 50, "tool_call_id": "t2"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]


def _tool_heavy_chat(history: list[dict]) -> list[dict]:
    return [
        {"role": m.get("role"), "content": m.get("content")}
        for m in history
        if m.get("role") in {"user", "assistant"} and m.get("content")
    ]


def _wire_ack_adapter(runner):
    """Attach a mock adapter + metadata helper so the interim ack has a send path."""
    from unittest.mock import AsyncMock

    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    return adapter


@pytest.mark.asyncio
async def test_compress_command_appends_warning_when_compression_aborts():
    """When the auxiliary summariser fails and the compressor ABORTS (returns
    messages unchanged), /compress must append a visible ⚠️ warning to its
    reply telling the user nothing was dropped and how to retry. Otherwise
    the failure is silently logged and the user has no idea why nothing
    happened."""
    history = _make_history()
    # Abort path: compressor returns the input messages unchanged.
    compressed = list(history)
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    # Simulate compression aborting (force=True bypassed cooldown but the
    # aux LLM is genuinely broken).
    agent_instance.context_compressor._last_compress_aborted = True
    agent_instance.context_compressor._last_summary_fallback_used = False
    agent_instance.context_compressor._last_summary_dropped_count = 0
    agent_instance.context_compressor._last_summary_error = (
        "404 model not found: gemini-3-flash-preview"
    )
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    agent_instance._compression_skipped_due_to_lock = False

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 100
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_estimate),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate),
    ):
        result = await runner._handle_compress_command(_make_event())

    # A clearly-marked warning must be appended.
    assert "⚠️" in result
    assert "Compression aborted" in result
    # Underlying error must surface so users can fix their config.
    assert "404 model not found" in result
    # User must be told nothing was dropped — the whole point of the
    # new behavior is no silent data loss.
    assert "No messages were dropped" in result
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_explains_when_token_estimate_rises():
    history = _make_history()
    compressed = [
        history[0],
        {"role": "hermes", "content": "Dense summary that still counts as more tokens."},
        history[-1],
    ]
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")
    agent_instance._compression_skipped_due_to_lock = False

    def _chat_est(messages, **_kwargs):
        # chat rises despite fewer messages → denser-summary note
        return 100 if messages != compressed else 120

    def _full_est(messages, **_kwargs):
        return 500 if messages != compressed else 480

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_chat_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_full_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Compressed: 4 → 3 messages" in result
    assert "Chat size: ~100 → ~120 tokens" in result
    assert "Full request size: ~500 → ~480 tokens" in result
    assert "denser summaries" in result
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_full_line_is_estimate_when_no_measured_value():
    """With no prior live turn (last_prompt_tokens == 0) the Full request size
    line falls back to the char-based estimate for both before and after,
    flagged with ~ and an 'estimated' note."""
    history = _make_history()
    compressed = [history[0], {"role": "hermes", "content": "s"}, history[-1]]
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 0  # no live turn yet
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.context_length = 1_000_000
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")

    def _chat_est(messages, **_kwargs):
        return 100 if messages != compressed else 80

    def _full_est(messages, **_kwargs):
        return 500 if messages != compressed else 400

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_chat_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_full_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Chat size: ~100 → ~80 tokens" in result
    assert "Full request size: ~500 → ~400 tokens" in result
    assert "estimated — no live request yet" in result
    assert "last live request" not in result


@pytest.mark.asyncio
async def test_compress_command_full_line_uses_real_measured_before_when_available():
    """When the session has a real, provider-measured last_prompt_tokens (the
    same number /usage shows), the Full request size line must use it as the
    'before' WITHOUT a ~ prefix (it's real, not an estimate), while the
    'after' stays an estimate. The Chat size line is the separate chat-only
    figure. This keeps the two metrics distinct and labelled."""
    history = _make_history()
    compressed = [
        history[0],
        {"role": "hermes", "content": "Dense summary."},
        history[-1],
    ]
    runner = _make_runner(history)
    # Real provider-measured context from a prior live turn.
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 290_310
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.context_length = 1_000_000
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (compressed, "")

    def _chat_est(messages, **_kwargs):
        return 29_521 if messages != compressed else 27_300

    def _full_est(messages, **_kwargs):
        # full-request estimate; the real before should override this number
        return 295_000 if messages != compressed else 265_000

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_chat_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_full_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Chat line = chat-only estimate.
    assert "Chat size: ~29,521 → ~27,300 tokens" in result
    assert "excludes system, tools, tool results" in result
    # Full line = REAL before (no ~) → estimate after.
    assert "Full request size: 290,310 → ~265,000 tokens" in result
    assert "before = last live request" in result
    # The char-based full-request *before* estimate must NOT be used when a
    # real count exists.
    assert "295,000" not in result


@pytest.mark.asyncio
async def test_compress_command_granular_failure_degrades_to_two_line():
    """If the granular stats build fails, /compress must fall back to the
    two-line enhanced form — never crash, never suppress feedback."""
    history = _make_tool_heavy_history()
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.name = "builtin"
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance.session_id = "sess-1"

    chat = _tool_heavy_chat(history)
    compressed = [dict(chat[0]), {"role": "assistant", "content": "s"}, dict(chat[-1])]

    def _compress(messages, *_args, **_kwargs):
        agent_instance.session_id = "sess-2"
        return compressed, ""

    agent_instance._compress_context.side_effect = _compress
    agent_instance._compression_skipped_due_to_lock = False

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch(
            "agent.compaction_stats.build_hygiene_stats",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Fell back to the enhanced two-line form.
    assert "Compressed:" in result and "stored messages" in result
    assert "Dropped: 3 stored tool/system messages" in result
    assert "Messages:" not in result  # granular block absent


@pytest.mark.asyncio
async def test_compress_command_granular_model_line_shows_session_reasoning():
    """The /compress granular Model line carries the session-truthful r:<effort>
    — the session /reasoning override, not the global config default (and not
    nothing at all: the manual path historically omitted r: entirely, unlike
    the auto-compaction announce)."""
    history = _make_tool_heavy_history()
    chat = _tool_heavy_chat(history)
    compressed = [
        dict(chat[0]),
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary of older turns"},
        dict(chat[-1]),
    ]
    runner = _make_runner(history)
    # Session-scoped /reasoning override — must appear as r:high on the Model line.
    _skey = runner._session_key_for_source(_make_source())
    runner._session_reasoning_overrides = {_skey: {"enabled": True, "effort": "high"}}
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.name = "builtin"
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance.session_id = "sess-1"

    def _compress(messages, *_args, **_kwargs):
        agent_instance.session_id = "sess-2"
        return compressed, ""

    agent_instance._compress_context.side_effect = _compress

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k", "provider": "test-prov"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Model: test-prov/test-model · r:high" in result


@pytest.mark.asyncio
async def test_compress_command_lcm_engine_wire_first_context_line():
    """Ace's live case (2026-07-02): an LCM session whose /compress granular
    block measured the STORED transcript but labeled it 'Context:' / 'Removed
    from live context' — overstating wire savings (~689K→~37K, 'freed 651K')
    against a real 303K request. The fix: with a REAL provider-measured
    before-count, the prominent Context line becomes the WIRE story (measured
    303,201 → next-request estimate); the archive totals are demoted into the
    Removed header as token-est; and the duplicate Full-request line is skipped.
    One number story — footer and /compress can no longer disagree.
    """
    history = _make_tool_heavy_history()
    chat = _tool_heavy_chat(history)
    compressed = [
        dict(chat[0]),
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary of older turns"},
        dict(chat[-1]),
    ]
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 303_201  # real provider-measured (the wire truth)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.name = "lcm"  # ← LCM engine (Ace's case)
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.compression_in_place = True
    agent_instance._last_compaction_in_place = True  # LCM compacts in place
    agent_instance.session_id = "sess-1"

    def _compress(messages, *_args, **_kwargs):
        return compressed, ""  # in-place: session_id unchanged, rewrite still fires

    agent_instance._compress_context.side_effect = _compress

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k", "provider": "test-prov"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
    ):
        result = await runner._handle_compress_command(_make_event())

    # E2E proof — print the full delivered message.
    print("\n───── delivered /compress message (LCM) ─────\n" + result + "\n────────────────────────────────────────────")

    # Wire-first: the prominent token line is the WIRE story with the REAL
    # measured before (303,201), not the archive estimate.
    assert "Context:   303,201 → ~" in result
    assert "before measured, after next-request estimate" in result
    # Archive totals demoted into the Removed header, labeled token-est.
    assert "Removed from stored transcript" in result
    assert "token-est reclaimed from archive" in result
    # The replacement-cost line, when present, carries the stored-basis wording.
    if "Replacement cost" in result:
        assert "kept in transcript" in result
    # The old stand-alone 'Stored transcript:' line and the misleading live-wire
    # wording must both be absent from the manual wire-first path.
    assert "Stored transcript:" not in result
    assert "Removed from live context" not in result
    assert "kept in context" not in result
    # The duplicate Full-request line is SKIPPED — the wire truth (303,201) is
    # already the Context line above; no double-reporting.
    assert "Full request size: 303,201" not in result
    # LCM recovery pointer.
    assert "lcm.db" in result
    # LCM compacts in place (_rewritten=True) → CASE D cannot fire; guard against
    # a future regression where the in-place path falls through to the CASE C
    # no-op message.
    assert "No changes" not in result


@pytest.mark.asyncio
async def test_compress_command_overrides_stale_resolver_identity():
    """If the resolver already supplies platform/gateway_session_key, the
    construction must (a) not raise "got multiple values for keyword argument",
    and (b) let the originating-source identity win — a stale/placeholder
    resolver value must not defeat the attribution fix."""
    history = _make_history()
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = False

    # Resolver injects a WRONG platform and a stale session key.
    runtime = {"api_key": "test-key", "platform": "discord", "gateway_session_key": "stale-key"}
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance) as mock_agent,
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())  # must not raise

    assert mock_agent.call_count == 1
    _, kwargs = mock_agent.call_args
    # Source-derived identity overrides the stale resolver values, passed once.
    assert kwargs["platform"] == "telegram"
    assert kwargs["gateway_session_key"] == runner._session_key_for_source(_make_source())


@pytest.mark.asyncio
async def test_compress_command_passes_session_db_and_persists_rotated_session():
    """session_db must be wired into the /compress temp agent so that
    _compress_context can actually rotate the session and persist the
    compressed transcript — without it compression is a silent no-op."""
    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "compressed summary"},
        history[-1],
    ]
    runner = _make_runner(history)
    runner._session_db = object()
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.compression_in_place = False
    agent_instance.session_id = "sess-1"

    def _compress(messages, *_args, **_kwargs):
        agent_instance.session_id = "sess-2"
        return compressed, ""

    agent_instance._compress_context.side_effect = _compress
    agent_instance._compression_skipped_due_to_lock = False

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 60
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance) as mock_agent_cls,
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Compressed:" in result
    mock_agent_cls.assert_called_once()
    assert mock_agent_cls.call_args.kwargs["session_db"] is runner._session_db
    runner.session_store._save.assert_called_once()
    runner.session_store.rewrite_transcript.assert_called_once_with(
        "sess-2", compressed
    )
    runner.session_store.update_session.assert_called_once_with(
        build_session_key(_make_source()), last_prompt_tokens=0
    )
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_persist_failure_surfaces_retry_not_noop():
    """CASE D regression: the compressor DID compact the transcript in memory
    (the returned list is shorter than the input), but the DB write that would
    persist it was rolled back -- a locked state.db / contended write -- so the
    session was NOT rotated and nothing was written. The reply must say the
    save FAILED and is retryable, and must NOT print the bland
    'No changes: transcript preserved' that a genuine no-op prints, nor
    fabricate a shrink."""
    history = _make_tool_heavy_history()
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    # The persist-failure signal: rotation's child-session create was rolled
    # back (DB locked), so the compacted result never reached the store.
    agent_instance._last_compaction_persist_failed = True
    agent_instance.session_id = "sess-1"  # rolled back to parent -> no rotation

    # Real compression shape: a genuinely SHORTER list (work was done in memory)
    chat = _tool_heavy_chat(history)
    compacted = [
        dict(chat[0]),
        {"role": "assistant", "content": "[CONTEXT COMPACTION -- REFERENCE ONLY] summary"},
        dict(chat[-1]),
    ]

    def _compress(messages, *_args, **_kwargs):
        # In-memory compaction succeeded (shorter), but session_id is UNCHANGED
        # because create_session failed and rolled back to the parent.
        return compacted, ""

    agent_instance._compress_context.side_effect = _compress

    def _est(messages, **_kwargs):
        return 100

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    print("\n----- delivered /compress message (persist-fail) -----\n" + result + "\n------------------------------------------------------")

    # Must NOT masquerade as a benign no-op.
    assert "No changes: transcript preserved" not in result
    # Must say the SAVE failed and it is retryable, and that nothing was lost.
    assert "could not be saved" in result.lower() or "couldn't be saved" in result.lower()
    assert "retry" in result.lower() or "/compress" in result
    assert "database" in result.lower() or "locked" in result.lower() or "busy" in result.lower()
    # Must NOT fabricate a shrink: the store is untouched, next request resends
    # the same context.
    assert "unchanged" in result.lower()
    # Nothing was persisted -> last_prompt_tokens must NOT be zeroed, transcript
    # must NOT be overwritten.
    runner.session_store.update_session.assert_not_called()


@pytest.mark.asyncio
async def test_compress_command_renders_granular_breakdown_on_real_compression():
    """When the rewrite happened AND the compressor actually changed the
    transcript, /compress renders the full granular CompactionStats block
    (Messages / Context / Removed-buckets with the tool sub-split) — the same
    renderer the auto-compaction announce uses — instead of the two-line form.
    """
    history = _make_tool_heavy_history()
    chat = _tool_heavy_chat(history)
    # Real compression shape: first chat row kept + 1 summary + last row kept.
    compressed = [
        dict(chat[0]),
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary of older turns"},
        dict(chat[-1]),
    ]
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.context_compressor.name = "builtin"
    agent_instance.context_compressor._last_compress_aborted = False
    agent_instance.context_compressor._last_summary_error = None
    agent_instance.context_compressor._last_aux_model_failure_model = None
    agent_instance.context_compressor._last_aux_model_failure_error = None
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance.session_id = "sess-1"

    def _compress(messages, *_args, **_kwargs):
        agent_instance.session_id = "sess-2"  # rotation → rewrite
        return compressed, ""

    agent_instance._compress_context.side_effect = _compress

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k", "provider": "test-prov"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Granular block present: Messages axis + wire Context line + removed buckets.
    assert "Messages:" in result
    # The summary row must be classified as summary, not "kept chat"
    # (built-in SUMMARY_PREFIX marker → kept 2 recent chat + 1 summary).
    assert "kept 2 recent chat + 1 summary" in result
    # Wire-first (Ace 2026-07-02): with a REAL provider-measured before-count
    # (453,542), the prominent token line is the WIRE story — measured before →
    # next-request estimate — NOT the archive estimate.
    assert "Context:   453,542 → ~" in result
    assert "before measured, after next-request estimate" in result
    # The archive totals are demoted into the Removed header, explicitly labeled
    # token-est so they can't be read as request-size savings.
    assert "Removed from stored transcript" in result
    assert "token-est reclaimed from archive" in result
    # The old stand-alone 'Stored transcript:' prominent line is gone in wire mode.
    assert "Stored transcript:" not in result
    assert "Removed from live context" not in result
    # Tool sub-split names the tool-result rows explicitly.
    assert "tool-result message" in result
    # Model line carries provider/model.
    assert "Model: test-prov/test-model" in result
    # Recovery pointer for the rotated (non-LCM) store.
    assert "previous transcript preserved: sess-1" in result
    # The duplicate Full-request line is SKIPPED — the wire story is already on
    # the Context line above (no double-reporting of 453,542).
    assert "Full request size: 453,542" not in result
    # And it must never claim "No changes".
    assert "No changes" not in result


@pytest.mark.asyncio
async def test_compress_command_reports_noop_without_success_banner():
    history = _make_history()
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = False

    def _chat_est(messages, **_kwargs):
        return 100  # chat size unchanged

    def _full_est(messages, **_kwargs):
        return 500  # full request size

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_chat_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_full_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "No changes from compression" in result
    assert "Compressed:" not in result
    # Chat line uses the unchanged form and excludes-system framing.
    assert "Chat size: ~100 tokens (unchanged" in result
    assert "excludes system, tools, tool results" in result
    # Full line still shown (estimate, no live turn yet).
    assert "Full request size: ~500 → ~500 tokens" in result
    assert "includes chat, system, tools, tool results" in result
    agent_instance.shutdown_memory_provider.assert_called_once()
    agent_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_compress_command_surfaces_lock_skip():
    """When _compress_context skips due to a concurrent lock, the gateway
    handler must surface a clear message, not the misleading no-op text."""
    history = _make_history()
    runner = _make_runner(history)
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")
    agent_instance._compression_skipped_due_to_lock = "pid=99999"

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "Compression already in progress" in result
    assert "pid=99999" in result
    assert "No changes from compression" not in result


@pytest.mark.asyncio
async def test_compress_command_tool_heavy_noop_chat_reports_compaction():
    """CASE A regression: chat-only compression no-ops (chat already compact)
    but the transcript rewrite drops the stored tool/system rows. The reply
    must report the compaction — with the dropped-rows detail — instead of
    the self-contradicting 'No changes ... 453,542 → ~32,036'."""
    history = _make_tool_heavy_history()
    chat = _tool_heavy_chat(history)
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542  # real provider-measured
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance.session_id = "sess-1"

    def _compress(messages, *_args, **_kwargs):
        # Chat no-op, but the session ROTATES → transcript rewrite happens.
        agent_instance.session_id = "sess-2"
        return list(messages), ""

    agent_instance._compress_context.side_effect = _compress

    def _chat_est(messages, **_kwargs):
        # chat-only rows small; non-chat rows big
        if all(m.get("role") in {"user", "assistant"} for m in messages):
            return 100
        return 420_000  # the non-chat (tool) rows

    def _full_est(messages, **_kwargs):
        return 453_000 if len(messages) == len(history) else 32_000

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_chat_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_full_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Headline reports the stored-transcript compaction, never "No changes".
    assert "No changes" not in result
    assert "Compacted stored transcript: 7 → 4 messages" in result
    # Chat axis honestly reported as already compact.
    assert "already compact, kept verbatim" in result
    # Dropped rows accounted: 3 non-chat rows (1 contentless + 2 tool).
    assert "Dropped: 3 stored tool/system messages" in result
    assert "420,000 tokens reclaimed" in result
    # Full line uses the REAL before (no ~) and the compressed-basis after.
    assert "Full request size: 453,542 → ~32,000 tokens" in result
    # Rewrite happened → stored token count reset.
    runner.session_store.update_session.assert_called_once_with(
        build_session_key(_make_source()), last_prompt_tokens=0
    )


@pytest.mark.asyncio
async def test_compress_command_true_noop_preserves_measured_tokens():
    """CASE C regression: when NO rewrite happens (no rotation, in-place off)
    the reply must say the transcript was preserved, the full-request line
    must say 'unchanged' (the next request resends the same context — an
    'after' measured over the chat-only list would fabricate a shrink), and
    last_prompt_tokens must NOT be zeroed (it is still the only real
    provider-measured figure)."""
    history = _make_tool_heavy_history()
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance.session_id = "sess-1"  # never rotates → no rewrite
    # No persist failure: MagicMock would otherwise return a truthy Mock for
    # this attr and spuriously trigger the CASE D retry message (#44794).
    agent_instance._last_compaction_persist_failed = False

    def _compress(messages, *_args, **_kwargs):
        return list(messages), ""

    agent_instance._compress_context.side_effect = _compress

    def _est(messages, **_kwargs):
        return 100

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Honest no-op: transcript preserved, with full composition.
    assert "No changes: transcript preserved (7 messages: 4 chat + 3 tool/system)" in result
    # No dropped-rows claim, no fabricated shrink.
    assert "Dropped:" not in result
    assert "reclaimed" not in result
    assert "Full request size: 453,542 tokens (unchanged" in result
    # The real provider-measured count survives for the next /compress or /usage.
    runner.session_store.update_session.assert_not_called()
    # Transcript must not have been overwritten either.
    runner.session_store.rewrite_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_compress_command_true_noop_still_says_preserved_not_failure():
    """Companion to the persist-failure test: a GENUINE no-op (nothing to
    compress, persist_failed False) must still print the calm
    'No changes: transcript preserved' -- the CASE D failure wording must NOT
    leak onto the honest no-op path."""
    history = _make_tool_heavy_history()
    runner = _make_runner(history)
    session_entry = runner.session_store.get_or_create_session.return_value
    session_entry.last_prompt_tokens = 453_542
    agent_instance = MagicMock()
    agent_instance.shutdown_memory_provider = MagicMock()
    agent_instance.close = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.compression_in_place = False
    agent_instance._last_compaction_in_place = False
    agent_instance._last_compaction_persist_failed = False  # genuine no-op
    agent_instance.session_id = "sess-1"
    agent_instance._compression_skipped_due_to_lock = False

    def _compress(messages, *_args, **_kwargs):
        return list(messages), ""  # unchanged -> true no-op

    agent_instance._compress_context.side_effect = _compress

    def _est(messages, **_kwargs):
        return 100

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=_est),
        patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_est),
    ):
        result = await runner._handle_compress_command(_make_event())

    assert "No changes: transcript preserved (7 messages: 4 chat + 3 tool/system)" in result
    assert "could not be saved" not in result.lower()
    assert "database" not in result.lower()


@pytest.mark.asyncio
async def test_compress_sends_progress_ack_for_large_session():
    """A session at/above the threshold gets an interim '⏳ compressing…' ack
    BEFORE the blocking compress, so a slow compress isn't mistaken for a hang."""
    from gateway.slash_commands import _COMPRESS_PROGRESS_ACK_MIN_MESSAGES

    history = _make_large_history(_COMPRESS_PROGRESS_ACK_MIN_MESSAGES)
    runner = _make_runner(history)
    adapter = _wire_ack_adapter(runner)
    agent_instance = _ack_agent(history)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=500),
    ):
        await runner._handle_compress_command(_make_event())

    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    # the count is interpolated and the wording reads as in-progress, not done
    assert "Compressing" in sent_text
    assert str(_COMPRESS_PROGRESS_ACK_MIN_MESSAGES) in sent_text.replace(",", "")


@pytest.mark.asyncio
async def test_compress_stays_silent_for_small_session():
    """Below the threshold a compress finishes fast — no ack noise."""
    from gateway.slash_commands import _COMPRESS_PROGRESS_ACK_MIN_MESSAGES

    history = _make_large_history(_COMPRESS_PROGRESS_ACK_MIN_MESSAGES - 1)
    runner = _make_runner(history)
    adapter = _wire_ack_adapter(runner)
    agent_instance = _ack_agent(history)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent_instance),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=500),
    ):
        await runner._handle_compress_command(_make_event())

    adapter.send.assert_not_awaited()
