"""Gateway-side session binding for async delegations (#57498, #55578).

Three invariants on the messaging-gateway surface, mirroring the TUI rules:

1. Completions are pinned to the spawning session (contributor commit).
2. A dead/ended spawning session is never resurrected: the injection is
   dropped, fail-closed (never rerouted to the peer's current session).
3. /new interrupts the old conversation's in-flight async delegations.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import tools.async_delegation as ad


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed_record(delegation_id, session_key="", parent_session_id="", status="running"):
    fn = MagicMock()
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": status,
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "interrupt_fn": fn,
        }
    return fn


class TestInterruptForSessionByParentId:
    def test_parent_session_id_selector(self):
        mine = _seed_record("d1", session_key="agent:main:telegram:dm:1", parent_session_id="sess_old")
        other = _seed_record("d2", session_key="agent:main:telegram:dm:2", parent_session_id="sess_other")
        n = ad.interrupt_for_session(parent_session_id="sess_old")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()


class TestGatewayPinningFailsClosed:
    """The gateway must follow only verified compression continuations."""

    @staticmethod
    def _entry(session_id):
        from datetime import datetime

        from gateway.config import Platform
        from gateway.session import SessionEntry

        return SessionEntry(
            session_key="agent:main:telegram:group:-100:4",
            session_id=session_id,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="group",
        )

    def _make_runner(
        self,
        rows,
        *,
        compression_tip=None,
        compression_error=None,
        switched_entry=None,
    ):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore

        runner = object.__new__(GatewayRunner)
        db = MagicMock()
        db.get_session = AsyncMock(side_effect=lambda session_id: rows.get(session_id))
        db.get_compression_tip = AsyncMock(
            return_value=compression_tip,
            side_effect=compression_error,
        )
        runner._session_db = db
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=switched_entry)
        runner.session_store.advance_compression_session = MagicMock(
            return_value=switched_entry
        )
        runner._async_session_store = AsyncSessionStore(runner.session_store)
        return runner

    @staticmethod
    def _assert_no_route_change(runner):
        getattr(runner.session_store, "switch_session").assert_not_called()
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_not_called()


    @pytest.mark.asyncio
    async def test_live_spawning_session_rebinds_from_different_route(self):
        current = self._entry("sess_current")
        pinned = self._entry("sess_live")
        runner = self._make_runner(
            {"sess_live": {"id": "sess_live", "ended_at": None}},
            switched_entry=pinned,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_live"
        )

        assert resolved is pinned
        getattr(runner.session_store, "switch_session").assert_called_once_with(
            current.session_key, "sess_live", expected_session_id=current.session_id,
        )

    @pytest.mark.asyncio
    async def test_non_compression_ended_parent_drops(self):
        current = self._entry("sess_old")
        runner = self._make_runner(
            {
                "sess_old": {
                    "id": "sess_old",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "session_reset",
                }
            }
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_old"
        )

        assert resolved is None
        self._assert_no_route_change(runner)


    @pytest.mark.asyncio
    async def test_intermediate_compression_route_advances_to_same_live_tip(self):
        current = self._entry("sess_middle")
        tip = self._entry("sess_tip")
        runner = self._make_runner(
            {
                "sess_parent": {
                    "id": "sess_parent",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "compression",
                },
                "sess_middle": {
                    "id": "sess_middle",
                    "ended_at": "2026-07-08T00:01:00",
                    "end_reason": "compression",
                    "parent_session_id": "sess_parent",
                },
                "sess_tip": {
                    "id": "sess_tip",
                    "ended_at": None,
                    "parent_session_id": "sess_middle",
                },
            },
            compression_tip="sess_tip",
            switched_entry=tip,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_middle", "sess_tip")

    @pytest.mark.asyncio
    async def test_compression_parent_follows_real_sessiondb_lineage(self, tmp_path):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore
        from hermes_state import AsyncSessionDB, SessionDB

        session_db = SessionDB(db_path=tmp_path / "state.db")
        session_db.create_session("sess_parent", source="telegram")
        session_db.end_session("sess_parent", end_reason="compression")
        session_db.create_session(
            "sess_tip",
            source="telegram",
            parent_session_id="sess_parent",
        )

        current = self._entry("sess_parent")
        tip = self._entry("sess_tip")
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(session_db)
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=tip)
        runner.session_store.advance_compression_session = MagicMock(return_value=tip)
        runner._async_session_store = AsyncSessionStore(runner.session_store)

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_parent", "sess_tip")




@pytest.mark.asyncio
async def test_process_child_pin_cannot_take_over_human_route(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from hermes_state import SessionDB, AsyncSessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    db = SessionDB(db_path=tmp_path / "fixture.db")
    db.create_session(entry.session_id, source="telegram")
    db.create_session("child", source="telegram", parent_session_id=entry.session_id,
                      model_config={"_delegate_from": entry.session_id})
    runner._session_db = AsyncSessionDB(db)
    runner._cache_session_source = lambda *a: None
    runner._recover_telegram_topic_thread_id = lambda s: None
    leases = SessionTurnLeaseRegistry()
    held = await leases.acquire("child", owner_key="child", generation=1)
    seen = []

    async def receive(event):
        resolved = await runner._hmwa_resolve_session(event, event.source)
        if resolved is not None:
            sid = resolved[1].session_id
            token = await leases.acquire(sid, owner_key="human", generation=1, timeout=0.1)
            seen.append(sid)
            leases.release(token)
        event._gateway_accepted = True

    adapter = SimpleNamespace(handle_message=receive)
    runner.adapters[Platform.TELEGRAM] = adapter
    event = {"type": "completion", "session_id": "proc_child", "session_key": entry.session_key,
             "parent_session_id": "child", "platform": "telegram", "chat_id": "123"}
    try:
        await runner._inject_watch_notification("child finished", event)
        print("ROUTE", runner.session_store.lookup_by_session_key(entry.session_key).session_id, "seen", seen)
        assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
        await asyncio.wait_for(receive(MessageEvent(text="follow up", source=source)), 2)
        assert seen == [entry.session_id]
        assert await runner._classify_completion_target("child") == "retry"
        assert await runner._classify_completion_target(entry.session_id) == "deliver"
        assert await runner._classify_completion_target("missing") == "terminal"
    finally:
        leases.release(held)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["none", "revoke", "replace"])
async def test_pending_pin_respects_concurrent_boundary(tmp_path, boundary):
    """A non-compression re-pin that resolved its row across an await must not move the route
    after the run was invalidated (/stop) or the route was replaced (/new, /resume) meanwhile;
    an undisturbed pin still lands. Real store + real resolver; the DB lookup is event-gated.
    Scenario by the #113690 reporter."""
    import asyncio
    from types import SimpleNamespace

    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test-chat", chat_type="dm", user_id="test-user")
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    generation = runner._begin_session_run_generation(entry.session_key)
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_session(session_id):
        entered.set()
        await release.wait()
        return {"id": session_id, "ended_at": None}

    runner._session_db = SimpleNamespace(get_session=AsyncMock(side_effect=get_session))
    task = asyncio.create_task(runner._resolve_async_delegation_session(entry, "test-pinned"))
    await asyncio.wait_for(entered.wait(), 3)
    expected = "test-pinned"
    if boundary != "none":
        runner._invalidate_session_run_generation(entry.session_key, reason="test boundary")
        assert not runner._is_session_run_current(entry.session_key, generation)
        expected = entry.session_id
    if boundary == "replace":
        store.switch_session(entry.session_key, "test-replacement")
        expected = "test-replacement"
    release.set()
    result = await asyncio.wait_for(task, 3)

    assert store.lookup_by_session_key(entry.session_key).session_id == expected
    if boundary == "none":
        assert result is not None and result.session_id == expected
    else:
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["new_session", "user_exit", "session_switch"])
async def test_user_closed_delegated_completion_is_terminal(tmp_path, monkeypatch, reason):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB, AsyncSessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    db = SessionDB(db_path=tmp_path / "fixture.db")
    db.create_session("child", source="tool", model_config={"_delegate_from": "parent"})
    db.end_session("child", reason)
    runner._session_db = AsyncSessionDB(db)
    try:
        claim = await runner._preflight_completion_delivery(
            {"type": "completion", "session_id": "proc_closed", "parent_session_id": "child"})
        assert not claim.proceed
        assert claim.early_result is not False, "terminal must stop the watcher, not ask it to retry"
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [True, False])
@pytest.mark.parametrize("source", ["tool", "telegram"])
async def test_delegated_origin_watcher_stops_once(
    tmp_path, monkeypatch, caplog, ended, source,
):
    """The real process watcher must dispose a child-origin completion, not re-poll forever."""
    import asyncio
    import logging
    import time
    from types import SimpleNamespace

    import gateway.run_notifications as notifications
    import tools.process_registry as processes
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_state import AsyncSessionDB, SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    route = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
    entry = runner.session_store.get_or_create_session(route)
    db = SessionDB(db_path=tmp_path / "fixture.db")
    db.create_session("child", source=source, model_config={"_delegate_from": "nonhuman-parent"})
    if ended:
        db.end_session("child", "completed")
    runner._session_db = AsyncSessionDB(db)
    # Advance only notification time: no wall-clock wait and no changes to DB timestamps.
    now = [db.get_session("child")["started_at"]]
    monkeypatch.setattr(notifications, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    process = SimpleNamespace(
        exited=True, output_buffer="done\n", exit_code=0, command="echo done",
        started_at=now[0], parent_session_id="child",
    )
    registry = SimpleNamespace(get=MagicMock(return_value=process), is_completion_consumed=lambda sid: False)
    monkeypatch.setattr(processes, "process_registry", registry)
    runner._load_background_notifications_mode = lambda: "off"
    runner._launching_turn_active = AsyncMock(return_value=False)
    runner._inject_watch_notification = AsyncMock(return_value=True)
    # Bypass only the coalescing delay; retain real watcher -> delivery -> preflight.
    runner._enqueue_process_completion_notification = runner._deliver_completion_notification
    polls = 0

    async def tick(_interval):
        nonlocal polls
        polls += 1
        assert polls <= 7, "delegated-origin completion retried past its five-minute budget"
        now[0] += 60

    monkeypatch.setattr(asyncio, "sleep", tick)
    watcher = {"session_id": "proc_child", "check_interval": 60, "notify_on_complete": True,
               "parent_session_id": "child", "agent_notify_mode": "all",
               "session_key": entry.session_key, "platform": "telegram", "chat_id": "123", "chat_type": "dm"}
    try:
        with caplog.at_level(logging.WARNING, logger="gateway.run"):
            await runner._run_process_watcher(watcher)
        assert polls == (1 if ended else 5)
        warnings = [r for r in caplog.records if r.name == "gateway.run" and r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "dropping notification" in warnings[0].getMessage()
        runner._inject_watch_notification.assert_not_awaited()
        assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    finally:
        db.close()


def _delegated_route_fixture(tmp_path, monkeypatch):
    """t_b9d81184: a live chat route whose CURRENT session carries an inherited ``_delegate_from``
    (prod: discord thread route 20261108_212456_25ca27, created_source=gateway_recovery off a
    subagent-created ancestor). Real runner, real SessionStore, real SessionDB."""
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_state import AsyncSessionDB, SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    db = SessionDB(db_path=tmp_path / "fixture.db")
    db.create_session(entry.session_id, source="telegram", session_key=entry.session_key,
                      model_config={"_delegate_from": "old-subagent-ancestor"})
    runner._session_db = AsyncSessionDB(db)
    runner._cache_session_source = lambda *a: None
    runner._recover_telegram_topic_thread_id = lambda s: None
    return runner, source, entry, db


def _kanban_wake(source, entry, pinned):
    from gateway.platforms.base import MessageEvent

    return MessageEvent(text="[boomerang-wake] child surfaced t_x", source=source, internal=True,
                        metadata={"kanban_owner_wake": True, "card_id": "t_x",
                                  "gateway_session_key": entry.session_key,
                                  "gateway_session_id": pinned})


@pytest.mark.asyncio
async def test_kanban_wake_pinned_to_route_own_session_starts_turn(tmp_path, monkeypatch):
    """The drop point: an owner/boomerang wake pins gateway_session_id to the route's own live
    session; the #140 child-takeover refusal dropped it after admission (no turn, no page)."""
    from gateway import turn_error_pager

    pages = []
    monkeypatch.setattr(turn_error_pager, "page_dropped_wake", lambda *a, **k: pages.append(a) or True,
                        raising=False)
    runner, source, entry, db = _delegated_route_fixture(tmp_path, monkeypatch)
    try:
        resolved = await runner._hmwa_resolve_session(_kanban_wake(source, entry, entry.session_id), source)
        assert resolved is not None, "wake to the route's own session was dropped"
        assert resolved[1].session_id == entry.session_id
        assert pages == []
        assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
        # Same class, async-completion pre-flight: the route's own session is a human owner.
        assert await runner._classify_completion_target(entry.session_id) == "deliver"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_kanban_wake_dropped_by_child_refusal_pages(tmp_path, monkeypatch):
    """#140 still holds for a pin to a DIFFERENT delegated session, and an admitted kanban wake that
    is dropped there is never silent: it pages."""
    from gateway import turn_error_pager

    pages = []
    monkeypatch.setattr(turn_error_pager, "page_dropped_wake", lambda *a, **k: pages.append(a) or True,
                        raising=False)
    runner, source, entry, db = _delegated_route_fixture(tmp_path, monkeypatch)
    db.create_session("child", source="telegram", model_config={"_delegate_from": entry.session_id})
    try:
        assert await runner._hmwa_resolve_session(_kanban_wake(source, entry, "child"), source) is None
        assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
        assert len(pages) == 1 and "child" in pages[0][1]
        assert await runner._classify_completion_target("child") == "retry"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_page_dropped_wake_once_per_room_episode(monkeypatch):
    from types import SimpleNamespace

    from gateway import turn_error_pager

    sent = []
    monkeypatch.setattr(turn_error_pager, "_dispatch", lambda args, key: sent.append(args))
    monkeypatch.setattr(turn_error_pager, "_open_episodes", {})
    event = SimpleNamespace(source=SimpleNamespace(platform="discord", chat_id="155"),
                            metadata={"card_id": "t_x"})
    assert turn_error_pager.page_dropped_wake(event, "refused pin", "k") is True
    assert turn_error_pager.page_dropped_wake(event, "refused pin", "k") is False
    assert len(sent) == 1
    assert sent[0][:5] == ["page", "--producer", turn_error_pager.WAKE_DROP_PRODUCER, "--key", "discord:155"]
    assert "t_x" in sent[0][6] and "refused pin" in sent[0][6]
    for handle in turn_error_pager._open_episodes.values():
        handle.cancel()
