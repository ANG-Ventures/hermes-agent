"""Fork canary: goal/loop orphan GC (t_92fc3ff0).

A ``/goal`` or ``/loop`` row is keyed by session id. On 2026-11-08 the Studio's default profile held
205 active goals; 190 sat on session ids no gateway routing key pointed at (oldest 2026-05-12). They
never woke anyone, yet every 15 s idle-wake scan reloaded them. Cause: the gateway's reset
rotations (``/new``, ``/reset``, a suspended auto-reset, the compression-exhausted reset) all end the
old session through ``SessionStore._finish_route_transition`` and nothing retired its goal/loop.

Contracts guarded here (behavior, not snapshots):
1. A reset rotation retires the old session's ACTIVE goal and loop as ``superseded`` (the transcript
   is dropped, so the goal never carried; ``/goal resume`` restores) and never touches a non-active row.
2. The hourly reaper retires (``orphaned``) an active goal/loop only when its session is unrouted AND
   idle >= ``goals.orphan_gc_days``; recent session-store activity (a CLI session) and routing protect it;
   ``0`` disables it.
3. Routing is read gateway-wide, so a goal routed only under a secondary profile is kept, while that
   profile's own store is still reaped.
4. Retire, never delete: an orphaned goal/loop round-trips to active through the resume path.
5. The reaper rides the goal idle-wake watcher (no new thread or job), throttled to once per hour.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import goals, loops
from hermes_state import SessionDB

FEATURE = "goal-loop-orphan-gc"
DAY = 86400.0


def _db(path) -> SessionDB:
    return SessionDB(db_path=path)


def _put_goal(db, sid, *, status="active", age_days=0.0, now=None):
    now = time.time() if now is None else now
    state = goals.GoalState(goal=f"goal for {sid}", status=status, created_at=now - age_days * DAY)
    db.set_meta(f"goal:{sid}", state.to_json())


def _put_loop(db, sid, *, status="active", age_days=0.0, now=None):
    now = time.time() if now is None else now
    state = loops.LoopState(prompt=f"tick {sid}", status=status, interval_seconds=300.0,
                            created_at=now - age_days * DAY)
    db.set_meta(f"loop:{sid}", state.to_json())


def _goal(db, sid):
    raw = db.get_meta_committed(f"goal:{sid}")
    return goals.GoalState.from_json(raw) if raw else None


def _loop(db, sid):
    raw = db.get_meta_committed(f"loop:{sid}")
    return loops.LoopState.from_json(raw) if raw else None


def _session_row(db, sid, *, last_active):
    """A sessions row whose start, last message and effective_last_active all read *last_active*."""
    db.create_session(session_id=sid, source="cli")

    def _txn(conn):
        conn.execute("UPDATE sessions SET started_at = ?, effective_last_active = ?, last_activity_at = ? "
                     "WHERE id = ?", (last_active, last_active, last_active, sid))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', ?)",
                     (sid, last_active))

    db._execute_write(_txn)


@pytest.fixture
def store_rig(tmp_path, monkeypatch):
    """Real SessionStore over a real SessionDB; the goals/loops module store is the same file."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    db = _db(tmp_path / "state.db")
    store._db = db
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4242", chat_type="dm", user_id="4242")
    yield store, db, source
    db.close()


# ── 1. Rotation leak ──────────────────────────────────────────────────

def test_new_reset_retires_the_replaced_goal_and_loop(store_rig):
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id)
    _put_loop(db, a.session_id)

    b = store.reset_session(a.session_key)  # the /new and /reset store call
    assert b is not None and b.session_id != a.session_id

    # RED-proof (M1): delete the `self._retire_replaced_goal_and_loop(...)` call in
    # gateway/session_recovery.py::_finish_route_transition (~line 414) -> both rows stay "active".
    g, lp = _goal(db, a.session_id), _loop(db, a.session_id)
    assert g.status == "superseded" and "superseded" in (g.paused_reason or "")
    assert lp.status == "superseded"
    assert a.session_id not in goals.list_active_goal_session_ids()
    assert a.session_id not in {sid for sid, _ in loops.list_active_loops()}
    # A fresh start never carried the goal (existing /new semantics): the successor has none.
    assert _goal(db, b.session_id) is None and _loop(db, b.session_id) is None


