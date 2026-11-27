"""Unit tests for agent.turn_facade_lease (admission + lease bracket)."""
import threading
from types import SimpleNamespace

from agent.turn_facade_lease import (
    LEASE_TTL_SECONDS,
    DurableTurnLease,
    admit_durable_turn_lease,
)


class _Db:
    def __init__(self, exists=True, acquired=True):
        self.exists = exists
        self.acquired = acquired
        self.events = []

    def get_session(self, session_id):
        return {"id": session_id} if self.exists else None

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("acquire", session_id, holder))
        return self.acquired

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        self.events.append(("release", session_id, holder))


def _agent(db, **overrides):
    agent = SimpleNamespace(
        _session_db=db,
        session_id="s1",
        _persist_disabled=False,
        _interrupt_requested=False,
        _interrupt_message=None,
        _execution_thread_id=None,
        _session_turn_lease_refresh_interval=60.0,
        statuses=[],
    )
    agent._emit_status = agent.statuses.append
    agent._emit_warning = agent.statuses.append
    agent._touch_activity = lambda *a, **k: None
    agent._liveness_activity_lock = lambda: threading.Lock()
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


def _admit(agent, history=None):
    return admit_durable_turn_lease(
        agent,
        session_id="s1",
        relay_turn_id="s1:t:abcd",
        task_context={"session_id": "s1", "task_id": "t", "platform": "cli"},
        conversation_history=history,
    )


def test_fresh_row_leases_with_seed_kept_and_persist_disabled_skips(monkeypatch):
    monkeypatch.setattr(
        "agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0)
    )
    seed = [{"role": "user", "content": "hi"}]
    agent = _agent(_Db(exists=False))
    admission = _admit(agent, seed)
    assert admission.lease is not None and admission.early_result is None
    assert admission.conversation_history is seed
    assert getattr(agent, "_session_db_created", False) is False
    admission.lease.release()

    db = _Db()
    admission = _admit(_agent(db, _persist_disabled=True), seed)
    assert admission.lease is None and db.events == []


def test_admission_sets_holder_attrs_and_release_clears_them(monkeypatch):
    monkeypatch.setattr(
        "agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0)
    )
    db = _Db()
    agent = _agent(db)
    admission = _admit(agent)
    lease = admission.lease
    assert isinstance(lease, DurableTurnLease)
    assert agent._session_db_created is True
    assert agent._active_session_turn_lease_holder == lease.holder
    assert agent._active_session_turn_lease_ttl_seconds == LEASE_TTL_SECONDS
    assert lease.holder.startswith("pid=") and ":platform=cli" in lease.holder
    assert lease.watchdog is None and lease.timer_handles == []
    assert lease.is_turn_active() is False

    lease.stop_refresher()
    lease.join_threads()
    lease.clear_interrupt()
    lease.release()
    assert db.events == [("acquire", "s1", lease.holder), ("release", "s1", lease.holder)]
    assert agent._active_session_turn_lease_holder is None
    assert agent._active_session_turn_lease_ttl_seconds is None


def test_timeout_and_interrupt_early_results():
    agent = _agent(_Db(acquired=False))
    admission = _admit(agent, [{"role": "user", "content": "x"}])
    assert admission.lease is None
    assert admission.early_result["failed"] is True
    assert admission.early_result["error"] == "session_turn_lease_timeout:s1"
    # Stamped so UI descriptors show "session busy" instead of code="unknown".
    assert admission.early_result["failure_reason"] == "session_busy"
    assert admission.early_result["failure_retryable"] is True
    assert admission.early_result["messages"] == [{"role": "user", "content": "x"}]

    agent = _agent(_Db(acquired=False), _interrupt_requested=True, _interrupt_message="stop")
    agent.clear_interrupt = lambda: None
    admission = _admit(agent)
    assert admission.early_result["interrupted"] is True
    assert admission.early_result["interrupt_message"] == "stop"


def test_interrupt_turn_only_while_active():
    agent = _agent(_Db())
    calls = []
    agent.interrupt = lambda msg, **kw: calls.append(msg)
    lease = DurableTurnLease(agent, agent._session_db, "s1", "h")
    lease._interrupt_turn("lost")  # inactive: ignored
    assert calls == [] and lease.interrupt_message is None
    lease.turn_active = True
    lease._interrupt_turn("lost")
    assert calls == ["lost"] and lease.interrupt_message == "lost"
    lease.deactivate_after_liveness_abort()
    assert lease.stop.is_set() and lease.is_turn_active() is False


_REFRESH_FAILED_MSG = "Session turn lease could not be refreshed; stopping to protect the transcript."


