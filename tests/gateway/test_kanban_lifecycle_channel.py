"""kanban.lifecycle_channel re-routes done / ready-for-review / blocked lines
(t_f7bba206). Driven through config.yaml in the sandboxed HERMES_HOME and one
real notifier tick."""
import asyncio

import yaml

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

LOG = "log-chan"


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _set_channel(value):
    path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    cfg.setdefault("kanban", {})["lifecycle_channel"] = value
    path.write_text(yaml.safe_dump(cfg))


def _card(conn, priority=0, mode="notify+wake"):
    tid = kb.create_task(conn, title="card", assignee="worker", priority=priority)
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin",
                      chat_type="group", user_id="u1", delivery_mode=mode)
    return tid


def _run(tmp_path, monkeypatch, channel, make):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "lc.db"))
    kb.init_db()
    if channel is not None:
        _set_channel(channel)
    with kb.connect_closing() as conn:
        tid = make(conn)
    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    return tid, adapter


def _complete(conn):
    tid = _card(conn)
    kb.complete_task(conn, tid, summary="shipped")
    return tid


def test_done_line_goes_to_lifecycle_channel_and_wake_stays(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", _complete)
    assert [(m["chat_id"], tid in m["text"] and " done" in m["text"]) for m in adapter.sent] == [(LOG, True)]
    assert len(adapter.handled) == 1, "the subscriber's agent is still woken"
    assert adapter.handled[0].source.chat_id == "origin"


def test_unset_keeps_subscriber_chat(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, None, _complete)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_malformed_value_keeps_subscriber_chat(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, "no-colon", _complete)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_review_and_plain_block_are_routed(tmp_path, monkeypatch):
    def make(conn):
        a = _card(conn)
        kb.request_review(conn, a, summary="PR ready", force=True)
        b = _card(conn)
        kb.block_task(conn, b, reason="waiting on parent", kind="dependency")
        return a, b

    (a, b), adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    by_chat = {m["chat_id"] for m in adapter.sent}
    assert by_chat == {LOG}, adapter.sent
    texts = " ".join(m["text"] for m in adapter.sent)
    assert "ready for review" in texts and f"{b} blocked" in texts


def test_needs_input_block_on_p100_card_stays_in_origin(tmp_path, monkeypatch):
    def make(conn):
        hi = _card(conn, priority=100)
        kb.block_task(conn, hi, reason="need a ruling", kind="needs_input")
        lo = _card(conn, priority=99)
        kb.block_task(conn, lo, reason="need a ruling", kind="needs_input")
        return hi, lo

    (hi, lo), adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    where = {m["text"].split("Kanban ")[1].split()[0]: m["chat_id"] for m in adapter.sent}
    assert where == {hi: "origin", lo: LOG}


def test_failure_lines_stay_in_origin(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "stalled", {"progress_age_seconds": 900})
        return tid

    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_lifecycle_channel_target_rules():
    from gateway.kanban_watchers import lifecycle_channel_target, parse_lifecycle_channel

    ch = parse_lifecycle_channel(" Discord:123 ")
    assert ch == ("discord", "123")
    assert parse_lifecycle_channel("") is None and parse_lifecycle_channel("discord:") is None

    class T:
        priority = 150

    assert lifecycle_channel_target(ch, "completed", {}, T()) == ch
    assert lifecycle_channel_target(ch, "blocked", {"kind": "needs_input"}, T()) is None
    assert lifecycle_channel_target(ch, "blocked", {"kind": "capability"}, T()) == ch
    assert lifecycle_channel_target(ch, "crashed", {}, T()) is None
    assert lifecycle_channel_target(None, "completed", {}, T()) is None


# --- kanban.lifecycle_digest_seconds (t_d62bd921) ------------------------------------------


def _set_digest(value):
    path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    cfg.setdefault("kanban", {})["lifecycle_digest_seconds"] = value
    path.write_text(yaml.safe_dump(cfg))


def _three(conn):
    a = _card(conn)
    kb.complete_task(conn, a, summary="one")
    b = _card(conn)
    kb.request_review(conn, b, summary="two", force=True)
    c = _card(conn)
    kb.block_task(conn, c, reason="three", kind="dependency")
    return a, b, c


def _run_digest(tmp_path, monkeypatch, window, make, clock):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "lc.db"))
    kb.init_db()
    _set_channel(f"telegram:{LOG}")
    _set_digest(window)
    with kb.connect_closing() as conn:
        ids = make(conn)
    adapter = RecordingAdapter()
    runner = _runner(adapter)
    import gateway.kanban_watchers as kw

    monkeypatch.setattr(kw.time, "time", lambda: clock[0])
    asyncio.run(_tick(monkeypatch, runner))
    return ids, adapter, runner


def test_digest_batches_routed_lines_into_one_post(tmp_path, monkeypatch):  # mutant: digest-bypass
    clock = [1000.0]
    (a, b, c), adapter, runner = _run_digest(tmp_path, monkeypatch, 900, _three, clock)
    # Held, not posted; each subscriber still woken for its own card.
    assert adapter.sent == [], adapter.sent
    assert len(adapter.handled) == 3, "wakes are never delayed by the digest"
    assert len(runner._kanban_lifecycle_digest) == 3
    # Window elapses: one message carrying all three transitions.
    clock[0] += 900
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    assert [m["chat_id"] for m in adapter.sent] == [LOG]
    text = adapter.sent[0]["text"]
    assert "3 transition(s)" in text and "family=digest:kanban-lifecycle" in text
    assert f"{a} done" in text and "ready for review" in text and f"{c} blocked" in text


