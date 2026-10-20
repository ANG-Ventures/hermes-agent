"""t_b7b31563: a downgraded wake for a HUMAN-owned card is DEFERRED, never dropped.

Incident 2026-10-12 05:38: ``wake for t_ac5f359a on discord/… sent as notify
(host load 184)`` on a review_requested handoff to human:apollo; no turn ran
for 25 min. Drives the real ``_kanban_notifier_watcher`` with a WakeGate fed a
fixed load and a fake dispatcher LoadGate state, then the real
``kanban_wake_defer.tick``.

Cases (card body): downgraded review wake -> row written; gate clears -> fired
once; ceiling -> fired once; owner comment in between -> dropped; restart
between downgrade and fire -> still fires. Plus the gate-input case: load1 184
with lane headroom and no proc_paused -> no downgrade for a human-owner wake.
"""
import asyncio
import json

import pytest

from gateway import kanban_wake_defer as defer
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_wake_gate import WakeGate


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


class FakeDispatcher:
    """The dispatcher LoadGate's published state (``load_gate.json``)."""

    def __init__(self, state="admitting"):
        self.state = state

    def __call__(self):
        return {"state": self.state}


def _runner(adapter, load, tmp_path, *, dispatcher=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_wake_gate = WakeGate(
        {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48},
                    "wake_load_gate": {"host_axis": "load1"}}},
        ncpu=32, loadavg=lambda: (load, load, load), lane_probe=lambda p: None,
        dispatcher_state=dispatcher or FakeDispatcher("admitting"),
        state_file=tmp_path / "wake_gate.json",
    )
    runner._kanban_pending_wakes = defer.PendingWakes(tmp_path / "gateway" / "kanban_pending_wakes.json")
    return runner


def _human_review_task(*, summary="PR ready: https://example.invalid/pr/7\nfull details below"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary=summary, reviewer="human:apollo",
                                 expected_run_id=run_id) is True
        return tid
    finally:
        conn.close()


def _set_load(runner, load):
    runner._kanban_wake_gate._loadavg = lambda: (load, load, load)
    runner._kanban_wake_gate._sampled_at = None  # force a re-sample


def _pending(runner):
    return runner._kanban_pending_wakes.rows


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "defer.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    return tmp_path


# --- (3) gate input -----------------------------------------------------------


def test_load1_alone_does_not_downgrade_a_human_owner_review_wake(board, monkeypatch, caplog):
    """load1 184, lane headroom fine, dispatcher NOT proc_paused -> a real wake."""
    tid = _human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("admitting"))
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1 and "(notify:" not in adapter.sent[0]
    assert len(adapter.handled) == 1, "load1 alone must not silence a human-owner wake"
    assert "PR ready: https://example.invalid/pr/7" in adapter.handled[0].text
    assert _pending(runner) == {}
    assert "sent as notify" not in caplog.text