def _refresh_lease(monkeypatch, outcomes):
    """A live lease whose refreshes follow ``outcomes`` (True/False or an exception to raise) on
    a fake clock; backoff waits advance the clock instead of sleeping."""
    import sqlite3

    from agent import turn_facade_lease as mod

    clock = [1000.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    db = _Db()
    calls = []

    def refresh(session_id, holder, **kwargs):
        calls.append(clock[0])
        outcome = outcomes.pop(0) if outcomes else sqlite3.OperationalError("database is locked")
        if isinstance(outcome, BaseException):
            clock[0] += 20.0  # the busy timeout spent before sqlite gives up
            raise outcome
        return outcome

    db.refresh_session_turn_lease = refresh
    agent = _agent(db)
    interrupts = []
    agent.interrupt = lambda msg, **kw: interrupts.append(msg)
    lease = DurableTurnLease(agent, db, "s1", "h")
    lease.turn_active = True
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return lease.stop.is_set()

    lease._backoff_wait = fake_wait
    clock[0] += 60.0  # first scheduled tick: one refresh interval after admission
    return lease, clock, calls, interrupts, waits


def test_refresh_locked_once_then_ok_does_not_interrupt(monkeypatch):
    import sqlite3

    lease, clock, calls, interrupts, waits = _refresh_lease(
        monkeypatch, [sqlite3.OperationalError("database is locked"), True]
    )
    assert lease.refresh_tick() is None  # timer keeps running
    assert interrupts == [] and lease.interrupt_message is None
    assert len(calls) == 2 and waits == [5.0]
    assert lease.last_refresh_ok == calls[1]


def test_refresh_locked_past_ttl_then_ok_kills_no_turn(monkeypatch):
    """2026-11-10 18:59 / 19:29: nine kanban workers exited 130 in two bursts. Each one's refresh
    raised "database is locked" for ~290 s while other workers held the state.db writer 37-80 s
    at a time, and the refresher interrupted the turn although its row was never taken. A locked
    write proves nothing about ownership: retry past the TTL, renew when the lock clears."""
    import sqlite3

    locked = [sqlite3.OperationalError("database is locked")] * 20
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, locked + [True])
    acquired_at = clock[0] - 60.0
    _with_owner_db(lease, lambda reads: "h")  # a WAL read still names this holder
    assert lease.refresh_tick() is None  # timer keeps running
    assert clock[0] - acquired_at > LEASE_TTL_SECONDS  # the lock really outlasted the TTL
    assert interrupts == [] and lease.interrupt_message is None
    assert len(calls) == 21 and len(waits) == 20
    assert max(waits) <= 30.0  # backoff stays capped while it keeps trying
    assert lease.last_refresh_ok == calls[-1]


def test_refresh_non_lock_error_interrupts_at_ttl_margin(monkeypatch):
    from agent import turn_facade_lease as mod

    errors = [OSError("disk I/O error")] * 40
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, errors)
    acquired_at = clock[0] - 60.0
    assert lease.refresh_tick() is False
    assert interrupts == [_REFRESH_FAILED_MSG]
    # Gave up only once the lease could actually lapse, never with real validity left.
    assert clock[0] - acquired_at >= LEASE_TTL_SECONDS - mod.LEASE_REFRESH_SAFETY_MARGIN_SECONDS
    assert clock[0] - acquired_at < LEASE_TTL_SECONDS
    assert len(waits) == len(calls) - 1


def test_refresh_locked_then_row_taken_over_interrupts_once(monkeypatch):
    """The holder-qualified UPDATE is the ownership answer: once the lock clears and the row
    names another holder, the turn stops."""
    import sqlite3

    locked = [sqlite3.OperationalError("database is locked")] * 20
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, locked + [False])
    _with_owner_db(lease, lambda reads: "h")
    assert lease.refresh_tick() is False
    assert interrupts == ["Session turn lease lost; stopping to protect the transcript."]
    assert len(calls) == 21


class _OwnerDb(_Db):
    """A _Db whose lock-free holder read follows ``owners`` (a holder string, or an exception)."""

    def __init__(self, owners):
        super().__init__()
        self.owners = owners
        self.reads = []

    def get_session_turn_lease_holder(self, session_id):
        owner = self.owners(self.reads)
        self.reads.append(owner)
        if isinstance(owner, BaseException):
            raise owner
        return owner


def _with_owner_db(lease, owners):
    owner_db = _OwnerDb(owners)
    owner_db.refresh_session_turn_lease = lease.db.refresh_session_turn_lease
    lease.db = owner_db
    return owner_db


