"""/stop's durable marker reads ONE row, and every stop logs where its time went.

Live 2026-11-08: /stop took up to 38.7 s on Discord (p90 15 s), almost all of it between the
generation bump and ``PHASE=user_stopped``. ``_mark_user_stopped`` loaded the whole transcript to
read the newest row id; it now asks for the newest row only, and ``PHASE=stop_stages`` splits the
stop into stages (plus the default-pool queue) so the next slow stop names its own cause.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        pass

    async def interrupt_session_activity(self, session_key, chat_id):
        pass


class _SpyDB:
    """Stands in for AsyncSessionDB: answers get_messages like SessionDB does for these kwargs."""

    def __init__(self, ids):
        self.ids = ids
        self.calls = []

    async def get_messages(self, session_id, **kwargs):
        self.calls.append(kwargs)
        rows = [{"id": i, "role": "user", "content": "x"} for i in self.ids]
        if kwargs.get("latest") and kwargs.get("limit"):
            rows = rows[-kwargs["limit"]:]
        return rows


def _runner(db, key):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._running_agent_tasks = {}
    runner._pending_messages = {}
    runner._background_tasks = set()
    runner._session_db = db
    marks = []

    def _mark(session_key, **kwargs):
        marks.append(kwargs)
        return True

    # The async facade (a read-only property) wraps this store, so stub the store itself.
    runner.session_store = SimpleNamespace(
        _entries={key: SimpleNamespace(session_id="sess-big")}, mark_user_stopped=_mark)
    runner._test_marks = marks
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    return runner


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1")


@pytest.mark.asyncio
async def test_marker_reads_only_the_newest_row():
    key = build_session_key(_source())
    db = _SpyDB(list(range(1, 40_001)))
    runner = _runner(db, key)

    assert await runner._mark_user_stopped(key)

    assert db.calls and db.calls[0].get("latest") is True and db.calls[0].get("limit") == 1, db.calls
    assert runner._test_marks and runner._test_marks[-1]["last_message_id"] == 40_000


@pytest.mark.asyncio
async def test_every_stop_logs_its_stages(caplog):
    source = _source()
    key = build_session_key(source)
    runner = _runner(_SpyDB([1, 2, 3]), key)
    agent = MagicMock()
    agent.session_id = "sess-big"
    runner._running_agents[key] = agent
    runner._session_state(key).turn.started_ts = time.time()

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await runner._interrupt_and_clear_session(
            key, source, interrupt_reason="stop", invalidation_reason="stop_command")

    lines = [r.getMessage() for r in caplog.records if "PHASE=stop_stages" in r.getMessage()]
    assert len(lines) == 1, lines
    for stage in ("bump=", "hold=", "deleg=", "hook=", "marker=", "adapter=", "release=", "pool_start=", "total="):
        assert stage in lines[0], (stage, lines[0])
    marker_lines = [r.getMessage() for r in caplog.records if "PHASE=user_stopped" in r.getMessage()]
    assert marker_lines and "rowid_s=" in marker_lines[0] and "store_s=" in marker_lines[0]
