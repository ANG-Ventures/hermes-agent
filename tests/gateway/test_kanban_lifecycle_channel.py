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


def _set_channel(value, digest=0):
    """Pins ``lifecycle_digest_seconds`` (default 0 = one post per line) so the routing tests
    read immediate posts; ``digest=None`` leaves the key absent (the r143 default applies)."""
    path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    cfg.setdefault("kanban", {})["lifecycle_channel"] = value
    if digest is not None:
        cfg["kanban"]["lifecycle_digest_seconds"] = digest
    path.write_text(yaml.safe_dump(cfg))


def _card(conn, priority=0, mode="notify+wake"):
    tid = kb.create_task(conn, boomerang_wake=False, title="card", assignee="worker", priority=priority)
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
        kb.block_task(conn, b, reason="waiting on parent", kind="needs_input")
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
    kb.block_task(conn, c, reason="three", kind="needs_input")
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
    # r142 C: titles do not fit, so every card id is listed, grouped by kind.
    for i in range(80):
        assert f"t_{i:08x}" in text, i
    assert "t_00000000[subs-ace]" in text   # board shown only off default
    assert "✔ done 27:" in text and "👀 ready 27:" in text and "⏸ blocked 26:" in text
    assert "more (card events" not in text and text.endswith("family=digest:kanban-lifecycle")
    assert "handoff body" not in text and "W3-6" not in text
    small = render(lines[:3])  # a short batch keeps the full header lines
    assert "@human:apollo Kanban t_00000000 done" in small
    mid = render(lines[:12])  # a mid batch keeps titles in compact form
    assert "✔ t_00000000 [subs-ace] W3-6" in mid and "👀 t_00000001 W3-6" in mid


def test_digest_grouped_ids_mark_repeats_and_fit():  # r142 C
    """A card handed back for review three times in one window reads as ONE id with x3,
    and 400 held lines (MAX_HELD) still render to one post with a +N more tally."""
    from gateway.kanban_lifecycle_digest import MAX_MESSAGE_CHARS, render

    title = " — " + "a long card title " * 10
    rep = [f"👀 [default] @human:apollo Kanban t_0000abcd ready for review{title}"] * 3
    rest = [f"✔ [default] @human:apollo Kanban t_{i:08x} done{title}" for i in range(30)]
    text = render(rep + rest)
    assert "👀 ready 3: t_0000abcd×3" in text
    assert text.count("t_0000abcd") == 1
    huge = render([f"✔ [default] @human:apollo Kanban t_{i:08x} done{title}" for i in range(400)])
    assert len(huge) <= MAX_MESSAGE_CHARS and "✔ done 400:" in huge and " more" in huge


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


# --- r143: default-on digest and per-card repeat collapse ---------------------------------


def test_digest_defaults_on_when_key_absent_and_explicit_zero_stays_off(tmp_path, monkeypatch):
    """r143: the knob was never set on the fleet, so #logs got 1,728 single lines in 48 h.
    An ABSENT key now means DEFAULT_DIGEST_SECONDS; an explicit 0 keeps one post per line."""
    from gateway.kanban_lifecycle_digest import DEFAULT_DIGEST_SECONDS, resolve_digest_seconds
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["lifecycle_digest_seconds"] == DEFAULT_DIGEST_SECONDS

    path = get_hermes_home() / "config.yaml"
    path.write_text(yaml.safe_dump({"kanban": {"lifecycle_channel": f"telegram:{LOG}"}}))
    assert resolve_digest_seconds() == DEFAULT_DIGEST_SECONDS == 1800
    path.write_text(yaml.safe_dump({"kanban": {"lifecycle_channel": f"telegram:{LOG}",
                                               "lifecycle_digest_seconds": 0}}))
    assert resolve_digest_seconds() == 0
    path.write_text(yaml.safe_dump({"kanban": {"lifecycle_digest_seconds": "junk"}}))
    assert resolve_digest_seconds() == 0


def test_unset_digest_holds_routed_lines_in_a_real_tick(tmp_path, monkeypatch):
    """No ``lifecycle_digest_seconds`` key: the three routed lines are held, not posted."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "lc.db"))
    kb.init_db()
    _set_channel(f"telegram:{LOG}", digest=None)
    with kb.connect_closing() as conn:
        _three(conn)
    adapter = RecordingAdapter()
    runner = _runner(adapter)
    asyncio.run(_tick(monkeypatch, runner))
    assert adapter.sent == [], adapter.sent
    assert len(runner._kanban_lifecycle_digest) == 3
    assert len(adapter.handled) == 3, "wakes are never delayed by the digest"


def test_card_bouncing_through_review_is_one_line_per_window():
    """r143: t_7c67ef3a re-entered review 10 times in 20 h (each a real review_requested
    after a send-back). Within one window a card's repeats of one kind are ONE line, the
    newest, tagged xN; other cards and other kinds of the same card stay separate."""
    from gateway.kanban_lifecycle_digest import render

    def rr(tid, note):
        return (f"👀 [default] @human:apollo Kanban {tid} ready for review — SDLC L3: green trunk"
                f"\nauto-routed: PR ANG-Ventures/hermes-home#{note} still open")

    lines = [rr("t_7c67ef3a", 10048), rr("t_aaaa0001", 1), rr("t_7c67ef3a", 10048),
             rr("t_7c67ef3a", 10520),
             "✔ [default] @human:apollo Kanban t_7c67ef3a done — SDLC L3: green trunk"]
    text = render(lines)
    assert text.startswith("🗂 **kanban lifecycle** — 5 transition(s) (👀 4 · ✔ 1)"), text
    ready = [ln for ln in text.splitlines() if "t_7c67ef3a ready for review" in ln]
    assert ready == ["👀 [default] @human:apollo Kanban t_7c67ef3a ready for review ×3 — SDLC L3: green trunk"]
    assert text.count("t_aaaa0001") == 1
    assert "Kanban t_7c67ef3a done — SDLC L3" in text
    # Same collapse in the compact tier (titles that do not fit the full tier).
    long_title = " — " + "a long card title " * 12
    many = [f"✔ [default] @w Kanban t_{i:08x} done{long_title}" for i in range(12)]
    many += [f"👀 [default] @w Kanban t_0000beef ready for review{long_title}"] * 4
    compact = render(many)
    assert "👀 t_0000beef×4 a long card title" in compact and compact.count("t_0000beef") == 1
    assert "16 transition(s)" in compact


def test_blocked_reason_containing_done_is_tagged_after_the_verb():
    from gateway.kanban_lifecycle_digest import render

    line = "⏸ [default] @w Kanban t_0000cafe blocked: waits until the deploy is done"
    text = render([line, line, "✔ [default] @w Kanban t_00000001 done — x"])
    assert "Kanban t_0000cafe blocked ×2: waits until the deploy is done" in text, text
