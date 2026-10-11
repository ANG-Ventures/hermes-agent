"""Needs-input pager (t_c8ca40b4): a card blocked on a human ruling pages its
origin channel once per (card, reason), re-pages every 2 h while it waits, and
stays silent below priority 100 without an origin channel."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gateway import kanban_watchers as kw
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

CHAN = "1553876718639390760"
ORIGIN = f"origin: discord Daemonarchy / #cc-native ({CHAN}) · session 20260928_010147_13a37c43 · 2026-10-01"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    return home


def _card(conn, *, priority=150, body=ORIGIN, reason="which bracket: B3 or B4?"):
    tid = kb.create_task(conn, title="phase 4", body=body, priority=priority, assignee=None)
    if reason is not None:
        assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
    return tid


def _tick(pager, now, sent, include_dependency=False):
    cards, boards = kw._needs_input_cards([("default", object())], include_dependency)

    def send(board, item, still_waiting):
        sent.append((board, item["task_id"], item["channel"], still_waiting, item["reason"]))
        return True

    return pager.observe(cards, send, boards, now=now)


def _run(*argv):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def test_transition_pages_origin_channel_once(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    sent = []
    pager = kw._NeedsInputPager()
    assert _tick(pager, 1000.0, sent) == 1
    assert sent == [("default", tid, CHAN, False, "which bracket: B3 or B4?")]


def test_same_reason_twice_pages_once(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    sent = []
    pager = kw._NeedsInputPager()
    _tick(pager, 1000.0, sent)
    _tick(pager, 1060.0, sent)
    # unblock + re-block for the SAME reason inside the window: still one post
    # (the same-kind re-block escalates to triage and is still a candidate)
    with kb.connect_closing() as conn:
        assert kb.unblock_task(conn, tid)
        _tick(pager, 1120.0, sent)
        assert kb.block_task(conn, tid, reason="which bracket: B3 or B4?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]
    _tick(pager, 1200.0, sent)
    assert len(sent) == 1


def test_repages_still_waiting_after_two_hours(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    sent = []
    pager = kw._NeedsInputPager()
    _tick(pager, 1000.0, sent)
    _tick(pager, 1000.0 + 7199, sent)
    assert len(sent) == 1
    _tick(pager, 1000.0 + 7200, sent)
    assert [s[3] for s in sent] == [False, True]
    assert sent[1][1] == tid


def test_new_reason_is_a_new_page(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    sent = []
    pager = kw._NeedsInputPager()
    _tick(pager, 1000.0, sent)
    with kb.connect_closing() as conn:
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="different question", kind="needs_input")
    _tick(pager, 1010.0, sent)
    assert [s[4] for s in sent] == ["which bracket: B3 or B4?", "different question"]
    assert [s[3] for s in sent] == [False, False]


def test_ledger_survives_restart(kanban_home, tmp_path):
    with kb.connect_closing() as conn:
        _card(conn)
    state = tmp_path / "pages.json"
    sent = []
    _tick(kw._NeedsInputPager(state), 1000.0, sent)
    _tick(kw._NeedsInputPager(state), 1100.0, sent)
    assert len(sent) == 1


def test_low_priority_without_origin_is_silent(kanban_home):
    with kb.connect_closing() as conn:
        silent = _card(conn, priority=50, body="no origin here")
        homed = _card(conn, priority=50)
        hot = _card(conn, priority=100, body="no origin here")
        _card(conn, priority=500, reason=None)  # not blocked
        tid = kb.create_task(conn, title="cap", body=ORIGIN, priority=150, assignee=None)
        assert kb.block_task(conn, tid, reason="no creds", kind="capability")
        got = {c["task_id"]: c for c in kb.needs_input_page_candidates(conn)}
    assert silent not in got and tid not in got
    assert got[homed]["channel"] == CHAN
    assert got[hot]["channel"] is None
    assert set(got) == {homed, hot}


def test_origin_channel_must_be_numeric_discord():
    assert kb.origin_discord_channel(ORIGIN) == CHAN
    assert kb.origin_discord_channel("origin: discord Srv / #x (cc-native) · s") is None
    assert kb.origin_discord_channel(f"origin: telegram chat ({CHAN}) · s") is None
    assert kb.origin_discord_channel(f"title\n{ORIGIN}") is None  # must be first line
    # a chat name with its own parentheses (format_origin_line copies it verbatim)
    assert kb.origin_discord_channel(
        f"origin: discord Team (EU) / #ops ({CHAN}) · session s · 2026-10-01") == CHAN
    assert kb.origin_discord_channel(
        f"origin: discord Srv (123456789012345678) / #ops · session s · 2026-10-01") is None


def test_origin_line_from_format_origin_line(monkeypatch):
    real_get = kb._ambient_session_env
    env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_NAME": "Team (EU) / #ops", "HERMES_SESSION_CHAT_ID": CHAN}
    monkeypatch.setattr(kb, "_ambient_session_env", lambda k: env.get(k, real_get(k)))
    line = kb.format_origin_line("20260928_010147_13a37c43")
    assert kb.origin_discord_channel(line) == CHAN


def test_edit_no_page_opts_out_and_page_restores(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    assert _run("edit", tid, "--no-page") == 0
    sent = []
    pager = kw._NeedsInputPager()
    _tick(pager, 1000.0, sent)
    assert sent == []
    assert _run("edit", tid, "--page") == 0
    _tick(pager, 1010.0, sent)
    assert len(sent) == 1
    with kb.connect_closing() as conn:
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("needs_input_page_off") == 1 and kinds.count("needs_input_page_on") == 1


def test_dependency_on_blocked_parent_is_config_keyed(kanban_home):
    with kb.connect_closing() as conn:
        parent = _card(conn, body="no origin", priority=10, reason="parent question")
        child = kb.create_task(conn, title="child", body=ORIGIN, priority=150, assignee=None)
        # A worker parks the card as `dependency` while the blocking parent is
        # open. Upstream 42a778ab4b re-kinds a `dependency` block with NO open
        # parent to sticky `needs_input`, so the parent link exists first; the
        # fixture writes the waiting state directly because linking demotes a
        # ready card to `todo`, which block_task (running/ready/review) refuses.
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status='todo', block_kind='dependency' WHERE id=?", (child,))
        assert kb.get_task(conn, child).block_kind == "dependency"
        assert kb.needs_input_page_candidates(conn) == []
        got = kb.needs_input_page_candidates(conn, include_dependency=True)
    assert [c["task_id"] for c in got] == [child]
    assert parent in got[0]["reason"] and "parent question" in got[0]["reason"]


def test_settings_default_on_and_fail_open():
    assert kw._resolve_needs_input_pager_settings(lambda: {}) == (True, False)
    assert kw._resolve_needs_input_pager_settings(
        lambda: {"kanban": {"needs_input_pager": False}}) == (False, False)
    assert kw._resolve_needs_input_pager_settings(
        lambda: {"kanban": {"needs_input_pager_dependency": True}}) == (True, True)

    def boom():
        raise OSError("unreadable")

    assert kw._resolve_needs_input_pager_settings(boom) == (True, False)


def test_send_routes_origin_target_and_alerts_mirror(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    monkeypatch.setattr(kw, "_notify_send",
                        lambda script, msg, extra: calls.append((msg, extra)) or True)
    item = {"task_id": "t_1", "title": "p4", "priority": 150, "reason": "x" * 400,
            "channel": CHAN, "alerts": False}
    assert kw._send_needs_input_page("default", item)
    assert [c[1] for c in calls] == [["--target", CHAN]]
    msg = calls[0][0]
    assert msg.startswith("⚠️ `t_1` needs a ruling: " + "x" * 300 + " — ")
    assert "x" * 301 not in msg and "`hermes kanban show t_1`" in msg

    calls.clear()
    assert kw._send_needs_input_page("ops", dict(item, alerts=True), True)
    assert [c[1] for c in calls] == [["--target", CHAN], ["--sev", "error"]]
    assert calls[0][0].startswith("⚠️ still waiting: ")
    assert "hermes kanban --board ops show t_1" in calls[0][0]

    calls.clear()
    assert kw._send_needs_input_page("default", dict(item, channel=None))
    assert [c[1] for c in calls] == [["--sev", "error"]]

    calls.clear()
    assert kw._send_needs_input_page("default", dict(item, channel="#cc-native"))
    assert [c[1] for c in calls] == [["--sev", "error"]]  # non-numeric never a target


def test_failed_send_retries_next_tick(kanban_home):
    with kb.connect_closing() as conn:
        _card(conn)
    cards, boards = kw._needs_input_cards([("default", object())])
    pager = kw._NeedsInputPager()
    assert pager.observe(cards, lambda b, i, s: False, boards, now=1000.0) == 0
    assert pager.observe(cards, lambda b, i, s: True, boards, now=1005.0) == 1


def test_failing_sends_cannot_starve_later_cards(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        bad = _card(conn, reason="bad channel")
        good = _card(conn, reason="good channel")
    cards, boards = kw._needs_input_cards([("default", object())])
    assert [c["task_id"] for _, c in cards] == sorted([bad, good])
    first = cards[0][1]["task_id"]
    # budget admits ONE send per tick; the first card always fails
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(kw.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(kw, "_GUARD_STUCK_PAGE_BUDGET_S", 30.0)
    tried = []

    def send(board, item, still):
        tried.append(item["task_id"])
        return item["task_id"] != first

    pager = kw._NeedsInputPager()
    pager.observe(cards, send, boards, now=1000.0)
    pager.observe(cards, send, boards, now=1001.0)
    assert set(tried) == {bad, good}


def _age_block(conn, tid, seconds=60):
    """Move the card's block events into the past so a comment lands after them."""
    conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? "
                 "AND kind IN ('blocked', 'block_loop_detected')", (seconds, tid))
    conn.commit()


