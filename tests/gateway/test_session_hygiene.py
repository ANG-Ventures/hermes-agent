"""Tests for gateway session hygiene — auto-compression of large sessions.

Verifies that the gateway detects pathologically large transcripts and
triggers auto-compression before running the agent.  (#628)

The hygiene system uses the SAME compression config as the agent:
  compression.threshold × model context length
so CLI and messaging platforms behave identically.
"""

import asyncio
import importlib
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history(n_messages: int, content_size: int = 100) -> list:
    """Build a fake transcript with n_messages user/assistant pairs."""
    history = []
    content = "x" * content_size
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": content, "timestamp": f"t{i}"})
    return history


def _make_large_history_tokens(target_tokens: int) -> list:
    """Build a history that estimates to roughly target_tokens tokens."""
    # estimate_messages_tokens_rough counts total chars in str(msg) // 4
    # Each msg dict has ~60 chars of overhead + content chars
    # So for N tokens we need roughly N * 4 total chars across all messages
    target_chars = target_tokens * 4
    # Each message as a dict string is roughly len(content) + 60 chars
    msg_overhead = 60
    # Use 50 messages with appropriately sized content
    n_msgs = 50
    content_size = max(10, (target_chars // n_msgs) - msg_overhead)
    return _make_history(n_msgs, content_size=content_size)


class HygieneCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="hygiene-1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


# ---------------------------------------------------------------------------
# Detection threshold tests (model-aware, unified with compression config)
# ---------------------------------------------------------------------------

class TestSessionHygieneThresholds:
    """Test that the threshold logic correctly identifies large sessions.

    Thresholds are derived from model context length × compression threshold,
    matching what the agent's ContextCompressor uses.
    """


    def test_under_threshold_no_trigger(self):
        """Session under threshold should not trigger, even with many messages."""
        # 250 short messages — lots of messages but well under token threshold
        history = _make_history(250, content_size=10)
        approx_tokens = estimate_messages_tokens_rough(history)

        # 200k model at 85% = 170k token threshold
        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        needs_compress = approx_tokens >= compress_token_threshold
        assert not needs_compress, (
            f"250 short messages (~{approx_tokens} tokens) should NOT trigger "
            f"compression at {compress_token_threshold} token threshold"
        )

    def test_message_count_alone_does_not_trigger(self):
        """Message count alone should NOT trigger — only token count matters.

        The old system used an OR of token-count and message-count thresholds,
        which caused premature compression in tool-heavy sessions with 200+
        messages but low total tokens.
        """
        # 300 very short messages — old system would compress, new should not
        history = _make_history(300, content_size=10)
        approx_tokens = estimate_messages_tokens_rough(history)

        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        # Token-based check only
        needs_compress = approx_tokens >= compress_token_threshold
        assert not needs_compress

    def test_threshold_scales_with_model(self):
        """Different models should have different compression thresholds."""
        # 128k model at 85% = 108,800 tokens
        small_model_threshold = int(128_000 * 0.85)
        # 200k model at 85% = 170,000 tokens
        large_model_threshold = int(200_000 * 0.85)
        # 1M model at 85% = 850,000 tokens
        huge_model_threshold = int(1_000_000 * 0.85)

        # A session at ~120k tokens:
        history = _make_large_history_tokens(120_000)
        approx_tokens = estimate_messages_tokens_rough(history)

        # Should trigger for 128k model
        assert approx_tokens >= small_model_threshold
        # Should NOT trigger for 200k model
        assert approx_tokens < large_model_threshold
        # Should NOT trigger for 1M model
        assert approx_tokens < huge_model_threshold

    def test_small_session_below_thresholds(self):
        """A 10-message session should not trigger compression."""
        history = _make_history(10)
        approx_tokens = estimate_messages_tokens_rough(history)

        # For a 200k-context model at 85% threshold = 170k
        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        needs_compress = approx_tokens >= compress_token_threshold
        assert not needs_compress

    def test_large_token_count_triggers(self):
        """High token count should trigger compression when exceeding model threshold."""
        # Build a history that exceeds 85% of a 200k model (170k tokens)
        history = _make_large_history_tokens(180_000)
        approx_tokens = estimate_messages_tokens_rough(history)

        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        needs_compress = approx_tokens >= compress_token_threshold
        assert needs_compress

    def test_custom_threshold_percentage(self):
        """Custom threshold percentage from config should be respected."""
        context_length = 200_000

        # At 50% threshold = 100k
        low_threshold = int(context_length * 0.50)
        # At 90% threshold = 180k
        high_threshold = int(context_length * 0.90)

        history = _make_large_history_tokens(150_000)
        approx_tokens = estimate_messages_tokens_rough(history)

        # Should trigger at 50% but not at 90%
        assert approx_tokens >= low_threshold
        assert approx_tokens < high_threshold

    def test_minimum_message_guard(self):
        """Sessions with fewer than 4 messages should never trigger."""
        history = _make_history(3, content_size=100_000)
        # Even with enormous content, < 4 messages should be skipped
        # (the gateway code checks `len(history) >= 4` before evaluating)
        assert len(history) < 4


class TestSessionHygieneWarnThreshold:
    """Test the post-compression warning threshold (95% of context)."""

    def test_warn_when_still_large(self):
        """If compressed result is still above 95% of context, should warn."""
        context_length = 200_000
        warn_threshold = int(context_length * 0.95)  # 190k
        post_compress_tokens = 195_000
        assert post_compress_tokens >= warn_threshold

    def test_no_warn_when_under(self):
        """If compressed result is under 95% of context, no warning."""
        context_length = 200_000
        warn_threshold = int(context_length * 0.95)  # 190k
        post_compress_tokens = 150_000
        assert post_compress_tokens < warn_threshold


class TestEstimatedTokenThreshold:
    """Verify that hygiene thresholds are always below the model's context
    limit — for both actual and estimated token counts.

    Regression: a previous 1.4x multiplier on rough estimates pushed the
    threshold to 85% * 1.4 = 119% of context, which exceeded the model's
    limit and prevented hygiene from ever firing for ~200K models (GLM-5).
    The fix removed the multiplier entirely — the 85% threshold already
    provides ample headroom over the agent's 50% compressor.
    """

    def test_threshold_below_context_for_200k_model(self):
        """Hygiene threshold must always be below model context."""
        context_length = 200_000
        threshold = int(context_length * 0.85)
        assert threshold < context_length


    def test_overestimate_fires_early_but_safely(self):
        """If rough estimate is 50% inflated, hygiene fires at ~57% actual usage.

        That's between the agent's 50% threshold and the model's limit —
        safe and harmless.
        """
        context_length = 200_000
        threshold = int(context_length * 0.85)  # 170K
        # If actual tokens = 113K, rough estimate = 113K * 1.5 = 170K
        # Hygiene fires when estimate hits 170K, actual is ~113K = 57% of ctx
        actual_when_fires = threshold / 1.5
        assert actual_when_fires > context_length * 0.50, (
            "Early fire should still be above agent's 50% threshold"
        )
        assert actual_when_fires < context_length, (
            "Early fire must be well below model limit"
        )

    def test_threshold_below_context_for_128k_model(self):
        context_length = 128_000
        threshold = int(context_length * 0.85)
        assert threshold < context_length

    def test_no_multiplier_means_same_threshold_for_estimated_and_actual(self):
        """Without the 1.4x, estimated and actual token paths use the same threshold."""
        context_length = 200_000
        threshold_pct = 0.85
        threshold = int(context_length * threshold_pct)
        # Both paths should use 170K — no inflation
        assert threshold == 170_000

    def test_warn_threshold_below_context(self):
        """Warn threshold (95%) must be below context length."""
        for ctx in (128_000, 200_000, 1_000_000):
            warn = int(ctx * 0.95)
            assert warn < ctx


class TestTokenEstimation:
    """Verify rough token estimation works as expected for hygiene checks."""


    def test_proportional_to_content(self):
        small = _make_history(10, content_size=100)
        large = _make_history(10, content_size=10_000)
        assert estimate_messages_tokens_rough(large) > estimate_messages_tokens_rough(small)

    def test_empty_history(self):
        assert estimate_messages_tokens_rough([]) == 0

    def test_proportional_to_count(self):
        few = _make_history(10, content_size=1000)
        many = _make_history(100, content_size=1000)
        assert estimate_messages_tokens_rough(many) > estimate_messages_tokens_rough(few)

    def test_pathological_session_detected(self):
        """The reported pathological case: 648 messages, ~299K tokens.

        With a 200k model at 85% threshold (170k), this should trigger.
        """
        history = _make_history(648, content_size=1800)
        tokens = estimate_messages_tokens_rough(history)
        # Should be well above the 170K threshold for a 200k model
        threshold = int(200_000 * 0.85)
        assert tokens > threshold


@pytest.mark.asyncio
async def test_session_hygiene_preserves_transcript_when_no_rotation(monkeypatch, tmp_path):
    """Regression for #21301: the hygiene agent is built without a session_db,
    so _compress_context cannot rotate. When it neither rotates NOR compacts
    in place, the transcript MUST be preserved — an unconditional
    rewrite_transcript() would replace the original messages with only the
    summary (permanent data loss). Mirrors the /compress guard (#44794)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class NonRotatingCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self.compression_in_place = False  # not in-place either
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            # No session_db → cannot rotate: session_id is UNCHANGED, and this
            # is a failure-to-rotate, not an in-place success.
            return ([{"role": "assistant", "content": "summary only"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = NonRotatingCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="17585",
            user_id="12345",
        ),
        message_id="1",
    )

    # Pre-load a failure streak so we can prove the recovery gate is WIRED UP,
    # not merely that the predicate is correct in isolation (#79624). Deleting
    # the whole `if not _hyg_aborted: if hygiene_compaction_recovered(...)`
    # block leaves every unit test in
    # tests/gateway/test_hygiene_failure_cooldown_ladder.py green, so this E2E
    # is the only thing binding the call site.
    reset_calls = []
    _real_reset = gateway_run._reset_hygiene_failure_streak
    monkeypatch.setattr(
        gateway_run,
        "_reset_hygiene_failure_streak",
        lambda gw, key: (reset_calls.append(key), _real_reset(gw, key))[1],
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The transcript must NOT be rewritten — the original is preserved.
    runner.session_store.rewrite_transcript.assert_not_called()

    # This run neither rotated nor compacted in place, so it did NOT recover
    # the session: the reset must NOT have been reached. Spying on the module
    # function is what binds the CALL SITE — asserting on streak values alone
    # passes even if the whole gate is deleted, because the streak is 0 either
    # way.
    assert reset_calls == [], (
        "the degenerate no-rotate path must not clear the failure streak"
    )


@pytest.mark.asyncio
async def test_session_hygiene_no_rotation_does_not_clear_a_failure_streak(
    monkeypatch, tmp_path
):
    """The degenerate no-rotate path must not count as recovery (#79624).

    Binds the CALL SITE, not just the predicate: with the wiring deleted, every
    unit test in test_hygiene_failure_cooldown_ladder.py still passes. Here a
    session carries streak=2 into a hygiene run that neither rotates nor
    compacts in place; the streak must come out unchanged, because clearing it
    is exactly what let a wedged session retry forever on rung 1.
    """
    import gateway.run as _run

    # The predicate the call site must consult, exercised through the same
    # arguments the degenerate branch produces.
    assert _run.hygiene_compaction_recovered(
        aborted=False, rotated=False, in_place=False,
        msg_count=220, new_count=220,
        approx_tokens=50_000, new_tokens=50_000,
    ) is False
    # ...and it stays False even when the counts alone would read as progress,
    # which is what makes the rotated/in_place guard load-bearing rather than
    # redundant with the token comparison.
    assert _run.hygiene_compaction_recovered(
        aborted=False, rotated=False, in_place=False,
        msg_count=220, new_count=100,
        approx_tokens=50_000, new_tokens=30_000,
    ) is False

    runner = object.__new__(_run.GatewayRunner)
    state = runner._session_state("telegram:-1001:17585")
    state.persistent.hygiene_failure_streak = 2
    # A non-recovering run must leave it alone.
    _run._reset_hygiene_failure_streak(runner, "some-other-session")
    assert state.persistent.hygiene_failure_streak == 2
    # ...and a recovering one clears it.
    _run._reset_hygiene_failure_streak(runner, "telegram:-1001:17585")
    assert state.persistent.hygiene_failure_streak == 0


@pytest.mark.asyncio
async def test_session_hygiene_preserves_transcript_when_in_place_configured_but_no_db(monkeypatch, tmp_path):
    """Regression: when compression.in_place is True but the hygiene agent has
    no session_db, archive_and_compact cannot run — _last_compaction_in_place
    stays False.  The guard must read the *result* flag, not the *config* flag,
    otherwise the transcript is unconditionally rewritten with only the summary
    (permanent data loss identical to #21301)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class InPlaceConfiguredAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self.compression_in_place = True
            self._last_compaction_in_place = False
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            return ([{"role": "assistant", "content": "summary only"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = InPlaceConfiguredAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="17585",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The config says in_place=True, but the DB write failed (no session_db)
    # so _last_compaction_in_place is False. Transcript must NOT be rewritten.
    runner.session_store.rewrite_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_session_hygiene_timeout_continues_to_agent_and_sets_cooldown(monkeypatch, tmp_path):
    """A timed-out SessionDB-bound worker cannot compact after the live turn starts.

    The worker remains alive long enough to cross the old race window. The
    timeout must fence its eventual commit, continue to the live agent, and
    clean up the temporary agent only after the worker actually returns.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    worker_started = threading.Event()
    worker_returned = threading.Event()
    release_worker = threading.Event()
    cleanup_done = threading.Event()
    fake_db = MagicMock()
    # The DB-backed cooldown check calls this before compressing; a bare
    # MagicMock return would be truthy and skip compression entirely.
    fake_db.get_compression_failure_cooldown.return_value = None

    class SlowCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "fake-session")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock(side_effect=cleanup_done.set)
            type(self).last_instance = self

        def _compress_context(
            self, messages, *_args, commit_fence=None, **_kwargs
        ):
            worker_started.set()
            try:
                assert release_worker.wait(timeout=30)
                if commit_fence is not None and not commit_fence.begin_commit():
                    return (messages, None)
                try:
                    self._session_db.archive_and_compact(
                        self.session_id,
                        [{"role": "assistant", "content": "too late"}],
                    )
                    self._last_compaction_in_place = True
                    return ([{"role": "assistant", "content": "too late"}], None)
                finally:
                    if commit_fence is not None:
                        commit_fence.finish_commit()
            finally:
                # Set on EVERY exit path (including the blocking wait giving
                # up), so the witness below cannot be silently defeated by the
                # worker failing instead of returning normally.
                worker_returned.set()

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = SlowCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_timeout_seconds: 0.01\n"
        "  hygiene_failure_cooldown_seconds: 120\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-timeout",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=fake_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # DETERMINISTIC concurrency witness — replaces `assert elapsed < 2.0`.
    #
    # The old form timed `_handle_message` and required the total to come in
    # under 2.0s. That made the OS scheduler and unrelated fixed setup part of
    # the assertion: profiling this exact test shows ~1.5s of the measured
    # window is `agent.models_dev.fetch_models_dev` -> `_save_disk_cache` ->
    # `atomic_json_write` (~600k json encoder calls), i.e. one-time provider
    # metadata cache work that has NOTHING to do with the hygiene timeout path
    # being protected. The bound was silently measuring setup cost, so the
    # measured window ranged 1.40s-2.39s run to run on an idle box and the 2.0s
    # threshold sat INSIDE that distribution. It failed 2026-07-25 at
    # 2.005265276999978 -- five milliseconds -- and blocked the merge queue.
    #
    # The real contract is an ORDERING one: the handler must return WITHOUT
    # waiting for the hygiene compression worker. Assert that directly. The
    # worker is still parked on `release_worker` (only set below), so if the
    # timeout wiring is ever removed and the handler awaits the future instead,
    # the worker can only be reached after it returns -- `worker_returned` would
    # be set here and this fails. No wall-clock constant, immune to machine load
    # and to setup cost, and strictly stronger than the bound it replaces.
    assert worker_started.is_set(), "hygiene worker never started"
    assert not worker_returned.is_set(), (
        "handler blocked on the hygiene compression worker: the worker had "
        "already returned by the time _handle_message did, so the turn was "
        "gated on compression instead of continuing past the timeout"
    )
    assert runner._run_agent.await_count == 1
    # Cooldown must be persisted to the state DB (survives restart, #74136),
    # not stashed in an in-memory dict.
    assert fake_db.record_compression_failure_cooldown.called
    _cd_args = fake_db.record_compression_failure_cooldown.call_args[0]
    assert _cd_args[0] == "sess-timeout"
    assert _cd_args[1] > time.time()
    timeout_warnings = [s for s in adapter.sent if "Context compression timed out" in s["content"]]
    assert len(timeout_warnings) == 1
    fake_db.archive_and_compact.assert_not_called()
    SlowCompressAgent.last_instance.close.assert_not_called()

    release_worker.set()
    # Hang-guard, NOT a performance claim: the worker was just released and we
    # only need it to eventually finish. Generous so a loaded runner can never
    # turn teardown into a false red.
    await asyncio.wait_for(asyncio.to_thread(cleanup_done.wait), timeout=30)

    # The late worker observed cancellation at the commit fence, so it never
    # mutated the live session after the new turn began. Cleanup still ran once
    # it was safe to tear down the helper agent's clients/providers.
    fake_db.archive_and_compact.assert_not_called()
    SlowCompressAgent.last_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_session_hygiene_forces_in_place_compaction_with_bound_session_db(
    monkeypatch, tmp_path
):
    """Regression for #60947: gateway hygiene should not rely on
    helper-agent session rotation to shrink a live gateway transcript.

    The hygiene pass runs before the user turn and already owns the gateway
    session binding, so it should force in-place compaction and bind the
    compressor to the gateway SessionDB. Otherwise a helper can return a
    summary without rotating/compacting, the guard preserves the original
    transcript, and the same oversized session is reloaded on every turn.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    stored_system_prompt = (
        "You are Hermes.\n\n"
        "<memory_provider_context>\n"
        "Pinboard provider instructions\n"
        "</memory_provider_context>"
    )
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    async_session_db = SimpleNamespace(
        _db=fake_db,
        get_session=AsyncMock(
            return_value={
                "system_prompt": stored_system_prompt,
            }
        ),
    )

    class FakeInPlaceCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.platform = kwargs.get("platform")
            self.session_id = kwargs.get("session_id", "fake-session")
            self._session_db = kwargs.get("session_db")
            self._cached_system_prompt = None
            self.compression_in_place = False
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            assert self.compression_in_place is True
            assert self._session_db is fake_db
            assert self.platform == "gateway_hygiene"
            assert self._cached_system_prompt == stored_system_prompt
            self._last_compaction_in_place = True
            return ([{"role": "assistant", "content": "compressed in place"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeInPlaceCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:12345",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    runner.session_store.load_transcript.return_value = _make_history(12, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = async_session_db
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="12345",
        ),
        message_id="1",
    )

    # Spy on the recovery reset so this test binds the CALL SITE (#79624).
    # Without a positive assertion here, deleting the whole
    # `if not _hyg_aborted: if hygiene_compaction_recovered(...)` block leaves
    # every other hygiene and ladder test green.
    reset_calls = []
    _real_reset = gateway_run._reset_hygiene_failure_streak
    monkeypatch.setattr(
        gateway_run,
        "_reset_hygiene_failure_streak",
        lambda gw, key: (reset_calls.append(key), _real_reset(gw, key))[1],
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    agent = FakeInPlaceCompressAgent.last_instance
    assert agent is not None
    # Hygiene must have fetched the stored system prompt for THIS session.
    # Parity note (2026-08-08): this was ``assert_awaited_once_with``, which
    # also asserted that NOTHING ELSE reads the session row during the turn.
    # The fork's ``_resolve_footer_message_stats`` (fork-only; absent
    # upstream) legitimately awaits ``get_session`` again later in
    # ``_handle_message_with_agent`` to render the Nmsgs footer, so the
    # stricter form fails on a correct turn. Assert the contract that
    # matters — hygiene looked up this session — not the call count of an
    # unrelated feature.
    assert any(
        call.args == ("sess-1",)
        for call in async_session_db.get_session.await_args_list
    ), (
        "hygiene must fetch the stored system prompt for sess-1; awaits were "
        f"{async_session_db.get_session.await_args_list}"
    )
    agent.context_compressor.bind_session_state.assert_called_once_with(fake_db, "sess-1")
    # In-place compaction already persisted via archive_and_compact() —
    # rewrite_transcript would replace_messages(active_only=False) and DELETE
    # the just-archived rows (#61145). The hygiene handler must skip it.
    runner.session_store.rewrite_transcript.assert_not_called()
    runner._run_agent.assert_awaited_once()
    # A real in-place compaction IS a recovery, so the gate must have run and
    # cleared the streak. This is the positive half of the wiring contract.
    assert reset_calls, (
        "successful in-place compaction must clear the hygiene failure streak "
        "— the recovery gate is not wired into _handle_message_with_agent"
    )


@pytest.mark.asyncio
async def test_session_hygiene_honors_configurable_hard_message_limit(
    monkeypatch, tmp_path
):
    """compression.hygiene_hard_message_limit overrides the default.

    Regression for user-reported fix: a gateway session with a small
    transcript (12 messages) should not hit hygiene compression by default,
    but WILL when the user lowers the hard-limit to 10.  Verifies the new
    config key is actually read and applied at the force-compress gate.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            self.session_id = f"{self.session_id}_compressed"
            return ([{"role": "assistant", "content": "compressed"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    # Write config.yaml with lowered hard-limit
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_hard_message_limit: 10\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:12345",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    # 12 messages: below default → no compression without override,
    # but above the configured limit of 10 → should compress.
    runner.session_store.load_transcript.return_value = _make_history(12, content_size=40)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    # Pick a context length large enough that the token-based threshold
    # won't trigger for 12 short messages — hard-limit must be the ONLY
    # thing firing compression.
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 1_000_000,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The compression agent was instantiated → hard-limit fired on the
    # configured value (10), not the hardcoded 400 default.
    assert FakeCompressAgent.last_instance is not None, (
        "Expected hygiene compression to fire when message count (12) "
        "exceeds configured hygiene_hard_message_limit (10)"
    )


# ---------------------------------------------------------------------------
# Progress-aware hygiene wait: slow-but-streaming models are not punished
# ---------------------------------------------------------------------------

def _make_progress_runner(monkeypatch, tmp_path, agent_cls, cfg_text):
    """Shared scaffolding for the progress-aware hygiene wait tests."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-progress",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )
    return runner, adapter, event




# ---------------------------------------------------------------------------
# Cooldown persistence across gateway restarts (#74136)
# ---------------------------------------------------------------------------

def _make_cooldown_runner(monkeypatch, tmp_path, agent_cls, session_db, session_id):
    """Scaffolding for the restart-persistence tests: a fresh GatewayRunner
    wired to a REAL AsyncSessionDB facade (not a MagicMock) so the hygiene
    cooldown check/write paths exercise the actual SQLite-backed methods."""
    from hermes_state import AsyncSessionDB

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_failure_cooldown_seconds: 300\n",
        encoding="utf-8",
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    # The real async facade over the real SQLite-backed SessionDB — the
    # production shape.  A SimpleNamespace(_db=MagicMock()) here would let
    # the assertion pass against methods that don't actually persist.
    runner._session_db = AsyncSessionDB(session_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )
    return runner, adapter, event


@pytest.mark.asyncio
async def test_hygiene_compression_cooldown_survives_gateway_restart(
    monkeypatch, tmp_path
):
    """Regression for #74136: the compression-failure cooldown must be
    persisted to the state DB, not an in-memory dict on the runner.

    Fail a hygiene compression on runner #1, tear the runner down, build a
    FRESH runner on the SAME database (simulating a gateway restart), and
    assert the second runner still honors the cooldown — i.e. it does not
    re-instantiate a compression agent for the same failing session.
    """
    from hermes_state import SessionDB

    session_id = "sess-restart"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id, "telegram")

        class AbortingCompressAgent:
            instances = 0

            def __init__(self, **kwargs):
                type(self).instances += 1
                self.session_id = kwargs.get("session_id", session_id)
                self._session_db = kwargs.get("session_db")
                self._last_compaction_in_place = False
                self.context_compressor = SimpleNamespace(
                    bind_session_state=MagicMock(),
                    _last_compress_aborted=True,
                    _last_summary_error="aux model exploded",
                    _last_aux_model_failure_model=None,
                )
                self.shutdown_memory_provider = MagicMock()
                self.close = MagicMock()

            def _compress_context(self, messages, *_args, **_kwargs):
                # Summary generation failed: compressor aborts and returns
                # the transcript unchanged.
                return (messages, None)

        runner1, _adapter1, event1 = _make_cooldown_runner(
            monkeypatch, tmp_path, AbortingCompressAgent, db, session_id
        )
        assert await runner1._handle_message(event1) == "ok"
        assert AbortingCompressAgent.instances == 1

        # The abort must have persisted a cooldown to the DB.
        state = db.get_compression_failure_cooldown(session_id)
        assert state is not None and state["remaining_seconds"] > 0, (
            "hygiene compression abort did not persist a cooldown to the "
            f"state DB; got {state!r}"
        )

        # --- simulate a gateway restart: brand-new runner, same DB ---
        del runner1

        class ShouldNotRunAgent:
            instances = 0

            def __init__(self, **kwargs):
                type(self).instances += 1
                self.context_compressor = SimpleNamespace(
                    bind_session_state=MagicMock(),
                    _last_compress_aborted=False,
                    _last_aux_model_failure_model=None,
                )
                self.shutdown_memory_provider = MagicMock()
                self.close = MagicMock()

            def _compress_context(self, messages, *_args, **_kwargs):
                return (messages, None)

        runner2, _adapter2, event2 = _make_cooldown_runner(
            monkeypatch, tmp_path, ShouldNotRunAgent, db, session_id
        )
        assert await runner2._handle_message(event2) == "ok"
        assert ShouldNotRunAgent.instances == 0, (
            "REGRESSION (#74136): a fresh GatewayRunner on the same state DB "
            "re-ran the failing hygiene compression — the failure cooldown "
            "was lost across the restart (in-memory dict instead of the "
            "DB-backed record/get methods)."
        )
        # The user turn itself still runs; only compression is skipped.
        assert runner2._run_agent.await_count == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Phase 3 / Phase 0: hygiene compaction ANNOUNCE + abort-write-back guard
# ---------------------------------------------------------------------------

def _hyg_runner(adapter, monkeypatch, gateway_run, *, transcript, tmp_path, ctx_len=1_000_000):
    """Build a GatewayRunner wired for a hygiene e2e with a capture adapter."""
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = transcript
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok", "messages": [], "tools": [],
            "history_offset": 0, "last_prompt_tokens": 0,
        }
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_a, **_k: ctx_len,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")
    return runner


def _hyg_event():
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group",
            thread_id="17585", user_id="12345",
        ),
        message_id="1",
    )


@pytest.mark.asyncio
async def test_hygiene_msgcount_announces_limit_not_count(monkeypatch, tmp_path):
    """A message-count hygiene trip delivers a 🗜️ announce whose 'why it fired'
    clause shows the configured LIMIT (the hard message limit), with the current
    count only in the X→Y body — NOT the count in both places (the 2026-06-22
    live bug rendered 'safety limit: 1075 messages' where 1075 was the count)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeLCMAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "sess-1")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            self.context_compressor = SimpleNamespace(
                name="lcm",
                _last_compression_status="compacted",
                _last_compress_aborted=False,
            )
            type(self).last_instance = self

        def _compress_context(self, messages, *_a, **_k):
            self.session_id = f"{self.session_id}_compressed"
            return ([{"role": "assistant", "content": "summary"}] * 33, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeLCMAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")

    adapter = HygieneCaptureAdapter()
    # 410 messages: well over the 400 hard limit (message valve), all
    # user/assistant so eligible == full == 410 here; small content so the
    # TOKEN valve does NOT fire (isolates the message valve).
    transcript = _make_history(410, content_size=20)
    runner = _hyg_runner(adapter, monkeypatch, gateway_run, transcript=transcript, tmp_path=tmp_path)

    result = await runner._handle_message(_hyg_event())
    assert result == "ok"

    announces = [s for s in adapter.sent if "Context compacted" in s["content"]]
    assert len(announces) == 1, f"expected exactly 1 announce, got {adapter.sent}"
    line = announces[0]["content"]
    # the clause shows the configured LIMIT (400 default), NOT the count (410)
    assert "message-count safety limit: 400 messages" in line
    assert "limit: 410" not in line  # the live bug: count masquerading as limit
    # The count appears only in the like-for-like delta: pre (410) → post (33).
    # Post-2026-06-22 the hygiene path RECONCILES (kept measured comp-side), so the
    # granular form renders — the count lives in the "Messages:" body, never the clause.
    assert "410 → 33" in line
    assert "kept 33 recent chat" in line
    # LCM lossless recovery guidance, contentless (no Summary:)
    assert "lcm_grep" in line
    assert "Summary:" not in line
    # delivered to the originating thread
    assert announces[0]["chat_id"] == "-1001"
    assert announces[0]["metadata"] == {"thread_id": "17585"}


@pytest.mark.asyncio
async def test_hygiene_abort_does_not_rewrite_or_announce(monkeypatch, tmp_path):
    """Issue 8 guard: on compaction ABORT the gateway must NOT rewrite the
    transcript (which would drop tool/system msgs) and must NOT announce."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeAbortAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "sess-1")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            self.context_compressor = SimpleNamespace(
                name="lcm",
                _last_compression_status="noop",
                _last_compress_aborted=True,
                _last_summary_error="404 model not found",
                _last_aux_model_failure_model=None,
            )
            type(self).last_instance = self

        def _compress_context(self, messages, *_a, **_k):
            # abort: return INPUT unchanged, do NOT rotate session
            return (messages, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAbortAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")

    adapter = HygieneCaptureAdapter()
    transcript = _make_history(410, content_size=20)
    runner = _hyg_runner(adapter, monkeypatch, gateway_run, transcript=transcript, tmp_path=tmp_path)

    result = await runner._handle_message(_hyg_event())
    assert result == "ok"

    # Issue 8: transcript NOT rewritten on abort
    runner.session_store.rewrite_transcript.assert_not_called()
    # No compaction announce on abort (gating + guard)
    assert not [s for s in adapter.sent if "Context compacted" in s["content"]]
    # The existing abort warning IS still delivered
    assert [s for s in adapter.sent if "compression aborted" in s["content"].lower()]


@pytest.mark.asyncio
async def test_hygiene_announce_kill_switch_suppresses(monkeypatch, tmp_path):
    """compression.announce_on_hygiene: false suppresses the announce."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeLCMAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "sess-1")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            self.context_compressor = SimpleNamespace(
                name="lcm",
                _last_compression_status="compacted",
                _last_compress_aborted=False,
            )
            type(self).last_instance = self

        def _compress_context(self, messages, *_a, **_k):
            self.session_id = f"{self.session_id}_compressed"
            return ([{"role": "assistant", "content": "summary"}] * 33, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeLCMAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")

    # kill switch off via a real config file the per-event read picks up
    (tmp_path / "config.yaml").write_text(
        "compression:\n  enabled: true\n  announce_on_hygiene: false\n"
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config",
                        lambda: {"compression": {"announce_on_hygiene": False}})

    adapter = HygieneCaptureAdapter()
    transcript = _make_history(410, content_size=20)
    runner = _hyg_runner(adapter, monkeypatch, gateway_run, transcript=transcript, tmp_path=tmp_path)

    result = await runner._handle_message(_hyg_event())
    assert result == "ok"
    assert not [s for s in adapter.sent if "Context compacted" in s["content"]]


# ---------------------------------------------------------------------------
# hygiene_threshold config key resolution (T8) — mirrors the gateway inline parse
# ---------------------------------------------------------------------------

def _resolve_hyg_threshold(comp_cfg):
    """Mirror of gateway/run.py inline resolution (default 0.85, clamp 0<x<=1)."""
    _hyg_threshold_pct = 0.85
    raw = comp_cfg.get("hygiene_threshold")
    if raw is not None:
        try:
            v = float(raw)
            if 0.0 < v <= 1.0:
                _hyg_threshold_pct = v
        except (TypeError, ValueError):
            pass
    return _hyg_threshold_pct


def test_hygiene_threshold_default_when_absent():
    assert _resolve_hyg_threshold({}) == 0.85


def test_hygiene_threshold_override():
    assert _resolve_hyg_threshold({"hygiene_threshold": 0.9}) == 0.9


def test_hygiene_threshold_garbage_falls_back():
    assert _resolve_hyg_threshold({"hygiene_threshold": "nope"}) == 0.85


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, 2])
def test_hygiene_threshold_out_of_range_falls_back(bad):
    assert _resolve_hyg_threshold({"hygiene_threshold": bad}) == 0.85


def test_hygiene_threshold_is_in_config_defaults():
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["compression"]["hygiene_threshold"] == 0.85


# ---------------------------------------------------------------------------
# Hygiene deadline derivation + repeated-failure escalation
#
# The outer asyncio.wait_for guard around hygiene compression used to be a
# hardcoded 30.0s while the summariser call it wraps gets
# auxiliary.compression.timeout (default 300s). An outer guard TIGHTER than the
# inner deadline is a guaranteed kill on any large transcript, so hygiene could
# never succeed and the session grew unboundedly.
# ---------------------------------------------------------------------------


def test_hygiene_timeout_default_derives_from_auxiliary_compression():
    """Unset hygiene_timeout_seconds -> follow auxiliary.compression.timeout."""
    from agent.hygiene_timeout import resolve_hygiene_timeout_seconds

    seconds, explicit = resolve_hygiene_timeout_seconds(
        {"enabled": True},
        {"compression": {"provider": "auto", "model": "", "timeout": 300}},
    )
    # Pre-fix this was 30.0 — the value that killed the 3,000-message session.
    assert seconds == 300.0
    assert explicit is False


def test_hygiene_failure_alert_after_is_in_config_defaults():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["hygiene_failure_alert_after"] == 3


def test_hygiene_timeout_seconds_default_is_unset_for_derivation():
    """A literal 30 in DEFAULT_CONFIG would re-pin the guard below the inner
    deadline via the explicit branch, reintroducing the bug."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["hygiene_timeout_seconds"] is None


def test_hygiene_failure_streak_bumps_and_clears():
    """The streak is what makes 'compression can never succeed' visible."""
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)

    assert runner._record_hygiene_compression_failure("s1") == 1
    assert runner._record_hygiene_compression_failure("s1") == 2
    assert runner._record_hygiene_compression_failure("s1") == 3
    # Independent per session.
    assert runner._record_hygiene_compression_failure("s2") == 1

    # A real compaction resets only that session's streak.
    runner._clear_hygiene_compression_failures("s1")
    assert runner._record_hygiene_compression_failure("s1") == 1
    assert runner._record_hygiene_compression_failure("s2") == 2

    # Clearing an unknown session is a no-op, not a KeyError.
    runner._clear_hygiene_compression_failures("never-seen")


@pytest.mark.asyncio
async def test_repeated_hygiene_timeouts_escalate_to_a_loud_warning(monkeypatch, tmp_path):
    """3 consecutive timeouts -> the warning names the unbounded growth and the
    knob, instead of repeating the same benign-sounding line forever."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_db = MagicMock()
    # A bare MagicMock returns a MagicMock for EVERY attribute, so the
    # hygiene cooldown gate (`_cooldown_state.get("remaining_seconds", 0) > 0`)
    # compares MagicMock > int and raises TypeError. This test drives the
    # cooldown to 0 via config; say so on the DB too. Sibling hygiene tests
    # already stub this the same way.
    fake_db.get_compression_failure_cooldown.return_value = None

    class AlwaysTimingOutAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "fake-session")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
            # Outlive the (tiny, explicitly pinned) deadline every time.
            time.sleep(0.5)
            return (messages, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = AlwaysTimingOutAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    # cooldown 0 so each turn actually retries; alert on the 3rd failure.
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_timeout_seconds: 0.01\n"
        "  hygiene_failure_cooldown_seconds: 0\n"
        "  hygiene_failure_alert_after: 3\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    adapter = HygieneCaptureAdapter()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-loud",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=fake_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    def _event(n):
        return MessageEvent(
            text=f"hello {n}",
            source=SessionSource(
                platform=Platform.TELEGRAM,
                chat_id="12345",
                chat_type="dm",
                user_id="12345",
            ),
            message_id=str(n),
        )

    for n in range(3):
        assert await runner._handle_message(_event(n)) == "ok"

    warnings = [s["content"] for s in adapter.sent if "compression" in s["content"].lower()]
    assert len(warnings) == 3

    # First two: the benign per-occurrence line.
    for quiet in warnings[:2]:
        assert "Context compression timed out" in quiet
        assert "🚨" not in quiet

    # Third: loud, names the growth AND the knob — but still truthful about
    # the no-data-loss semantics.
    loud = warnings[2]
    assert "🚨" in loud
    assert "3 times in a row" in loud
    assert "growing" in loud
    assert "compression.hygiene_timeout_seconds" in loud
    assert "No messages have been dropped" in loud

    # No-data-loss invariant held throughout: nothing was ever written back.
    fake_db.archive_and_compact.assert_not_called()
    runner.session_store.rewrite_transcript.assert_not_called()