def test_worker_owner_wake_still_downgrades_on_load1(board, monkeypatch):
    """The t_74bf5296 rule is unchanged for a worker-owned card."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gate task", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.block_task(conn, tid, reason="need a decision")
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board)
    asyncio.run(_tick(monkeypatch, runner))
    assert "(notify: host load 184)" in adapter.sent[0]
    assert adapter.handled == []
    assert _pending(runner) == {}, "a worker-owned card is not deferred (no human to wait on)"


# --- (1) pending wake ---------------------------------------------------------


def _deferred_runner(board, monkeypatch, caplog):
    tid = _human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    return tid, adapter, runner


def test_downgraded_review_wake_writes_a_pending_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    assert len(adapter.sent) == 1 and "(notify: dispatcher proc_paused)" in adapter.sent[0]
    assert adapter.handled == [], "no wake turn while proc_paused"
    rows = list(_pending(runner).values())
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == tid and row["platform"] == "telegram" and row["chat_id"] == "chat-1"
    assert row["kinds"] == ["review_requested"] and row["owner"] == "human:apollo"
    assert row["reason"] == "dispatcher proc_paused" and row["cursor"] > 0
    assert "PR ready: https://example.invalid/pr/7" in row["text"]
    on_disk = json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())
    assert tid in json.dumps(on_disk), "the row is persisted in the gateway state dir"
    assert f"kanban wake deferred for {tid} on telegram/chat-1: dispatcher proc_paused" in caplog.text
    assert f"sent as notify (dispatcher proc_paused); deferred for human:apollo" in caplog.text


def test_gate_clears_fires_the_wake_once(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    # still paused: nothing fires
    assert asyncio.run(defer.tick(runner)) == 0 and adapter.handled == []
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 1
    assert len(adapter.handled) == 1
    text = adapter.handled[0].text
    assert tid in text and "PR ready: https://example.invalid/pr/7" in text
    assert "(deferred 0m under dispatcher proc_paused)" in text
    assert _pending(runner) == {}
    assert f"kanban wake deferred-fire for {tid} on telegram/chat-1 after 0m: gate cleared (was: dispatcher proc_paused)" in caplog.text
    assert asyncio.run(defer.tick(runner)) == 0 and len(adapter.handled) == 1, "fired once"


def test_ceiling_fires_the_wake_once_while_still_contended(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    monkeypatch.setattr(defer, "ceiling_seconds", lambda: 1200)
    first_seen = next(iter(_pending(runner).values()))["first_seen"]
    assert asyncio.run(defer.tick(runner, now=first_seen + 1199)) == 0
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner, now=first_seen + 1200)) == 1
    assert len(adapter.handled) == 1
    assert "(deferred 20m under dispatcher proc_paused)" in adapter.handled[0].text
    assert f"kanban wake deferred-fire for {tid} on telegram/chat-1 after 20m: ceiling 1200s reached (still: dispatcher proc_paused)" in caplog.text
    assert asyncio.run(defer.tick(runner, now=first_seen + 1300)) == 0 and len(adapter.handled) == 1


def test_owner_comment_in_between_drops_the_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="human:apollo", body="on it")
    finally:
        conn.close()
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [], "the owner already acted: no wake"
    assert _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: owner human:apollo commented" in caplog.text


def test_restart_between_downgrade_and_fire_still_fires(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    # A new gateway process: fresh runner, state re-read from disk.
    adapter2 = RecordingAdapter()
    runner2 = _runner(adapter2, 10.0, board, dispatcher=FakeDispatcher("admitting"))
    assert tid in json.dumps(_pending(runner2)), "the row survived the restart"
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner2)) == 1
    assert len(adapter2.handled) == 1 and tid in adapter2.handled[0].text
    assert "(deferred 0m under dispatcher proc_paused)" in adapter2.handled[0].text
    assert _pending(runner2) == {}
    assert not json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())["pending"]


def test_card_done_while_deferred_drops_the_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="landed", force=True)
    finally:
        conn.close()
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [] and _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: card is done" in caplog.text


def test_ceiling_config_knob(monkeypatch):
    from hermes_cli import config as _cfg

    monkeypatch.setattr(_cfg, "load_config_readonly", lambda: {"kanban": {"wake_defer_ceiling_s": 300}})
    assert defer.ceiling_seconds() == 300
    monkeypatch.setattr(_cfg, "load_config_readonly", lambda: {"kanban": {"wake_defer_ceiling_s": "junk"}})
    assert defer.ceiling_seconds() == defer.DEFAULT_CEILING_S


# --- Prism post-merge P1s on #1924 (t_3a8069a3) -------------------------------


def _sub_cursor(tid):
    conn = kb.connect()
    try:
        return conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (tid,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_persist_failure_does_not_advance_the_cursor(board, monkeypatch, caplog):
    """ac41fdbadf47: a deferred wake that cannot be made durable is not acked."""
    tid = _human_review_task()
    before = _sub_cursor(tid)
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    real_mkstemp = defer.tempfile.mkstemp

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(defer.tempfile, "mkstemp", disk_full)
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    assert _sub_cursor(tid) == before, "cursor must not advance past a memory-only wake"
    assert _pending(runner) == {}, "no memory-only row left to fire later"
    assert len(adapter.sent) == 1
    assert "not persisted" in caplog.text and "claim rewound" in caplog.text
    # Disk recovers: the next tick re-claims and records the row durably.
    monkeypatch.setattr(defer.tempfile, "mkstemp", real_mkstemp)
    runner._running = True
    asyncio.run(_tick(monkeypatch, runner))
    assert _sub_cursor(tid) > before
    on_disk = json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())
    assert tid in json.dumps(on_disk["pending"])


def test_tick_retries_a_failed_drop_write(board, monkeypatch, caplog):
    """A failed write in tick() is retried on the next poll, not forgotten."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    real_mkstemp = defer.tempfile.mkstemp
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real_mkstemp(*a, **k)

    monkeypatch.setattr(defer.tempfile, "mkstemp", flaky)
    try:
        asyncio.run(defer.tick(runner))  # the drop's write fails
    except OSError:
        pass  # the notifier loop logs and retries next poll
    assert _pending(runner) == {}
    asyncio.run(defer.tick(runner))  # next poll: nothing left to evaluate
    assert not json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())["pending"]