def test_digest_not_due_before_window(tmp_path, monkeypatch):
    clock = [1000.0]
    _ids, adapter, runner = _run_digest(tmp_path, monkeypatch, 900, _three, clock)
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0] + 899))
    assert adapter.sent == []


def test_digest_zero_keeps_one_post_per_line(tmp_path, monkeypatch):
    clock = [1000.0]
    _ids, adapter, runner = _run_digest(tmp_path, monkeypatch, 0, _three, clock)
    assert [m["chat_id"] for m in adapter.sent] == [LOG, LOG, LOG]
    assert len(runner._kanban_lifecycle_digest) == 0


def test_digest_never_holds_origin_lines(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "stalled", {"progress_age_seconds": 900})
        hi = _card(conn, priority=100)
        kb.block_task(conn, hi, reason="need a ruling", kind="needs_input")
        return tid, hi

    clock = [1000.0]
    _ids, adapter, runner = _run_digest(tmp_path, monkeypatch, 900, make, clock)
    assert [m["chat_id"] for m in adapter.sent] == ["origin", "origin"]
    assert len(runner._kanban_lifecycle_digest) == 0


def test_digest_failed_send_keeps_batch_for_next_tick():
    from gateway.kanban_lifecycle_digest import LifecycleDigest

    class Flaky:
        def __init__(self):
            self.fail, self.sent = True, []

        async def send(self, chat_id, text, metadata=None):
            if self.fail:
                raise RuntimeError("503")
            self.sent.append(text)

    ad = Flaky()
    dg = LifecycleDigest()
    dg.add(("discord", "L"), ad, "✔ [default] @w Kanban t_1 done — x", 60, 0.0)
    dg.add(("discord", "L"), ad, "👀 [default] @w Kanban t_2 ready for review — y", 60, 1.0)
    assert asyncio.run(dg.flush(61.0)) == 0 and len(dg) == 2
    ad.fail = False
    assert asyncio.run(dg.flush(62.0)) == 1 and len(dg) == 0
    assert "2 transition(s)" in ad.sent[0]


def test_digest_single_line_posts_unchanged_and_bad_config_is_off():
    from gateway.kanban_lifecycle_digest import parse_digest_seconds, render

    line = "✔ [default] @w Kanban t_1 done — x\nhandoff"
    assert render([line]) == line
    assert [parse_digest_seconds(v) for v in (None, "", "abc", -5, 0, "900", 900)] == [0, 0, 0, 0, 0, 900, 900]


def test_digest_of_a_4h_batch_is_one_message():  # t_26d7df3d
    """80 transitions (a busy 4 h window, census 2026-10-02) render to ONE <= 2000-char message
    that still names the newest cards, never an adapter-split stream of ~9 posts."""
    from gateway.kanban_lifecycle_digest import MAX_MESSAGE_CHARS, render

    lines = [
        f"{'✔👀⏸'[i % 3]} [{'default' if i % 4 else 'subs-ace'}] @human:apollo Kanban t_{i:08x} "
        f"{('done', 'ready for review', 'blocked')[i % 3]} — " + "W3-6: a long card title " * 12
        + "\nhandoff body that never reaches the digest"
        for i in range(80)
    ]
    text = render(lines)
    assert len(text) <= MAX_MESSAGE_CHARS < 2000
    assert text.startswith("🗂 **kanban lifecycle** — 80 transition(s)")
    assert "✔ t_00000000 [subs-ace] W3-6" in text   # board shown only off default
    assert "👀 t_00000001 W3-6" in text and "⏸ t_00000002 W3-6" in text
    assert "more (card events" in text and text.endswith("family=digest:kanban-lifecycle")
    assert "handoff body" not in text
    small = render(lines[:3])  # a short batch keeps the full header lines
    assert "@human:apollo Kanban t_00000000 done" in small


def _second_sub(conn, tid, chat):
    """A legacy/--also second subscriber row, written raw so the test does not
    depend on the admission rule it sits next to."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id,"
            " user_id, chat_type, delivery_mode, created_at, last_event_id)"
            " VALUES (?, 'telegram', ?, '', 'u2', 'group', 'notify', 0,"
            " (SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?))",
            (tid, chat, tid),
        )


def test_multi_subscriber_card_posts_one_receipt(tmp_path, monkeypatch):
    """t_484a3c72: a card with two subscriber chats posted its done line to the
    lifecycle channel twice. Receipts land in exactly one place."""
    def make(conn):
        tid = _card(conn, mode="notify")
        _second_sub(conn, tid, "other-chat")
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    done = [m for m in adapter.sent if tid in m["text"] and " done" in m["text"]]
    assert [m["chat_id"] for m in done] == [LOG], adapter.sent


def test_subscriber_that_is_the_lifecycle_channel_gets_one_line(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, mode="notify")
        _second_sub(conn, tid, LOG)
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    assert [m["chat_id"] for m in adapter.sent if tid in m["text"]] == [LOG], adapter.sent


def test_multi_subscriber_card_digests_one_line(tmp_path, monkeypatch):
    """t_484a3c72 x t_d62bd921: with the digest on, a two-subscriber card adds
    its done line to the batch once."""
    def make(conn):
        tid = _card(conn, mode="notify")
        _second_sub(conn, tid, "other-chat")
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    clock = [1000.0]
    tid, adapter, runner = _run_digest(tmp_path, monkeypatch, 900, make, clock)
    assert adapter.sent == []
    assert len(runner._kanban_lifecycle_digest) == 1