@pytest.mark.parametrize("body,answered", [
    ("APOLLO 14:45 — B4 RULED: keep the tool-bearing fixture.", True),
    ("APOLLO RULING 18:10 PT: build now, flip gated.", True),
    ("APOLLO 06:00 (Ace 05:40): RULINGS: (1) land it.", True),
    ("APOLLO 09:11 — B3 RULING: (B)+(C).", True),
    ("APOLLO 09:00 ANSWERED: option B.", True),
    ("APOLLO 17:25 — B4 CLOSED, DEPLOYED, GO. ruling applied", False),  # lowercase: no marker
    ("APOLLO 14:40 PT: B4 NEEDS RULING: waiting for Ace.", False),        # Prism 88f87cb46ec3
    ("APOLLO 14:40: B4 NEEDS AN OPERATOR RULING", False),                 # Prism cb9e35a4fe01
    ("APOLLO 09:00: B4 NOT ANSWERED yet.", False),
    ("APOLLO 10:00: NO RULING yet, Ace is away.", False),
    ("APOLLO 10:00: in front of Ace, AWAITING RULING.", False),
    ("APOLLO 14:40 PT: B4 is in front of Ace now as a 1-3-1.", False),
    ("FYI: APOLLO RULED on the sibling", False),                          # not a leading APOLLO
])
def test_ruling_comment_after_block_stops_the_page(kanban_home, body, answered):
    """t_dfc938c4: t_e6b3713d paged "needs a ruling" after Apollo posted "B4 RULED"."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _age_block(conn, tid)
        kb.add_comment(conn, tid, "default", body)
        got = [c["task_id"] for c in kb.needs_input_page_candidates(conn)]
    assert got == ([] if answered else [tid])


@pytest.mark.parametrize("author", ["daedalus", "daedalus-opus", "default (subagent)", "apollo (subagent)"])
def test_ruling_text_from_a_non_operator_does_not_silence(kanban_home, author):
    """Prism 162590a51fb8: only RULING_AUTHORS can answer; a worker or a delegated child cannot."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _age_block(conn, tid)
        kb.add_comment(conn, tid, author, "APOLLO 09:00 ANSWERED: option B.")
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]


