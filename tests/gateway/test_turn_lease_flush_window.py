"""Turn-lease flush window: slot released, lease still held (t_d659b247, 2026-10-24 14:45).

Incident (Apollo default gateway, routing key ``agent:main:discord:group:1535189663533506600:...``):

1. gen 2 ``response ready``; ``_run_agent``'s finally (``_run_agent_cleanup_turn_tasks``) cleared
   the turn SLOT, so ``_is_session_running()`` read IDLE.
2. gen 2 was still inside ``_handle_message_with_agent_admitted`` persisting + delivering (a
   ~170 s flush on a 6 GB state.db). The turn LEASE is held through that flush by design and
   released only in ``_handle_message``'s finally.
3. A same-key inbound (kanban wake) saw the empty slot, was dispatched as a FRESH turn (gen 3)
   and reached ``SessionTurnLeaseRegistry.acquire()``.
4. ``_holder_is_stale(gen 2)`` = ``not is_current(gen 2)`` = True, because gen 3 now existed. The
   live flushing holder was logged as "stopped and still draining", the wait was clipped to 5 s,
   ``TurnLeaseTimeoutError`` -> "Another turn is still running... resend" bounce.
5. A user prompt queued behind gen 3 (pending sentinel) was fed into the same rejection and lost.

Three R-blocks, all through the REAL ``_handle_message`` (real ``GatewayRunner.__init__`` under a
temp home, real ``BasePlatformAdapter``, ``_run_agent`` stubbed, the persist step gated so the
flush can be held open):

R-1  an inbound during the flush window is QUEUED (slot/overflow), not dispatched; no bounce, no
     ``TurnLeaseTimeoutError``; releasing the flush runs it next, in order, ``max_live == 1``.
R-2  no "STALE turn" / ``PHASE=stale_lease_holder`` for a superseded LIVE holder; a /stop-
     invalidated holder is still stale (registry predicate, D-2).
R-3  the fail-closed rejection (alias-key lease timeout) never drops a user item queued behind the
     rejected generation: it is HELD and the bounce names the count.
"""

import asyncio
import logging
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.turn_lease import SessionTurnLeaseRegistry, TurnLeaseTimeoutError

SESSION_ID = "sess-flush-window"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(str(content))
        return SendResult(success=True, message_id="m1")

    async def edit_message(self, chat_id, message_id, content, finalize=False) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345")


KEY = build_session_key(_source())


def _msg(text, *, internal=False):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), message_id=text,
                        internal=internal)


async def _wait_for(predicate, what, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


class _Rig:
    """Real runner + real adapter; ``_run_agent`` stubbed; persist step gated per turn."""

    def __init__(self, monkeypatch, tmp_path, *, lease_timeout="0.3"):
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
        # Short budgets: the base-sha RED is a prompt bounce, not a 5 s stall.
        monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", lease_timeout)
        monkeypatch.setenv("HERMES_STALE_LEASE_WAIT", lease_timeout)
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true")
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
        monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **k: 100_000)

        runner = gateway_run.GatewayRunner(GatewayConfig())
        # The runner's own registry reads the env budgets at construction; rebuild it so the
        # monkeypatched values apply regardless of import-time defaults. On the base sha the
        # registry has no ``is_generation_invalidated`` kwarg: fall back so the RED is behavioural.
        reg_kwargs = dict(is_generation_current=runner._is_session_run_current, stale_wait=float(lease_timeout))
        if hasattr(runner, "_is_session_run_invalidated"):
            reg_kwargs["is_generation_invalidated"] = runner._is_session_run_invalidated
        runner._turn_leases = SessionTurnLeaseRegistry(**reg_kwargs)
        self.adapter = adapter = _Adapter()
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._delivery_adapter_for = lambda source: adapter
        runner._is_user_authorized = lambda _source: True
        runner._set_session_env = lambda _context: None
        runner._session_db = MagicMock()
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._cache_session_source = lambda _key, _source: None
        runner._reply_anchor_for_event = lambda _event: None
        runner._get_guild_id = lambda _event: None
        runner._should_send_voice_reply = lambda *_a, **_kw: False
        runner._run_post_turn_hooks = AsyncMock()
        runner.hooks = MagicMock()
        runner.hooks.emit = AsyncMock()
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = SessionEntry(
            session_key=KEY, session_id=SESSION_ID, created_at=datetime.now(),
            updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="group",
        )
        runner.session_store.load_transcript.return_value = []
        runner.session_store.append_to_transcript = MagicMock()
        runner.session_store.has_platform_message_id.return_value = False
        runner.session_store.transcript_tail_role.return_value = "user"
        runner.session_store.update_session = MagicMock()
        runner.session_store.clear_turn_active = lambda *_a, **_k: True
        self.runner = runner

        self.calls, self.live, self.max_live = [], 0, 0
        self.flush_gates = {}  # message text -> asyncio.Event the persist step waits on
        self.flush_entered = {}
        rig = self

        async def fake_run_agent(message, **kwargs):
            rig.live += 1
            rig.max_live = max(rig.max_live, rig.live)
            rig.calls.append(message)
            # _run_agent's finally releases the SLOT while the dispatch layer still holds the lease.
            runner._release_running_agent_state(kwargs["session_key"], run_generation=kwargs["run_generation"])
            return {"final_response": f"answer {message}", "messages": [], "api_calls": 1,
                    "history_offset": 0, "last_prompt_tokens": 0}

        runner._run_agent = fake_run_agent
        real_persist = runner._hmwa_persist_turn_transcript

        async def gated_persist(*args, **kwargs):
            text = (kwargs["event"].text or "").split("\n\n")[0]
            gate = rig.flush_gates.get(text)
            rig.flush_entered.setdefault(text, asyncio.Event()).set()
            if gate is not None:
                await gate.wait()
            try:
                return await real_persist(*args, **kwargs)
            finally:
                rig.live -= 1

        runner._hmwa_persist_turn_transcript = gated_persist
        adapter.set_message_handler(runner._handle_message)

    def hold_flush(self, text):
        gate = self.flush_gates[text] = asyncio.Event()
        self.flush_entered[text] = asyncio.Event()
        return gate

    async def settle(self, expect_calls, timeout=5.0):
        await _wait_for(
            lambda: len(self.calls) >= expect_calls
            and not any(not t.done() for t in list(self.adapter._session_tasks.values())),
            f"{expect_calls} turns to finish", timeout,
        )