def _worker_session(tid, sid, profile):
    """A dispatched worker's session: it writes a heartbeat at spawn."""
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at, "
                "actor_profile, actor_session_id) VALUES (?, 1, 'heartbeat', NULL, 0, ?, ?)",
                (tid, profile, sid),
            )
    finally:
        conn.close()


def _state_session(home, sid, source):
    """Record ``sid`` in the profile's own state.db (``home`` = that profile's HERMES_HOME)."""
    from hermes_state import SessionDB

    db = SessionDB(home / "state.db")
    try:
        db.create_session(sid, source=source)
    finally:
        db.close()


def _comment_as(tid, sid, profile, author):
    conn = kb.connect()
    try:
        with kb.mutation_actor(session_ids=[sid], profile=profile):
            kb.add_comment(conn, tid, author=author, body="final note")
    finally:
        conn.close()


def _agent_homed_review_task(agent_sid):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker", session_id=agent_sid)
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="human:apollo",
                                 expected_run_id=run_id) is True
        return tid
    finally:
        conn.close()


def test_agent_home_session_is_not_recorded_as_the_owner(board, monkeypatch, caplog):
    """dfc7f883625d: tasks.session_id is the agent's; its later event is not the human acting."""
    agent_sid = "20261012_090000_agentsess"
    tid = _agent_homed_review_task(agent_sid)
    _worker_session(tid, agent_sid, "worker")
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    row = next(iter(_pending(runner).values()))
    assert row["home_sid"] == "", "an unverified creator session is not the owner's"
    _comment_as(tid, agent_sid, "worker", "worker")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1, "the reviewer has not acted: the wake fires"


def test_worker_under_owner_profile_does_not_cancel_the_wake(board, monkeypatch, caplog):
    """1a09cf36846b: a default-profile worker's comment is not human:apollo acting."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    worker_sid = "20261012_091500_workersess"
    _worker_session(tid, worker_sid, "default")
    _comment_as(tid, worker_sid, "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1
    assert len(adapter.handled) == 1


def test_sessionless_write_under_owner_profile_does_not_cancel_the_wake(board, monkeypatch, caplog):
    """1a09cf36846b: a script with HERMES_PROFILE=default and no session is not the owner."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    conn = kb.connect()
    try:
        with kb.mutation_actor(profile="default"):
            kb.add_comment(conn, tid, author="default", body="auto note")
    finally:
        conn.close()
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1


def test_owner_chat_session_comment_still_drops_the_row(board, monkeypatch, caplog):
    """The owner's own chat session (profile default, no worker run) acting still dedups."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    _state_session(board, "20261012_092000_apollochat", "discord")
    _comment_as(tid, "20261012_092000_apollochat", "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [] and _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: owner human:apollo" in caplog.text


def test_verified_owner_home_session_is_recorded(board, monkeypatch, caplog):
    """A creator session that is the owner's own chat session is kept as home_sid."""
    owner_sid = "20261012_093000_apollohome"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker", session_id=owner_sid)
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
    finally:
        conn.close()
    _state_session(board, owner_sid, "discord")
    _comment_as(tid, owner_sid, "default", "default")  # the owner's session, stamped
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="human:apollo",
                                 expected_run_id=run_id) is True
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    asyncio.run(_tick(monkeypatch, runner))
    row = next(iter(_pending(runner).values()))
    assert row["home_sid"] == owner_sid


def test_cross_board_worker_comment_does_not_cancel_the_wake(board, monkeypatch, caplog):
    """7d9c8fa1f2ee: a default-profile worker heartbeating ANOTHER board comments here."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    worker_sid = "20261012_094000_otherboardworker"
    _state_session(board, worker_sid, "kanban")  # no heartbeat on THIS board
    _comment_as(tid, worker_sid, "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1
    assert len(adapter.handled) == 1


def test_unrecorded_session_comment_does_not_cancel_the_wake(board, monkeypatch, caplog):
    """No provenance in the owner profile's state.db is not proof of a human."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    _comment_as(tid, "20261012_094500_nowhere", "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1


