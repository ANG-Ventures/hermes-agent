import asyncio


from gateway.config import Platform
from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _release_singleton_lock,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_stalled_event_pages_subscription_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "stall-notify.db"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="idle worker", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "stalled", {"progress_age_seconds": 900})
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert "stalled" in adapter.sent[0]["text"]
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_unsubscribes_after_delivering_done(tmp_path, monkeypatch):
    """done ends notification ownership AFTER delivery (t_6d6e9467).

    The completion line and the wake both still arrive; then the row is gone,
    so no later event on the card can wake the origin session again.
    """
    db_path = tmp_path / "done-unsub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.handled[0].source.thread_id == "origin-thread"

    conn = kbc.connect()
    try:
        assert kbn.list_notify_subs(conn, tid) == []
        # Later noise on the card reaches nobody.
        kb._append_event(conn, tid, "crashed")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_notifier_keeps_subscription_when_done_delivery_fails(
    tmp_path, monkeypatch,
):
    """A failed send rewinds and keeps the sub: unsubscribe is post-delivery."""
    db_path = tmp_path / "done-fail.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.attempts == 1
    conn = kbc.connect()
    try:
        assert len(kbn.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()
    assert len(_unseen_terminal_events(tid)) == 1

def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def _unseen_terminal_events_for(tid, chat_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kbc.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kbn.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kbn, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kbn.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kbn, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kbc.connect()
    try:
        _, remaining = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []


# ---------------------------------------------------------------------------
# #111125 — a repeated-block circuit breaker establishes that orchestration
# attention is needed, NOT that a human decision exists. The formatter must
# use neutral wording unless the block was typed as a genuine owner-input
# request (`needs_input`, the only kind that carries a concrete question).
# ---------------------------------------------------------------------------


class _StubEvent:
    def __init__(self, payload):
        self.payload = payload


class _StubNotif:
    head = "H123"


def _fmt_block_loop(payload):
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    msg, _, _ = _EVENT_FORMATTERS["block_loop_detected"](_StubEvent(payload), _StubNotif())
    return msg


def test_block_loop_technical_kind_uses_neutral_orchestration_wording():
    """A repeated technical block (transient/capability/untyped) routed to
    triage is an orchestration handoff with no question for the owner, so the
    ping must not claim a human decision (#111125)."""
    payload = {"reason": "waiting on upstream", "kind": "transient", "recurrences": 2}
    msg = _fmt_block_loop(payload)
    assert "for orchestration attention" in msg
    assert "human decision" not in msg
    # Circuit-breaker visibility is preserved.
    assert "TRIAGE" in msg
    assert "waiting on upstream" in msg


def test_block_loop_owner_input_keeps_decision_wording():
    """A `needs_input` block carries a concrete question for the owner, so the
    owner-decision wording is correct and must be retained (#111125)."""
    payload = {
        "reason": "Which API key should this use?",
        "kind": "needs_input",
        "recurrences": 2,
        "limit": kb.BLOCK_RECURRENCE_LIMIT,
    }
    msg = _fmt_block_loop(payload)
    assert "needs a human decision" in msg
    assert "for orchestration attention" not in msg
    assert "Which API key should this use?" in msg


# ---------------------------------------------------------------------------
# Handoffs that hand a decision back to the origin must wake it, not only ping
# it: `review_requested` (implementation done, waiting for a reviewer) and
# `block_loop_detected` (routed to triage) are terminal kinds just like
# `blocked`.
# ---------------------------------------------------------------------------


def _wake_text(adapter):
    """Text of the single synthetic wake turn injected into the adapter."""
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


def _review_handoff_task(
    *,
    delivery_mode="notify+wake",
    summary="PR ready: https://example.invalid/pr/7\nfull details below",
):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="implement the thing",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary=summary, expected_run_id=run_id,
        ) is True
        return tid
    finally:
        conn.close()


def test_review_requested_wakes_the_origin_session(tmp_path, monkeypatch):
    """A review handoff wakes the origin and carries the worker's summary."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-wake.db"))
    kb.init_db()
    tid = _review_handoff_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "the passive review ping is unchanged"

    wake = _wake_text(adapter)
    assert tid in wake
    assert "PR ready: https://example.invalid/pr/7" in wake, (
        "the worker's handoff must ride the wake turn like it does for "
        "`completed`, otherwise the woken reviewer has to re-read the board"
    )


def test_block_loop_detected_wakes_the_origin_session(tmp_path, monkeypatch):
    """A triage escalation wakes the origin so a decision gets made."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage-wake.db"))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="loops forever",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in _wake_text(adapter)