# ---------------------------------------------------------------------------
# R-1: inbound during the flush window is queued, then runs next in order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_inbound_during_flush_is_queued_then_runs_in_order(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path)
    gate = rig.hold_flush("first")
    caplog.set_level(logging.INFO)

    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(rig.flush_entered["first"].is_set, "first turn to reach its flush")
    # The incident window: slot released, lease still held by gen 1.
    assert not rig.runner._is_session_running(KEY), "precondition: slot already released"
    state = rig.runner._peek_session_state(KEY)
    assert rig.runner._turn_leases.is_holder(state.turn.lease_tokens[1]), "precondition: lease still held"

    # Same-key inbound reaching the runner directly (the fresh-dispatch door of the incident).
    response = await asyncio.wait_for(rig.runner._handle_message(_msg("second")), timeout=5)

    assert response is None or "not processed" not in str(response).lower(), (
        f"flush-window inbound was bounced instead of queued: {response!r}"
    )
    queued = [e.text for e in [rig.adapter._pending_messages.get(KEY)] if e is not None]
    queued += [e.text for e in (rig.runner._overflow_queue(KEY) or [])]
    assert queued == ["second"], f"inbound not queued behind the flushing turn: {queued}"
    assert "turn lease wait timed out" not in caplog.text
    assert "Rejecting turn" not in caplog.text
    assert rig.calls == ["first"], "the queued item must not run while the lease is held"

    gate.set()
    await rig.settle(expect_calls=2)
    assert rig.calls == ["first", "second"]
    assert rig.max_live == 1
    assert not any("not processed" in s.lower() for s in rig.adapter.sent), rig.adapter.sent
    assert rig.adapter._pending_messages.get(KEY) is None
    assert not rig.runner._overflow_queue(KEY)


@pytest.mark.asyncio
async def test_r1_internal_wake_during_flush_is_queued_silently(monkeypatch, tmp_path, caplog):
    """The incident's inbound was a kanban wake: queued, no ack, no bounce, runs after the flush."""
    rig = _Rig(monkeypatch, tmp_path)
    gate = rig.hold_flush("first")
    caplog.set_level(logging.INFO)

    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(rig.flush_entered["first"].is_set, "first turn to reach its flush")

    response = await asyncio.wait_for(rig.runner._handle_message(_msg("wake", internal=True)), timeout=5)
    assert response is None, f"internal wake produced a reply during the flush window: {response!r}"
    assert "Rejecting turn" not in caplog.text
    slot = rig.adapter._pending_messages.get(KEY)
    assert slot is not None and slot.text == "wake"

    gate.set()
    await rig.settle(expect_calls=2)
    assert rig.calls == ["first", "wake"]
    assert rig.max_live == 1