def test_suspended_auto_reset_retires_the_replaced_goal(store_rig):
    """The automatic rotation (fork has no time-based expiry; ``suspended`` is the auto-reset)."""
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id)
    assert store.suspend_session(a.session_key)

    b = store.get_or_create_session(source)
    assert b.session_id != a.session_id and b.auto_reset_reason == "suspended"
    # RED-proof (M1): same edit as above; the auto-reset reaches the same funnel.
    assert _goal(db, a.session_id).status == "superseded"
    assert a.session_id not in goals.list_active_goal_session_ids()


@pytest.mark.parametrize("status", ["done", "paused", "cleared"])
def test_reset_never_touches_a_non_active_goal(store_rig, status):
    """Ace 2026-10-19: a done or /stop-latched goal never wakes; retirement only ever reads ACTIVE."""
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id, status=status)
    store.reset_session(a.session_key)
    # RED-proof (M6): in hermes_cli/goals.py::retire_goal (~line 883) drop
    # `or state.status != "active"` -> done/paused/cleared rows are re-stamped "superseded".
    assert _goal(db, a.session_id).status == status


# ── 2. Reaper ─────────────────────────────────────────────────────────

def test_orphaned_ids_pure_contract():
    now = 1_800_000_000.0
    activity = {"old-unrouted": now - 8 * DAY, "fresh-unrouted": now - 1 * DAY, "old-routed": now - 30 * DAY,
                "exactly-7d": now - 7 * DAY}
    # RED-proof (M2): drop `and (last or 0.0) <= cutoff` in
    # hermes_cli/goals.py::orphaned_goal_and_loop_ids (~line 905) -> "fresh-unrouted" is returned.
    assert goals.orphaned_goal_and_loop_ids(now, {"old-routed"}, activity, gc_days=7) == ["exactly-7d", "old-unrouted"]
    assert goals.orphaned_goal_and_loop_ids(now, set(), activity, gc_days=0) == []


def test_reaper_retires_only_unrouted_and_idle(tmp_path):
    db = _db(tmp_path / "state.db")
    now = time.time()
    try:
        _put_goal(db, "old-unrouted", age_days=8, now=now)
        _put_loop(db, "old-unrouted", age_days=8, now=now)
        _session_row(db, "old-unrouted", last_active=now - 8 * DAY)
        # CLI-like: the goal row is old, but the session store saw a message yesterday.
        _put_goal(db, "cli-recent", age_days=30, now=now)
        _session_row(db, "cli-recent", last_active=now - 1 * DAY)
        _put_goal(db, "routed-idle", age_days=30, now=now)
        _put_loop(db, "routed-idle", age_days=30, now=now)
        _put_goal(db, "old-done", status="done", age_days=60, now=now)

        counts = goals.reap_orphaned_goals_and_loops({"routed-idle"}, gc_days=7, now=now, db=db)

        assert counts == {"scanned": 3, "orphaned": 1, "loops": 1}
        g = _goal(db, "old-unrouted")
        assert g.status == "orphaned" and "orphan-gc" in g.paused_reason
        assert time.strftime("%Y-%m-%d", time.localtime(now)) in g.paused_reason
        # RED-proof (M21): in hermes_cli/goals.py::reap_orphaned_goals_and_loops (~line 1072) delete
        # `scanned[prefix] = written[1]` -> the loop's sibling fence sees our own goal write and keeps it.
        assert _loop(db, "old-unrouted").status == "orphaned"
        # RED-proof (M3): in hermes_cli/goals.py::_goal_loop_session_activity (~line 943) replace
        # `activity[sid] = max(activity[sid], ...)` with `pass` -> "cli-recent" is reaped.
        assert _goal(db, "cli-recent").status == "active"
        assert _goal(db, "routed-idle").status == "active" and _loop(db, "routed-idle").status == "active"
        assert _goal(db, "old-done").status == "done"   # never revived, never re-stamped
    finally:
        db.close()


def test_reaper_disabled_at_zero_days(tmp_path):
    db = _db(tmp_path / "state.db")
    try:
        _put_goal(db, "ancient", age_days=365)
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=0, db=db) == {"scanned": 0, "orphaned": 0, "loops": 0}
        assert _goal(db, "ancient").status == "active"
    finally:
        db.close()


class _HookAfterScanDB:
    """Delegates to a real SessionDB and runs *hook* right after the reaper's candidate scan (its
    last read, the sessions recency query) and before any retiring write: a deterministic stand-in
    for another writer landing between scan and retire. No sleeps."""

    def __init__(self, db, hook):
        self._db, self._hook, self.fired, self.writes = db, hook, 0, 0

    def __getattr__(self, name):
        return getattr(self._db, name)

    def _execute_write(self, *args, **kwargs):  # counts the reaper's retiring transactions
        self.writes += 1
        return self._db._execute_write(*args, **kwargs)

    def _read_all(self, *args, **kwargs):
        rows = self._db._read_all(*args, **kwargs)
        self._hook()
        self.fired += 1
        return rows