def test_review_requested_does_not_wake_a_notify_only_subscription(
    tmp_path, monkeypatch,
):
    """delivery_mode still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-notify.db"))
    kb.init_db()
    _review_handoff_task(delivery_mode="notify")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "notify-only subscriptions must not be woken by a review handoff"
    )


class _GoneTargetAdapter:
    """Reports the target chat as deleted (``error_kind='not_found'``)."""

    def __init__(self, error_kind):
        self.attempts = 0
        self.error_kind = error_kind

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(
            success=False,
            error="404 Not Found (error code: 10003): Unknown Channel",
            error_kind=self.error_kind,
        )


def test_notifier_drops_subscription_at_once_when_target_is_gone(tmp_path, monkeypatch):
    """A deleted chat never comes back: one not_found drops the sub; any other
    failure still gets the MAX_SEND_FAILURES grace (t_ff4197d3)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "gone.db"))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = _make_runner(_GoneTargetAdapter(error_kind=None))
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    conn = kbc.connect()
    try:
        assert len(kbn.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()

    adapter = _GoneTargetAdapter(error_kind="not_found")
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.attempts == 1
    conn = kbc.connect()
    try:
        assert kbn.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# t_a4890a77: a transition made by the subscriber chat's OWN session posts the
# passive line but must not wake that chat ("echo of my own close").
# ---------------------------------------------------------------------------


def _self_caused_completion(actor_session_id, delivery_mode="notify+wake"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="closed from chat",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode=delivery_mode, user_id="u1",
        )
        kb.complete_task(conn, tid, summary="merged and closed")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET actor_session_id=? "
                "WHERE task_id=? AND kind='completed'",
                (actor_session_id, tid),
            )
        return tid
    finally:
        conn.close()


def _origins(monkeypatch, mapping):
    import gateway.kanban_watchers as kw

    monkeypatch.setattr(kw, "_session_origin", lambda sid: mapping.get(sid))


def test_self_caused_completion_posts_line_but_does_not_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "self-echo.db"))
    kb.init_db()
    _origins(monkeypatch, {"sess-chat-1": {"platform": "telegram", "chat_id": "chat-1", "thread_id": "", "user_id": "u1", "profile": "default"}})
    tid = _self_caused_completion("sess-chat-1")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1 and tid in adapter.sent[0]["text"]
    assert adapter.handled == [], "own-session close must not wake the same chat"
    assert _unseen_terminal_events(tid) == [], "cursor still advances"


def test_completion_from_another_chat_still_wakes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "other-chat.db"))
    kb.init_db()
    _origins(monkeypatch, {"sess-other": {"platform": "telegram", "chat_id": "chat-2", "thread_id": "", "user_id": "u1", "profile": "default"}})
    tid = _self_caused_completion("sess-other")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in _wake_text(adapter)