def test_delegated_child_comment_is_marked_and_ignored(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _age_block(conn, tid)
    monkeypatch.setattr(kb, "_is_delegated_child", lambda: True)
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "default", "APOLLO 09:00 RULED: option B.")
    monkeypatch.setattr(kb, "_is_delegated_child", lambda: False)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT author FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] \
            .endswith(kb.SUBAGENT_AUTHOR_MARKER.strip())
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]


def test_ruling_before_the_latest_block_does_not_silence_it(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, reason="B3?")
        _age_block(conn, tid, 120)
        kb.add_comment(conn, tid, "default", "APOLLO 09:11 — B3 RULING: (B)+(C).")
        conn.execute("UPDATE task_comments SET created_at = created_at - 60 WHERE task_id = ?", (tid,))
        conn.commit()
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="B4?", kind="needs_input")
        got = kb.needs_input_page_candidates(conn)
    assert [c["task_id"] for c in got] == [tid] and got[0]["reason"] == "B4?"


def test_non_ruling_comments_keep_paging(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _age_block(conn, tid)
        kb.add_comment(conn, tid, "default", "FYI: ruled out the cache; APOLLO RULED nothing yet")
        kb.add_comment(conn, tid, "default", "APOLLO 14:40 PT: B4 is in front of Ace now as a 1-3-1.")
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]


