"""`unblock_task(expect_block_event=, expect_last_comment=)`: a compare-and-swap unblock.

A caller that decided to unblock from an earlier read (hermes-home's unblock_when sweep, t_12d49eb5) must
not clear a card that was re-blocked or re-pointed between its read and the write.
"""
from pathlib import Path

import argparse
import time

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked(conn, reason="unblock_when: after:+1m"):
    t = kb.create_task(conn, title="parked", assignee="daedalus")
    assert kb.block_task(conn, t, reason=reason)
    return t


def _latest(conn, t):
    ev = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='blocked'", (t,)).fetchone()[0]
    cm = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (t,)).fetchone()[0] or 0
    return ev, cm


def test_matching_expectation_unblocks(kanban_home):
    with kb.connect() as conn:
        t = _blocked(conn)
        ev, cm = _latest(conn, t)
        assert kb.unblock_task(conn, t, expect_block_event=ev, expect_last_comment=cm)
        assert kb.get_task(conn, t).status == "ready"


def test_reblocked_between_read_and_write_is_left_blocked(kanban_home):
    with kb.connect() as conn:
        t = _blocked(conn)
        ev, cm = _latest(conn, t)
        # the race: someone unblocks and a worker re-blocks on a NEW predicate after the sweep read
        assert kb.unblock_task(conn, t)
        assert kb.block_task(conn, t, reason="unblock_when: host_up:ace-ai", kind="capability")
        assert not kb.unblock_task(conn, t, expect_block_event=ev, expect_last_comment=None,
                                   comment=("sweep", "UNBLOCK: stale", None, None))
        assert kb.get_task(conn, t).status == "blocked"
        bodies = [c.body for c in kb.list_comments(conn, t)]
        assert not any("stale" in b for b in bodies)   # a refused CAS writes no comment


def test_new_comment_between_read_and_write_is_left_blocked(kanban_home):
    with kb.connect() as conn:
        t = _blocked(conn)
        ev, cm = _latest(conn, t)
        kb.add_comment(conn, t, "apollo", "unblock_when: ruling:changed-my-mind")
        assert not kb.unblock_task(conn, t, expect_block_event=ev, expect_last_comment=cm)
        assert kb.get_task(conn, t).status == "blocked"


def test_expectation_refuses_a_non_blocked_card(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="scheduled", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (t,))
        assert not kb.unblock_task(conn, t, expect_block_event=0)
        assert kb.unblock_task(conn, t)   # without an expectation the old behaviour holds


def _deferred(conn, reason="unblock_when: pr_merged:o/r#1"):
    t = kb.create_task(conn, title="deferred", assignee="daedalus")
    assert kb.block_task(conn, t, reason=reason, kind="deferred", until=int(time.time()) + 86400)
    assert kb.get_task(conn, t).status == "scheduled"
    ev = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='scheduled'", (t,)).fetchone()[0]
    cm = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (t,)).fetchone()[0] or 0
    return t, ev, cm


def test_deferred_card_cas_on_its_scheduled_event(kanban_home):   # t_7f670c57
    with kb.connect() as conn:
        t, ev, cm = _deferred(conn)
        assert not kb.unblock_task(conn, t, expect_block_event=ev + 1, expect_last_comment=cm)
        assert kb.get_task(conn, t).status == "scheduled"
        assert kb.unblock_task(conn, t, expect_block_event=ev, expect_last_comment=cm)
        row = conn.execute("SELECT status, next_eligible_at FROM tasks WHERE id=?", (t,)).fetchone()
        assert row[0] == "ready" and row[1] is None   # the deferral's wake stamp is lifted too


def test_deferred_card_reparked_or_repointed_is_left_alone(kanban_home):   # t_7f670c57
    with kb.connect() as conn:
        t, ev, cm = _deferred(conn)
        kb.add_comment(conn, t, "apollo", "unblock_when: ruling:changed")
        assert not kb.unblock_task(conn, t, expect_block_event=ev, expect_last_comment=cm)
        t2, ev2, cm2 = _deferred(conn)
        assert kb.unblock_task(conn, t2)
        assert kb.block_task(conn, t2, reason="unblock_when: after:+1h", kind="deferred",
                             until=int(time.time()) + 3600)
        assert not kb.unblock_task(conn, t2, expect_block_event=ev2, expect_last_comment=None)
        assert kb.get_task(conn, t2).status == "scheduled"


def test_cli_flags_reach_the_cas(kanban_home, capsys):
    with kb.connect() as conn:
        t = _blocked(conn)
        ev, cm = _latest(conn, t)
    args = ["unblock", "--reason", "cleared", "--expect-block-event", str(ev + 1),
            "--expect-last-comment", str(cm), t]
    rc = kcli.kanban_command(_parse(args))
    assert rc == 1
    assert "blocking state changed" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "blocked"
    rc = kcli.kanban_command(_parse(["unblock", "--expect-block-event", str(ev), "--expect-last-comment", str(cm), t]))
    assert rc == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def _parse(argv):
    return kcli.build_parser(argparse.ArgumentParser().add_subparsers(dest="_top")).parse_args(argv)