def _rewrite(db, key, cls, **fields):
    state = cls.from_json(db.get_meta_committed(key))
    for name, value in fields.items():
        setattr(state, name, value)
    db.set_meta(key, state.to_json())


def _message_now(db, sid):
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'back', ?)",
        (sid, time.time())))


# Every way a session turns live between the scan and the retiring write. Each must keep BOTH rows.
_LIVE_AFTER_SCAN = {
    # Prism f0a17e9ea50d: a message lands; the goal JSON is unchanged.
    "message": lambda db, routes: _message_now(db, "s"),
    # Prism 3f5809a119d6 (txn side): the API/tool/compaction heartbeat, before any message persists.
    "heartbeat": lambda db, routes: db.touch_session_activity("s", ts=time.time()),
    # Prism 257ff1d18304: the goal itself runs a turn.
    "goal-turn": lambda db, routes: _rewrite(db, "goal:s", goals.GoalState, last_turn_at=time.time()),
    # Prism f0485ed61fa5: user edits that move NO clock (the scanned-row fence is what keeps these).
    "goal-subgoal": lambda db, routes: goals.GoalManager("s").add_subgoal("new requirement"),
    "goal-gate": lambda db, routes: goals.GoalManager("s").add_gate("pytest -q"),
    "goal-contract": lambda db, routes: goals.GoalManager("s").set_contract(goals.GoalContract(outcome="ship it")),
    # Prism 4ca29f7a0858 (txn side): the sibling loop is re-armed (what resume() writes).
    "loop-rearm": lambda db, routes: _rewrite(db, "loop:s", loops.LoopState, next_due_at=time.time()),
    # Prism d6291f28e729: /resume (or a topic rebind) routes the session after the routing snapshot.
    "route": lambda db, routes: routes.add("s"),
}


@pytest.mark.parametrize("event", sorted(_LIVE_AFTER_SCAN))
def test_reaper_keeps_a_session_that_turned_live_after_the_scan(tmp_path, monkeypatch, event):
    db = _db(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    now = time.time()
    try:
        _put_goal(db, "s", age_days=9, now=now)
        _put_loop(db, "s", age_days=9, now=now)
        _session_row(db, "s", last_active=now - 8 * DAY)
        live_routes: set = set()
        hooked = _HookAfterScanDB(db, lambda: _LIVE_AFTER_SCAN[event](db, live_routes))

        counts = goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=hooked,
                                                     is_routed=live_routes.__contains__)

        assert hooked.fired == 1  # the interleaving really ran between scan and retire
        # RED-proof, all in hermes_cli/goals.py:
        # (M13) message/heartbeat: in _retire_if_still_orphaned (~line 1007) delete the
        #   `if srow is not None and float(srow[0] or 0.0) > cutoff: return None` session recheck.
        # (M10) goal-turn/goal-subgoal/goal-gate/goal-contract/loop-rearm: in _retire_if_still_orphaned
        #   (~line 993) drop `or raw != scanned.get(prefix)` (the scanned-row fence on the retired row).
        # (M19) goal-subgoal: in _retire_if_still_orphaned (~line 1000) delete the sibling fence
        #   `if sibling != scanned[p]: return None` -> the loop is retired beside the edited goal.
        # (M15) route: in reap_orphaned_goals_and_loops (~line 1062) delete the pre-write
        #   `if live_routed(sid): continue` -> the row is retired, then given back (visible flip).
        assert counts["orphaned"] == 0 and counts["loops"] == 0
        assert _goal(db, "s").status == "active" and _loop(db, "s").status == "active"
        if event == "route":  # routed before the write: no retiring write at all, not retire-then-undo
            assert hooked.writes == 0
    finally:
        db.close()