def test_same_second_ruling_counts_and_same_second_reblock_pages(kanban_home):
    """Prism 6da195bd4c18: created_at is whole seconds; order by event id instead."""
    with kb.connect_closing() as conn:
        tid = _card(conn, reason="B4?")
        kb.add_comment(conn, tid, "default", "APOLLO 14:45 — B4 RULED: keep the fixture.")
        conn.execute("UPDATE task_events SET created_at = 1000 WHERE task_id = ?", (tid,))
        conn.execute("UPDATE task_comments SET created_at = 1000 WHERE task_id = ?", (tid,))
        conn.commit()
        assert kb.needs_input_page_candidates(conn) == []
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="B5?", kind="needs_input")
        conn.execute("UPDATE task_events SET created_at = 1000 WHERE task_id = ?", (tid,))
        conn.commit()
        got = kb.needs_input_page_candidates(conn)
    assert [c["task_id"] for c in got] == [tid] and got[0]["reason"] == "B5?"


def test_worker_run_on_the_card_cannot_answer_with_an_operator_label(kanban_home):
    """Prism 4b2f58cb33c4: a dispatched worker's comment on its own card carries its run_id."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _age_block(conn, tid)
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'daedalus', 'running', 1)",
            (tid,),
        ).lastrowid
        conn.commit()
        kb.add_comment(conn, tid, "default", "APOLLO 09:00 ANSWERED: option B.", run_id=run_id)
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]


def _legacy_rekinded_block(conn, tid):
    """A block event as the pre-t_1e0609f4 kernel wrote it for a parentless
    ``dependency`` ask (re-kinded to needs_input). Live boards still hold
    these rows; the pager must keep treating them as time waits."""
    assert kb.block_task(conn, tid, reason="DEFERRED 24h wall clock", kind="needs_input")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET payload = json_set(payload, '$.requested_kind', 'dependency', "
            "'$.rekind_reason', 'no_open_parent') WHERE id = (SELECT MAX(id) FROM task_events "
            "WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected'))", (tid,))


def test_legacy_rekinded_dependency_does_not_page(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park", body=ORIGIN, priority=150, assignee="w")
        _legacy_rekinded_block(conn, tid)
        assert kb.needs_input_page_candidates(conn) == []
        # control: an explicit needs_input block still pages
        other = _card(conn)
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [other]


def test_legacy_rekinded_dependency_pages_once_escalated_to_triage(kanban_home):
    """Prism aefe736f033c on #1835: a recurrence into ``triage`` needs a ruling."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park", body=ORIGIN, priority=150, assignee="w")
        _legacy_rekinded_block(conn, tid)
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="DEFERRED 24h wall clock", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert [c["task_id"] for c in kb.needs_input_page_candidates(conn)] == [tid]


def test_deferred_block_wakes_to_ready_with_zero_pages(kanban_home):
    """Card acceptance (t_1e0609f4): `block --kind deferred --until +1m` on a
    priority-150 card with an origin channel parks, wakes to ready at the
    wake time, and the needs-input pager sends NOTHING across park and wake
    (dependency paging on, the widest setting)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park", body=ORIGIN, priority=150, assignee="w")
        assert kb.claim_task(conn, tid, claimer="w") is not None
    assert _run("block", tid, "DEFERRED", "24h", "wall", "clock", "--kind", "deferred", "--until", "+1m") == 0
    sent = []
    pager = kw._NeedsInputPager()
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_kind) == ("scheduled", "deferred")
        wake = int(task.next_eligible_at)
    for now in (1000.0, 1000.0 + 2 * 3600, 1000.0 + 5 * 3600):
        _tick(pager, now, sent, include_dependency=True)
    with kb.connect_closing() as conn:
        assert kb.wake_due_scheduled(conn, now=wake - 1) == []
        assert kb.wake_due_scheduled(conn, now=wake) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
    _tick(pager, 1000.0 + 7 * 3600, sent, include_dependency=True)
    assert sent == []


def test_bare_dependency_block_is_refused_and_never_pages(kanban_home, capsys):
    """Card acceptance (t_1e0609f4): `block --kind dependency` with no parent
    and no --until is refused with the message naming both options; nothing
    lands, so nothing pages."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park", body=ORIGIN, priority=150, assignee="w")
        assert kb.claim_task(conn, tid, claimer="w") is not None
    assert _run("block", tid, "waiting", "--kind", "dependency") == 1
    err = capsys.readouterr().err
    assert "--kind deferred --until" in err and "--kind needs_input" in err
    sent = []
    _tick(kw._NeedsInputPager(), 1000.0, sent, include_dependency=True)
    assert sent == []
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