@pytest.mark.asyncio
async def test_r1_requeue_laps_log_the_flush_queue_once(monkeypatch, tmp_path, caplog):
    """The requeue back-off re-dispatches a queued wake about once a second for the whole flush;
    each lap re-queues it. One INFO line per flush window, not one per lap (94 lines on 10-25)."""
    rig = _Rig(monkeypatch, tmp_path)
    gate = rig.hold_flush("first")
    caplog.set_level(logging.INFO, logger="gateway.run")

    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(rig.flush_entered["first"].is_set, "first turn to reach its flush")
    for _ in range(3):
        await asyncio.wait_for(rig.runner._handle_message(_msg("wake", internal=True)), timeout=5)
        rig.adapter._pending_messages.pop(KEY, None)  # the adapter's back-off lap takes it again
    info = [r for r in caplog.records if r.levelno == logging.INFO and "PHASE=lease_flush_queued" in r.getMessage()]
    assert len(info) == 1, [r.getMessage()[:80] for r in info]

    gate.set()
    rig.adapter._pending_messages[KEY] = _msg("wake", internal=True)
    await rig.settle(expect_calls=2)


@pytest.mark.asyncio
async def test_r1_adapter_door_inbound_behind_direct_path_holder(monkeypatch, tmp_path, caplog):
    """Holder entered the runner directly (boot resume / slash re-dispatch: no adapter guard), so
    the next message comes through ``adapter.handle_message`` and reaches ``_handle_message`` as a
    fresh dispatch. It is queued, acked ONCE, re-tried by the adapter's requeue back-off without a
    bounce, and runs as soon as the lease frees."""
    rig = _Rig(monkeypatch, tmp_path)
    gate = rig.hold_flush("first")
    caplog.set_level(logging.INFO)

    holder = asyncio.create_task(rig.runner._handle_message(_msg("first")))
    await _wait_for(rig.flush_entered["first"].is_set, "first turn to reach its flush")
    assert KEY not in rig.adapter._active_sessions, "precondition: holder has no adapter guard"

    await rig.adapter.handle_message(_msg("second"))
    await asyncio.sleep(1.5)  # several requeue back-off laps while the lease is held
    assert rig.calls == ["first"]
    acks = [s for s in rig.adapter.sent if "Queued for the next turn" in s]
    assert len(acks) == 1, f"expected exactly one queued ack, got {rig.adapter.sent}"
    assert "Rejecting turn" not in caplog.text

    gate.set()
    await asyncio.wait_for(holder, 5)
    await rig.settle(expect_calls=2)
    assert rig.calls == ["first", "second"]
    assert rig.max_live == 1
    assert not any("not processed" in s.lower() for s in rig.adapter.sent), rig.adapter.sent


# ---------------------------------------------------------------------------
# R-2: superseded live holder is NOT stale; /stop-invalidated holder IS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_no_stale_holder_warning_for_superseded_live_turn(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path)
    gate = rig.hold_flush("first")
    caplog.set_level(logging.DEBUG)

    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(rig.flush_entered["first"].is_set, "first turn to reach its flush")
    await asyncio.wait_for(rig.runner._handle_message(_msg("second")), timeout=5)

    assert "held by a STALE turn" not in caplog.text, "live flushing holder logged as stopped/draining"
    assert "PHASE=stale_lease_holder" not in caplog.text
    gate.set()
    await rig.settle(expect_calls=2)


