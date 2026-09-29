"""Session-hygiene valve must not fire on a STALE ``last_prompt_tokens``.

Live incident 2026-09-29 (session 20260927_183134_b6f8858a): the stored
``session_entry.last_prompt_tokens`` still held the pre-compaction peak
(~1,019,594) after an in-turn compaction had shrunk the context to ~125k and
two further turns had run at 134k..165k real.  The next inbound message tripped
the hygiene valve on the stale figure ("token threshold, ~1,019,594 tokens")
and compacted a 16%-occupied context, cold-starting a 99%-cached prefix.

Two defects, both covered here:

* READER: the valve trusted the stored figure even though the cached agent
  for the same session held a fresher, real last-call figure (the one
  ``/context`` shows).
* WRITER: the turn chain (normal turn -> queued follow-up -> ``/stop``) ended
  on the stale-generation discard path, which returned before the
  ``update_session(last_prompt_tokens=...)`` write, so neither turn's real
  figure was persisted.
"""

import importlib
import sys
import threading
import types
from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from tests.gateway.test_session_hygiene import (
    HygieneCaptureAdapter,
    _hyg_event,
    _hyg_runner,
    _make_history,
)


SESSION_KEY = "agent:main:telegram:group:-1001:17585"
CTX = 1_000_000  # threshold 85% = 850,000


def _install_fake_agent(monkeypatch):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeLCMAgent:
        compress_calls = 0

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

        def _compress_context(self, messages, *_a, **_k):
            type(self).compress_calls += 1
            return ([{"role": "assistant", "content": "summary"}] * 8, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeLCMAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    return FakeLCMAgent, importlib.import_module("gateway.run")


def _cache_live_agent(runner, *, session_id, last_prompt_tokens):
    live = SimpleNamespace(
        session_id=session_id,
        context_compressor=SimpleNamespace(last_prompt_tokens=last_prompt_tokens),
    )
    runner._agent_cache = OrderedDict({SESSION_KEY: (live, "sig")})
    runner._agent_cache_lock = threading.Lock()


def _prepare(monkeypatch, tmp_path, *, stored, transcript):
    agent_cls, gateway_run = _install_fake_agent(monkeypatch)
    adapter = HygieneCaptureAdapter()
    runner = _hyg_runner(
        adapter, monkeypatch, gateway_run, transcript=transcript,
        tmp_path=tmp_path, ctx_len=CTX,
    )
    runner.session_store.get_or_create_session.return_value.last_prompt_tokens = stored
    return agent_cls, adapter, runner


@pytest.mark.asyncio
async def test_valve_prefers_live_figure_over_stale_stored(monkeypatch, tmp_path, caplog):
    """stored > threshold but the cached agent's latest real call < threshold -> no fire."""
    agent_cls, adapter, runner = _prepare(
        monkeypatch, tmp_path, stored=1_019_594, transcript=_make_history(218, content_size=40),
    )
    _cache_live_agent(runner, session_id="sess-1", last_prompt_tokens=165_349)

    caplog.set_level("INFO", logger="gateway.run")
    assert await runner._handle_message(_hyg_event()) == "ok"

    assert agent_cls.compress_calls == 0, "hygiene compacted on a stale stored figure"
    assert not [s for s in adapter.sent if "Context compacted" in s["content"]]
    assert "superseded by live" in caplog.text


@pytest.mark.asyncio
async def test_valve_still_fires_on_live_figure_over_threshold(monkeypatch, tmp_path, caplog):
    """The live figure is authoritative both ways: a real over-threshold call fires."""
    agent_cls, _adapter, runner = _prepare(
        monkeypatch, tmp_path, stored=100_000, transcript=_make_history(218, content_size=40),
    )
    _cache_live_agent(runner, session_id="sess-1", last_prompt_tokens=900_000)

    caplog.set_level("INFO", logger="gateway.run")
    assert await runner._handle_message(_hyg_event()) == "ok"
    assert agent_cls.compress_calls == 1
    assert "~900,000 tokens (actual (live))" in caplog.text


@pytest.mark.asyncio
async def test_valve_ignores_cached_agent_of_another_session(monkeypatch, tmp_path):
    """A cached agent bound to a different session_id is not evidence for this one."""
    agent_cls, _adapter, runner = _prepare(
        monkeypatch, tmp_path, stored=900_000, transcript=_make_history(218, content_size=10_000),
    )
    _cache_live_agent(runner, session_id="other-session", last_prompt_tokens=10_000)
    assert await runner._handle_message(_hyg_event()) == "ok"
    assert agent_cls.compress_calls == 1


@pytest.mark.asyncio
async def test_valve_no_cached_agent_discards_stored_that_predates_transcript(
    monkeypatch, tmp_path, caplog,
):
    """After a restart (no cached agent), a stored figure far above what the loaded
    transcript can hold predates the last compaction -> treat as absent."""
    agent_cls, adapter, runner = _prepare(
        monkeypatch, tmp_path, stored=1_019_594, transcript=_make_history(218, content_size=40),
    )
    caplog.set_level("INFO", logger="gateway.run")
    assert await runner._handle_message(_hyg_event()) == "ok"
    assert agent_cls.compress_calls == 0
    assert not [s for s in adapter.sent if "Context compacted" in s["content"]]
    assert "predates the current transcript" in caplog.text


@pytest.mark.asyncio
async def test_valve_no_cached_agent_trusts_plausible_stored(monkeypatch, tmp_path):
    """No cached agent and a stored figure consistent with the transcript -> still fires."""
    agent_cls, _adapter, runner = _prepare(
        monkeypatch, tmp_path, stored=900_000, transcript=_make_history(218, content_size=10_000),
    )
    assert await runner._handle_message(_hyg_event()) == "ok"
    assert agent_cls.compress_calls == 1


# ---------------------------------------------------------------------------
# WRITER: the stale-generation discard path must still persist the real figure
# ---------------------------------------------------------------------------

def _writer_runner(monkeypatch, tmp_path, gateway_run):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
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
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-stale",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *_a, **_k: 100_000,
    )
    return runner