def test_sibling_loop_resumed_after_the_scan_keeps_the_goal(tmp_path, monkeypatch):
    """A loop that was PAUSED at scan time (so it has no scanned row to fence) is resumed before the
    goal's retiring write: its re-armed ``next_due_at`` is a live clock, so the goal is kept."""
    db = _db(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    now = time.time()
    try:
        _put_goal(db, "s", age_days=9, now=now)
        _put_loop(db, "s", status="paused", age_days=9, now=now)
        _session_row(db, "s", last_active=now - 8 * DAY)
        hooked = _HookAfterScanDB(db, lambda: loops.LoopManager("s").resume())
        counts = goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=hooked)
        assert hooked.fired == 1
        # RED-proof (M20): in hermes_cli/goals.py::_retire_if_still_orphaned (~line 1004) delete
        # `if last is not None and last > cutoff: return None` (the unscanned-sibling clock check).
        assert counts["orphaned"] == 0
        assert _goal(db, "s").status == "active" and _loop(db, "s").status == "active"
    finally:
        db.close()


def test_routing_is_never_read_while_the_db_write_lock_is_held(tmp_path):
    """Prism 0d90dba2499b: the gateway's routing lookup can load routing from this same SessionDB,
    whose write lock is a non-reentrant ``threading.Lock``. It must run outside ``_execute_write``."""
    db = _db(tmp_path / "state.db")
    now = time.time()
    held, calls = [], []

    def _is_routed(sid):
        calls.append(sid)
        if db._lock.locked():  # single-threaded test: only the reaper's own write could hold it
            held.append(sid)
        return False

    try:
        _put_goal(db, "s", age_days=9, now=now)
        _put_loop(db, "s", age_days=9, now=now)
        counts = goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=db, is_routed=_is_routed)
        assert counts == {"scanned": 1, "orphaned": 1, "loops": 1} and calls
        # RED-proof (M18): in hermes_cli/goals.py::reap_orphaned_goals_and_loops (~line 1064) wrap
        # `retire` so it calls live_routed(sid) (routing read back inside the write txn, the round-2 shape).
        assert held == []
    finally:
        db.close()


def test_route_landing_during_the_write_gives_the_row_back(tmp_path):
    """The session is unrouted at the pre-write check and routed by the post-commit check (a /resume
    landed in between): the retirement is undone byte-for-byte."""
    db = _db(tmp_path / "state.db")
    now = time.time()
    answers = iter([False, True])  # goal: pre-write check, post-commit check; then routed for good
    try:
        _put_goal(db, "s", age_days=9, now=now)
        _put_loop(db, "s", age_days=9, now=now)
        before_goal, before_loop = db.get_meta_committed("goal:s"), db.get_meta_committed("loop:s")
        counts = goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=db,
                                                     is_routed=lambda sid: next(answers, True))
        # RED-proof (M17): in hermes_cli/goals.py::reap_orphaned_goals_and_loops (~line 1069) delete
        # the post-commit `if live_routed(sid): db.compare_and_set_meta(...); continue` -> goal stays orphaned.
        assert counts["orphaned"] == 0 and counts["loops"] == 0
        assert db.get_meta_committed("goal:s") == before_goal
        assert db.get_meta_committed("loop:s") == before_loop
    finally:
        db.close()


def test_reaper_control_same_setup_without_activity_is_retired(tmp_path):
    """Control for the test above: with a no-op hook the same rows ARE retired, so each live event
    (not the setup) is what keeps them."""
    db = _db(tmp_path / "state.db")
    now = time.time()
    try:
        _put_goal(db, "s", age_days=9, now=now)
        _put_loop(db, "s", age_days=9, now=now)
        _session_row(db, "s", last_active=now - 8 * DAY)
        counts = goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=_HookAfterScanDB(db, lambda: None))
        assert counts == {"scanned": 1, "orphaned": 1, "loops": 1}
    finally:
        db.close()


def test_activity_heartbeat_protects_a_resumed_cli_session(tmp_path):
    """Prism 3f5809a119d6's repro: every scanned clock is 8 days old, then
    ``touch_session_activity(now)`` (no message yet) -> the goal must stay active."""
    db = _db(tmp_path / "state.db")
    now = time.time()
    try:
        _put_goal(db, "s", age_days=8, now=now)
        _session_row(db, "s", last_active=now - 8 * DAY)
        db.touch_session_activity("s", ts=now)
        # RED-proof (M14): in hermes_cli/goals.py::_orphan_gc_session_activity_sql (~line 918)
        # return the pre-fix expression (started_at / effective_last_active / messages, no
        # last_activity_at) -> the live goal is orphaned.
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=7, now=now, db=db)["orphaned"] == 0
        assert _goal(db, "s").status == "active"
    finally:
        db.close()