@pytest.mark.asyncio
async def test_r2_registry_stale_means_invalidated_not_superseded(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    runner, registry = rig.runner, rig.runner._turn_leases

    gen1 = runner._begin_session_run_generation(KEY)
    holder = await registry.acquire(SESSION_ID, owner_key=KEY, generation=gen1, timeout=1)
    assert holder is not None
    try:
        assert registry._holder_is_stale(holder) is False

        # A later inbound's own generation bump SUPERSEDES gen 1; nothing stopped it.
        runner._begin_session_run_generation(KEY)
        assert registry._holder_is_stale(holder) is False, (
            "a superseded live holder was read as stopped-and-draining"
        )

        # /stop on the turn that holds the lease: now it IS stopped and draining.
        gen_held_now = runner._begin_session_run_generation(KEY)
        stopped = await registry.acquire(SESSION_ID + "-b", owner_key=KEY, generation=gen_held_now, timeout=1)
        assert registry._holder_is_stale(stopped) is False
        runner._invalidate_session_run_generation(KEY, reason="stop_command")
        assert registry._holder_is_stale(stopped) is True
        # Invalidation is a watermark: /stop stops whatever the session was doing, so every older
        # generation (gen 1 included) is stopped-and-draining too. Only a generation ABOVE the
        # watermark that is not current is "superseded", never "invalidated".
        assert registry._holder_is_stale(holder) is True
        after_stop = runner._begin_session_run_generation(KEY)
        runner._begin_session_run_generation(KEY)  # supersedes after_stop without stopping it
        assert runner._is_session_run_invalidated(KEY, after_stop) is False
        assert runner._is_session_run_current(KEY, after_stop) is False
        registry.release(stopped)
    finally:
        registry.release(holder)


def test_r2_registry_fails_open_without_invalidation_predicate():
    """Standalone embedders / older rigs that supply only ``is_generation_current`` keep the
    pre-fix semantics; no predicate at all => every holder is LIVE."""
    async def scenario():
        current = {"gen": 1}
        registry = SessionTurnLeaseRegistry(is_generation_current=lambda key, gen: current["gen"] == gen)
        token = await registry.acquire("s", owner_key="k", generation=1, timeout=1)
        assert registry._holder_is_stale(token) is False
        current["gen"] = 2
        assert registry._holder_is_stale(token) is True
        registry.release(token)

        bare = SessionTurnLeaseRegistry()
        token = await bare.acquire("s", owner_key="k", generation=1, timeout=1)
        assert bare._holder_is_stale(token) is False
        bare.release(token)

        def boom(key, gen):
            raise RuntimeError("predicate bug")

        failing = SessionTurnLeaseRegistry(is_generation_invalidated=boom)
        token = await failing.acquire("s", owner_key="k", generation=1, timeout=1)
        assert failing._holder_is_stale(token) is False
        failing.release(token)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# R-3: the fail-closed rejection keeps the user item queued behind it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_rejection_holds_queued_user_item_and_names_the_count(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path, lease_timeout="0.05")
    runner, registry = rig.runner, rig.runner._turn_leases
    caplog.set_level(logging.INFO)

    # Alias-key contention: another routing key holds this session's lease (the case the lease
    # exists for, and the one D-1 cannot see because the token lives on the other key).
    other = await registry.acquire(SESSION_ID, owner_key="agent:main:telegram:dm:other", generation=7, timeout=1)
    assert other is not None
    real_acquire = registry.acquire
    behind_queued = asyncio.Event()

    async def acquire_then_queue_behind(session_id, **kwargs):
        # While this turn holds the pending sentinel, a user prompt arrives and is queued behind it
        # (the incident's voice message at 14:45:06).
        reply = await runner._handle_message(_msg("behind"))
        assert reply is None, reply
        behind_queued.set()
        return await real_acquire(session_id, **kwargs)

    registry.acquire = acquire_then_queue_behind
    try:
        response = await asyncio.wait_for(runner._handle_message(_msg("rejected")), timeout=5)
    finally:
        registry.acquire = real_acquire
        registry.release(other)

    assert behind_queued.is_set()
    assert isinstance(response, str) and "not processed" in response.lower(), response
    assert rig.calls == [], "the rejected turn must not have run"
    survivors = list(runner._peek_session_state(KEY).conversation.held_queue)
    survivors += [e for e in [rig.adapter._pending_messages.get(KEY)] if e is not None]
    survivors += list(runner._overflow_queue(KEY) or [])
    assert [e.text for e in survivors] == ["behind"], f"queued user item dropped on rejection: {survivors}"
    assert "1 queued prompt" in response, f"bounce does not name the kept count: {response!r}"
    assert "resend" in response.lower()


def test_r2_every_raw_generation_bump_records_the_invalidation_watermark():
    """Source contract (t_d659b247 review): a raw ``persistent.run_generation += 1`` outside the
    ``_begin_session_run_generation`` funnel is an invalidation (idle /stop) and must raise
    ``invalidated_run_generation`` first, or a flushing holder reads as live after /stop."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "gateway"
    offenders = []
    for path in root.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "run_generation += 1" in line and "invalidated" not in line:
                window = "\n".join(lines[max(0, i - 8):i])
                if "invalidated_run_generation" not in window:
                    offenders.append(f"{path.name}:{i + 1}")
    assert not offenders, f"generation bump without invalidation watermark: {offenders}"