def test_refresh_locked_successor_claims_expired_row_interrupts_within_one_step(monkeypatch):
    """Prism 598ed7150791 repro: renewal locked past the TTL, the writer clears long enough for a
    successor to claim the expired row, then locks again. The original turn must stop at its next
    re-check (one 5 s step plus one busy timeout), not run on beside the successor."""
    import sqlite3

    locked = [sqlite3.OperationalError("database is locked")] * 40
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, locked)
    acquired_at = clock[0] - 60.0
    takeover_at = acquired_at + LEASE_TTL_SECONDS + 1.0
    db = _with_owner_db(lease, lambda reads: "succ" if clock[0] >= takeover_at else "h")
    assert lease.refresh_tick() is False
    assert interrupts == ["Session turn lease lost; stopping to protect the transcript."]
    assert db.reads[-1] == "succ" and "h" in db.reads
    assert clock[0] - takeover_at <= 5.0 + 20.0  # one re-check step + one busy timeout
    assert waits[-2:] == [5.0, 5.0]  # past the margin it re-checks at the shortest step


def test_refresh_locked_and_owner_read_fails_keeps_ttl_margin_give_up(monkeypatch):
    import sqlite3

    from agent import turn_facade_lease as mod

    locked = [sqlite3.OperationalError("database is locked")] * 40
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, locked)
    acquired_at = clock[0] - 60.0
    _with_owner_db(lease, lambda reads: sqlite3.OperationalError("disk I/O error"))
    assert lease.refresh_tick() is False
    assert interrupts == [_REFRESH_FAILED_MSG]
    assert clock[0] - acquired_at < LEASE_TTL_SECONDS
    assert clock[0] - acquired_at >= LEASE_TTL_SECONDS - mod.LEASE_REFRESH_SAFETY_MARGIN_SECONDS


def test_refresh_returning_false_interrupts_immediately(monkeypatch):
    lease, clock, calls, interrupts, waits = _refresh_lease(monkeypatch, [False])
    assert lease.refresh_tick() is False
    assert interrupts == ["Session turn lease lost; stopping to protect the transcript."]
    assert len(calls) == 1 and waits == []


def test_real_state_db_write_lock_past_busy_timeout_kills_no_turn(tmp_path, monkeypatch):
    """2026-10-18 04:18: a VACUUM held the state.db writer past the write patience and every live
    turn's refresh raised "database is locked". Against a REAL SessionDB, a foreign writer holding
    the lock for several write-patience windows must kill zero turns, and the row is renewed once
    the lock clears. Time is scaled down (patience 0.2 s, TTL 8 s, lock 3 s > the writer's
    1 s busy timeout); the shape is the incident's."""
    import sqlite3
    import time

    from agent import turn_facade_lease as mod
    from hermes_state import SessionDB

    monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 0.2)
    monkeypatch.setattr(mod, "LEASE_TTL_SECONDS", 8.0)
    monkeypatch.setattr(mod, "LEASE_REFRESH_SAFETY_MARGIN_SECONDS", 2.0)
    monkeypatch.setattr(mod, "_REFRESH_RETRY_BACKOFF_SECONDS", (0.1, 0.2))
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s1", source="test")
    holder = "pid=0:turn=live"
    assert db.try_acquire_session_turn_lease("s1", holder, ttl_seconds=8.0)
    agent = _agent(db)
    interrupts = []
    agent.interrupt = lambda msg, **kw: interrupts.append(msg)
    lease = DurableTurnLease(agent, db, "s1", holder)
    lease.turn_active = True

    def expires_at():
        with sqlite3.connect(path) as c:
            return c.execute("SELECT expires_at FROM session_turn_leases").fetchone()[0]

    before = expires_at()
    blocker = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")  # the foreign writer (VACUUM stand-in)
    threading.Timer(3.0, lambda: blocker.execute("COMMIT")).start()
    started = time.monotonic()
    try:
        assert lease.refresh_tick() is None
    finally:
        try:
            blocker.execute("COMMIT")
        except sqlite3.Error:
            pass
        blocker.close()
    assert time.monotonic() - started >= 3.0  # it really waited out the lock
    assert interrupts == [] and lease.interrupt_message is None
    assert expires_at() > before
    db.release_session_turn_lease("s1", holder)


def test_refresh_retry_stops_quietly_when_turn_ends(monkeypatch):
    import sqlite3

    lease, clock, calls, interrupts, waits = _refresh_lease(
        monkeypatch, [sqlite3.OperationalError("database is locked")]
    )

    def wait_then_stop(seconds):
        lease.stop_refresher()
        return True

    lease._backoff_wait = wait_then_stop
    assert lease.refresh_tick() is False
    assert interrupts == [] and len(calls) == 1