@pytest.mark.parametrize("prior", ["orphaned", "paused"])
def test_resumed_loop_is_not_reaped_before_its_first_tick(tmp_path, monkeypatch, prior):
    """Prism 4ca29f7a0858's repro: ``LoopManager.resume()`` of an old orphaned or paused loop moves
    neither ``created_at`` nor ``last_fired_at``; the next hourly pass must not cancel the resume."""
    db = _db(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        _put_loop(db, "s", status=prior, age_days=9)
        assert loops.LoopManager("s").resume() is not None
        assert _loop(db, "s").status == "active"
        # RED-proof (M11): drop "next_due_at" from the loop clocks in
        # hermes_cli/goals.py::_ORPHAN_GC_ROW_CLOCKS (~line 914) -> the resumed loop is re-orphaned.
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=db)["loops"] == 0
        assert _loop(db, "s").status == "active"
    finally:
        db.close()


# ── 4. Retire, never delete ───────────────────────────────────────────

def test_orphaned_goal_and_loop_round_trip_through_resume(tmp_path, monkeypatch):
    db = _db(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        _put_goal(db, "s", age_days=9)
        _put_loop(db, "s", age_days=9)
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=db)["orphaned"] == 1

        # RED-proof (M9): in hermes_cli/goals.py::retire_goal (~line 888) replace the
        # compare_and_set_meta write with a DELETE of the row -> resume() has nothing to restore.
        mgr = goals.GoalManager("s")
        assert mgr.resume() is not None
        assert _goal(db, "s").status == "active" and _goal(db, "s").paused_reason is None
        assert "s" in goals.list_active_goal_session_ids()

        lm = loops.LoopManager("s")
        assert lm.resume() is not None
        assert _loop(db, "s").status == "active"
    finally:
        db.close()


# ── 3 + 5. Gateway: multiplex scopes, throttle, watcher wiring, log line ──

_ACTIVE_STORE: contextvars.ContextVar = contextvars.ContextVar("t92_active_store", default=None)


@pytest.fixture
def gw_rig(tmp_path, monkeypatch):
    dbs = {None: _db(tmp_path / "root.db"), "sec": _db(tmp_path / "sec.db")}
    monkeypatch.setattr(goals, "_get_session_db", lambda: dbs[_ACTIVE_STORE.get()])

    @contextlib.asynccontextmanager
    async def scope_cm(profile_home):
        token = _ACTIVE_STORE.set("sec" if profile_home is not None else None)
        try:
            yield
        finally:
            _ACTIVE_STORE.reset(token)

    routed = SimpleNamespace(session_key="agent:sec:discord:channel:9", session_id="sec-routed")
    runner = object.__new__(GatewayRunner)
    runner.config = {"goals": {"orphan_gc_days": 7}}
    runner.session_store = SimpleNamespace(list_sessions=lambda: [routed])
    runner._run_in_executor_with_context = asyncio.to_thread

    async def _warm(_label):
        return None

    runner._warm_goals_session_db = _warm
    yield runner, dbs, scope_cm
    for db in dbs.values():
        db.close()


@pytest.mark.asyncio
async def test_goal_routed_only_under_a_secondary_profile_is_kept(gw_rig, caplog):
    runner, dbs, scope_cm = gw_rig
    _put_goal(dbs[None], "root-orphan", age_days=10)
    _put_goal(dbs["sec"], "sec-routed", age_days=30)
    _put_goal(dbs["sec"], "sec-orphan", age_days=10)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        totals = await runner._goal_orphan_gc([(None, None), ("sec", "/profiles/sec")], scope_cm)

    assert _goal(dbs["sec"], "sec-routed").status == "active"
    # The secondary store IS scanned (else the line above would pass vacuously). RED-proof (M5): in
    # gateway/run_goals.py::_goal_orphan_gc (~line 832) iterate `scopes[:1]` -> "sec-orphan" stays active.
    assert _goal(dbs["sec"], "sec-orphan").status == "orphaned"
    assert _goal(dbs[None], "root-orphan").status == "orphaned"
    assert totals == {"scanned": 3, "orphaned": 2, "loops": 0}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("goal-gc:")]
    assert lines == ["goal-gc: scanned=3 orphaned=2 loops=0"]


@pytest.mark.asyncio
async def test_config_zero_disables_the_gateway_pass(gw_rig):
    runner, dbs, scope_cm = gw_rig
    runner.config = {"goals": {"orphan_gc_days": 0}}
    _put_goal(dbs[None], "root-orphan", age_days=100)
    await runner._goal_orphan_gc([(None, None)], scope_cm)
    # RED-proof (M7): in gateway/run_goals.py::_goal_orphan_gc (~line 836) default a 0 to 7 days.
    assert _goal(dbs[None], "root-orphan").status == "active"