def test_self_caused_event_ids_matching_rules():
    from gateway.kanban_watchers import self_caused_event_ids

    def ev(i, actor):
        return kb.Event(id=i, task_id="t", kind="completed", payload=None,
                        created_at=0, actor_session_id=actor)

    sub = {"platform": "Discord", "chat_id": "c1", "thread_id": "",
           "user_id": "u1", "notifier_profile": None}
    base = {"platform": "discord", "chat_id": "c1", "thread_id": "",
            "user_id": "u1", "profile": "default"}
    origins = {
        "same": dict(base),
        "alt": dict(base, user_id="", user_id_alt="u1"),
        "thread": dict(base, thread_id="th9"),
        "other": dict(base, chat_id="c2"),
        # Same group chat, different participant: per-user group sessions
        # mean the wake targets u1's session, not u2's (c62d95fdcbb8).
        "other_user": dict(base, user_id="u2"),
        # Same chat + user, different bot profile (daed91426bf1).
        "other_profile": dict(base, profile="apollo2"),
        "no_profile": {k: v for k, v in base.items() if k != "profile"},
    }
    events = [ev(1, "same"), ev(2, "thread"), ev(3, "other"), ev(4, None),
              ev(5, "unknown"), ev(6, "c1"), ev(7, "other_user"),
              ev(8, "other_profile"), ev(9, "no_profile"), ev(10, "alt")]
    got = self_caused_event_ids(sub, events, origin_of=origins.get)
    # 6: an actor id that merely equals chat_id is NOT proof of the same
    # chat (FleetReview e014c9b89f4e); only a recorded origin match counts.
    assert got == {1, 10}
    # A sub with no recorded participant can't prove self: fail open.
    anon = dict(sub, user_id=None)
    assert self_caused_event_ids(anon, events, origin_of=origins.get) == set()


def test_self_caused_event_whose_line_was_not_sent_still_wakes(tmp_path, monkeypatch):
    """The wake is dropped only for events whose passive line was confirmed
    sent (6c26d69da6e0). A self-caused failure event the lane planner keeps
    silent (directive None: no send) must still wake the subscriber."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "unsent.db"))
    kb.init_db()
    _origins(monkeypatch, {"sess-chat-1": {"platform": "telegram", "chat_id": "chat-1", "thread_id": "", "user_id": "u1", "profile": "default"}})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="self crash", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode="notify+wake", user_id="u1",
        )
        kb._append_event(conn, tid, kind="crashed")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET actor_session_id=? "
                "WHERE task_id=? AND kind='crashed'",
                ("sess-chat-1", tid),
            )
    finally:
        conn.close()

    class SilentPlanner:
        def plan(self, deliveries, now=None):
            for d in deliveries:
                d["failure_directives"] = {ev.id: None for ev in d["events"]}

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._kanban_lane_dedupe = SilentPlanner()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == [], "planner kept the line silent"
    assert tid in _wake_text(adapter), "unsent self-caused event must still wake"



def test_self_caused_event_on_wake_only_sub_still_wakes(tmp_path, monkeypatch):
    """delivery_mode='wake' posts no passive line: the wake is the only
    delivery, so a self-caused event must still wake (FleetReview d9c37a52d911)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-only.db"))
    kb.init_db()
    _origins(monkeypatch, {"sess-chat-1": {"platform": "telegram", "chat_id": "chat-1", "thread_id": "", "user_id": "u1", "profile": "default"}})
    tid = _self_caused_completion("sess-chat-1", delivery_mode="wake")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert tid in _wake_text(adapter)
    assert _unseen_terminal_events(tid) == []


def test_self_caused_event_on_apiserver_sub_still_wakes(tmp_path, monkeypatch):
    """Non-push (api_server) subs: the self-post wake IS the delivery, so an
    event whose actor is the subscriber session still wakes it
    (FleetReview d9c37a52d911, delegated child completion case)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver-self.db"))
    kb.init_db()
    _origins(monkeypatch, {"origin-session": {"platform": "api_server", "chat_id": "origin-session", "thread_id": "", "user_id": "u1", "profile": "default"}})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="child work", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="api_server", chat_id="origin-session",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="child done")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET actor_session_id=? "
                "WHERE task_id=? AND kind='completed'",
                ("origin-session", tid),
            )
    finally:
        conn.close()

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    class ApiServerLike:
        supports_async_delivery = False

        async def send(self, chat_id, text, metadata=None):
            from gateway.platforms.base import SendResult

            return SendResult(success=False, error="no send()")

        async def handle_message(self, event):
            raise AssertionError("api_server wake must not use handle_message")

    runner = _make_runner(ApiServerLike())
    runner.adapters = {Platform.API_SERVER: runner.adapters[Platform.TELEGRAM]}
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(posts) == 1 and tid in posts[0]["text"]
    assert posts[0]["session_id"] == "origin-session"