def test_delegated_child_comment_does_not_cancel_the_wake(board, monkeypatch, caplog):
    """cb8149c5fbc5: a delegate child's comment (the permitted child path) is not the owner."""
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    child_sid = "20261012_095000_childsess"
    # Even a child recorded under a chat-looking source is refused by its author marker.
    _state_session(board, child_sid, "discord")
    with monkeypatch.context() as m:
        m.setattr(kb, "_is_delegated_child", lambda: True)
        _comment_as(tid, child_sid, "default", "default")
    conn = kb.connect()
    try:
        author = conn.execute(
            "SELECT author FROM task_comments WHERE task_id = ? ORDER BY id DESC", (tid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert author.endswith(kb.SUBAGENT_AUTHOR_MARKER.strip())
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1


# --- Apollo review of #1934 @303101e9 ------------------------------------------


@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    """Two boards under one sandboxed HERMES_HOME: ``default`` and ``other``."""
    for key in [k for k in list(__import__("os").environ) if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    kb.create_board("other")
    return tmp_path


def test_worker_heartbeating_board_a_commenting_on_board_b_does_not_cancel(two_boards, monkeypatch, caplog):
    """7d9c8fa1f2ee, the two-board shape: a default-profile worker dispatched on
    board ``other`` (heartbeat there, ``kanban`` session source) comments on a
    ``human:apollo`` card on board ``default`` via ``kanban_comment(board=...)``.
    Board ``default`` has no heartbeat for that session; the wake must still fire."""
    board = two_boards
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    worker_sid = "20261012_100000_boardaworker"
    conn = kb.connect(board="other")
    try:
        other_tid = kb.create_task(conn, title="board A card", assignee="default")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at, "
                "actor_profile, actor_session_id) VALUES (?, 1, 'heartbeat', NULL, 0, ?, ?)",
                (other_tid, "default", worker_sid),
            )
    finally:
        conn.close()
    _state_session(board, worker_sid, "kanban")
    conn = kb.connect(board="default")
    try:
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE actor_session_id = ? AND kind = 'heartbeat'",
            (worker_sid,),
        ).fetchone(), "board B sees no heartbeat for the board-A worker"
        with kb.mutation_actor(session_ids=[worker_sid], profile="default"):
            kb.add_comment(conn, tid, author="default", body="cross-board final note")
    finally:
        conn.close()
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1, "a worker on another board is not the reviewer"
    assert len(adapter.handled) == 1


def _bare_human_review_task():
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="human",
                                 expected_run_id=run_id) is True
        return tid
    finally:
        conn.close()


def test_bare_human_owner_own_session_cancels_the_wake(board, monkeypatch, caplog):
    """cb8149c5fbc5 follow-up: ``human`` (no name) is not an operator label.
    The owner's own chat-session comment must drop the row, or a stale wake fires."""
    home_sid = "20261012_101000_humanhome"
    _state_session(board, home_sid, "discord")
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker", session_id=home_sid)
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
    finally:
        conn.close()
    _comment_as(tid, home_sid, "default", "default")  # the home session, stamped
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="human",
                                 expected_run_id=run_id) is True
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    asyncio.run(_tick(monkeypatch, runner))
    row = next(iter(_pending(runner).values()))
    assert row["owner"] == "human" and row["home_sid"] == home_sid, "a bare-human owner's home is verified"
    _comment_as(tid, home_sid, "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [] and _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: owner human wrote commented" in caplog.text


def test_bare_human_owner_explicit_human_comment_cancels_the_wake(board, monkeypatch, caplog):
    """A ``human:<anyone>`` comment author (operator token) is the human acting on a bare-``human`` card."""
    tid = _bare_human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    asyncio.run(_tick(monkeypatch, runner))
    assert next(iter(_pending(runner).values()))["owner"] == "human"
    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="human:apollo", body="on it")
    finally:
        conn.close()
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [] and _pending(runner) == {}


def test_bare_human_owner_worker_comment_still_does_not_cancel(board, monkeypatch, caplog):
    """The bare sentinel must not widen the gate: a default-profile worker is still not the human."""
    tid = _bare_human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    asyncio.run(_tick(monkeypatch, runner))
    worker_sid = "20261012_102000_workersess"
    _worker_session(tid, worker_sid, "default")
    _state_session(board, worker_sid, "kanban")
    _comment_as(tid, worker_sid, "default", "default")
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    assert asyncio.run(defer.tick(runner)) == 1