@pytest.mark.asyncio
async def test_each_profile_reads_its_own_orphan_gc_days(gw_rig, monkeypatch):
    """Prism d6291f28e729: under multiplex ``goals.*`` comes from ``load_config()`` inside the profile
    scope; root has GC off, the secondary has it on."""
    import hermes_cli.config as hcfg

    runner, dbs, scope_cm = gw_rig
    runner.config = SimpleNamespace()  # a GatewayConfig carries no goals block
    per_profile = {None: {"goals": {"orphan_gc_days": 0}}, "sec": {"goals": {"orphan_gc_days": 7}}}
    monkeypatch.setattr(hcfg, "load_config", lambda *a, **k: per_profile[_ACTIVE_STORE.get()])
    _put_goal(dbs[None], "root-orphan", age_days=100)
    _put_goal(dbs["sec"], "sec-orphan", age_days=10)

    await runner._goal_orphan_gc([(None, None), ("sec", "/profiles/sec")], scope_cm)
    # RED-proof (M12): in gateway/run_goals.py::_goal_orphan_gc (~line 835) hoist the
    # `gc_days = self._goal_int_setting(...)` read above the scope loop -> root's 0 disables sec too.
    assert _goal(dbs[None], "root-orphan").status == "active"
    assert _goal(dbs["sec"], "sec-orphan").status == "orphaned"


@pytest.mark.asyncio
async def test_gateway_revalidates_routing_against_the_live_store(gw_rig):
    """Prism d6291f28e729: the routing snapshot (``list_sessions``) says unrouted, but by the time the
    row is retired the live store routes it (a /resume landed). The gateway must pass the live
    lookup, so the goal is kept; an id the live store does not route is still reaped."""
    runner, dbs, scope_cm = gw_rig
    resumed = SimpleNamespace(session_key="agent:main:telegram:dm:7", session_id="resumed")
    runner.session_store = SimpleNamespace(
        list_sessions=lambda: [],  # stale snapshot
        lookup_by_session_id=lambda sid: resumed if sid == "resumed" else None,
    )
    _put_goal(dbs[None], "resumed", age_days=20)
    _put_goal(dbs[None], "gone", age_days=20)

    await runner._goal_orphan_gc([(None, None)], scope_cm)
    # RED-proof (M16): in gateway/run_goals.py::_goal_orphan_gc (~line 848) drop
    # `is_routed=_is_routed_now` from the reap call -> the resumed conversation's goal is orphaned.
    assert _goal(dbs[None], "resumed").status == "active"
    assert _goal(dbs[None], "gone").status == "orphaned"


def test_gc_runs_at_most_once_per_hour(monkeypatch):
    import gateway.run_goals as rg

    runner = object.__new__(GatewayRunner)
    clock = [1000.0]
    monkeypatch.setattr(rg.time, "monotonic", lambda: clock[0])
    assert runner._goal_orphan_gc_due() is True
    # RED-proof (M8): delete the `if last is not None and now - last < ...: return False` throttle in
    # gateway/run_goals.py::_goal_orphan_gc_due (~line 813) -> due on every 15 s tick.
    clock[0] += 3599
    assert runner._goal_orphan_gc_due() is False
    clock[0] += 2
    assert runner._goal_orphan_gc_due() is True


@pytest.mark.asyncio
async def test_idle_wake_watcher_runs_the_gc(gw_rig, monkeypatch):
    """The reaper rides the existing watcher. RED-proof (M4): delete the
    `if self._goal_orphan_gc_due(): await self._goal_orphan_gc(scopes, _scope)` lines in
    gateway/run_goals.py::_goal_idle_wake_watcher (~line 800) -> the goal stays active."""
    import gateway.run_goals as rg

    runner, dbs, _scope_cm = gw_rig
    runner.session_store = SimpleNamespace(list_sessions=lambda: [])
    _put_goal(dbs[None], "root-orphan", age_days=10)
    runner._running = True
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:  # the startup delay, then one full tick
            runner._running = False

    monkeypatch.setattr(rg.asyncio, "sleep", _sleep)
    await runner._goal_idle_wake_watcher(interval=15.0)
    assert _goal(dbs[None], "root-orphan").status == "orphaned"