def _writer_event():
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345",
        ),
        message_id="m-1",
    )


@pytest.mark.asyncio
async def test_interrupted_stale_generation_turn_persists_last_prompt_tokens(
    monkeypatch, tmp_path,
):
    """/stop mid-API-call invalidates the generation; the discarded result's real
    last_prompt_tokens must still reach the session store (guarded by session_id)."""
    import gateway.run as gateway_run

    runner = _writer_runner(monkeypatch, tmp_path, gateway_run)
    runner._is_session_run_current = lambda _key, _gen: False
    runner._run_agent = AsyncMock(return_value={
        "final_response": "", "messages": [], "history_offset": 0,
        "interrupted": True, "session_id": "sess-stale",
        "last_prompt_tokens": 165_349,
    })

    await runner._handle_message_with_agent(
        _writer_event(), _writer_event().source, "agent:main:telegram:group:-1001:12345", 1,
    )

    token_writes = [
        c for c in runner.session_store.update_session.call_args_list
        if "last_prompt_tokens" in c.kwargs
    ]
    assert token_writes, "stale-generation discard dropped the real last_prompt_tokens"
    kw = token_writes[-1].kwargs
    assert kw["last_prompt_tokens"] == 165_349
    assert kw["expected_session_id"] == "sess-stale"
    assert kw["touch_activity"] is False


@pytest.mark.asyncio
async def test_post_compaction_awaiting_real_usage_marker_persisted_on_discard(
    monkeypatch, tmp_path,
):
    """A discarded turn that ended right after an in-turn compaction (compressor at
    -1 = awaiting real usage) must overwrite the pre-compaction peak, not keep it."""
    import gateway.run as gateway_run

    runner = _writer_runner(monkeypatch, tmp_path, gateway_run)
    runner._is_session_run_current = lambda _key, _gen: False
    runner._run_agent = AsyncMock(return_value={
        "final_response": "", "messages": [], "history_offset": 0,
        "interrupted": True, "session_id": "sess-stale", "last_prompt_tokens": -1,
    })
    await runner._handle_message_with_agent(
        _writer_event(), _writer_event().source, "agent:main:telegram:group:-1001:12345", 1,
    )
    token_writes = [
        c for c in runner.session_store.update_session.call_args_list
        if "last_prompt_tokens" in c.kwargs
    ]
    assert token_writes and token_writes[-1].kwargs["last_prompt_tokens"] == -1


def test_update_session_expected_session_id_guard(tmp_path):
    """A stale write must not land on an entry that was reset to a new session."""
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None
    store._loaded = True
    key = "agent:main:telegram:group:-1001:12345"
    store._entries[key] = SessionEntry(
        session_key=key, session_id="new-after-reset",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="group", last_prompt_tokens=7,
    )
    store._save_entry = lambda *_a, **_k: None
    store._record_gateway_session_peer = lambda *_a, **_k: None

    store.update_session(key, last_prompt_tokens=165_349, touch_activity=False,
                         expected_session_id="old-before-reset")
    assert store._entries[key].last_prompt_tokens == 7

    store.update_session(key, last_prompt_tokens=165_349, touch_activity=False,
                         expected_session_id="new-after-reset")
    assert store._entries[key].last_prompt_tokens == 165_349


def test_turn_result_reports_post_compaction_real_call():
    """After an in-turn compaction (last_prompt_tokens=-1, awaiting real usage) the
    next real API usage replaces the marker, so the turn result the gateway persists
    carries the post-compaction call, not the pre-compaction peak."""
    from agent.context_compressor import ContextCompressor

    comp = ContextCompressor.__new__(ContextCompressor)
    try:
        comp.__init__(model="test-model", threshold_percent=0.75, quiet_mode=True,
                      config_context_length=CTX)
    except TypeError:
        pytest.skip("ContextCompressor signature differs")
    comp.last_prompt_tokens = -1
    comp.awaiting_real_usage_after_compression = True
    comp.update_from_response({"prompt_tokens": 125_644, "completion_tokens": 10,
                               "total_tokens": 125_654})
    assert comp.last_prompt_tokens == 125_644
    assert comp.awaiting_real_usage_after_compression is False
