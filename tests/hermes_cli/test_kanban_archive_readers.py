"""Worker context and the PR -> card lookup read rows R8 moved to ``kanban-archive.db`` (t_589fb988).

R8 (hermes-home ``kanban-events-retention.py``, t_d1b5fc83) moves a long-done card's comments, runs
and events to ``kanban-archive.db`` beside the board and keeps the tasks row. These readers must still
see that history: live rows first, archived rows deduped by id (a live row always wins), and an
unreadable archive raises instead of rendering less history. Part of t_d6c66796 (R8 arming).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_archive_read as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pr_owner as po

PR_URL = "https://github.com/ANG-Ventures/hermes-agent/pull/4242"
TARGET = ("ang-ventures/hermes-agent", 4242)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def _move_out(conn, tid: str) -> Path:
    """R8's contract for one card: copy every row (all columns + moved_at) to the archive, then delete it."""
    main = next(p for _s, n, p in conn.execute("PRAGMA database_list") if n == "main")
    arch = Path(main).parent / kar.ARCHIVE_NAME
    conn.execute("ATTACH DATABASE ? AS march", (str(arch),))
    for tbl in ("task_comments", "task_runs", "task_events"):
        cols = [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % tbl)]
        conn.execute("CREATE TABLE IF NOT EXISTS march.%s (id INTEGER PRIMARY KEY, %s, moved_at INTEGER NOT NULL)"
                     % (tbl, ", ".join(c for c in cols if c != "id")))
        cl = ", ".join(cols)
        conn.execute("INSERT INTO march.%s (%s, moved_at) SELECT %s, 1 FROM main.%s WHERE task_id=?"
                     % (tbl, cl, cl, tbl), (tid,))
        conn.execute("DELETE FROM main.%s WHERE task_id=?" % tbl, (tid,))
    conn.commit()
    conn.execute("DETACH DATABASE march")
    for tbl in ("task_comments", "task_runs"):
        assert conn.execute("SELECT count(*) FROM %s WHERE task_id=?" % tbl, (tid,)).fetchone()[0] == 0
    return arch


def _done_card(conn, title="old done card") -> str:
    tid = kb.create_task(conn, title=title, assignee="daedalus")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "apollo", "thread note kept across the move")
    kb.complete_task(conn, tid, summary="handoff: shipped the parser",
                     metadata={"pr_url": PR_URL})
    return tid


def test_parent_handoff_survives_move(conn):
    parent = _done_card(conn)
    _move_out(conn, parent)
    child = kb.create_task(conn, title="new child", assignee="daedalus", parents=[parent])
    ctx = kb.build_worker_context(conn, child)
    assert f"### {parent}" in ctx
    assert "handoff: shipped the parser" in ctx
    assert PR_URL in ctx   # the run's metadata line


def test_prior_attempts_and_comments_survive_move(conn):
    tid = _done_card(conn)
    _move_out(conn, tid)
    ctx = kb.build_worker_context(conn, tid)
    assert "## Prior attempts on this task" in ctx
    assert "handoff: shipped the parser" in ctx
    assert "## Comment thread" in ctx
    assert "thread note kept across the move" in ctx


def test_pr_cards_finds_moved_card(conn):
    tid = _done_card(conn)   # PR named only in run metadata, not the title
    _move_out(conn, tid)
    assert [c[0] for c in po.pr_cards(conn, TARGET)] == [tid]
    assert po.pr_owner_card(conn, TARGET) == tid
    assert TARGET in po.card_pr_targets(conn, tid, archived=True)
    other = kb.create_task(conn, title="follow-up", assignee="daedalus")
    kb.claim_task(conn, other)
    kb.complete_task(conn, other, summary="done", metadata={"pr_url": PR_URL})
    assert po.pr_card_map(conn, other) == {
        "ang-ventures/hermes-agent#4242": [{"id": tid, "status": "done", "assignee": "daedalus"}]}


def test_live_row_beats_stale_archived_copy(conn):
    tid = _done_card(conn)
    arch = _move_out(conn, tid)
    # Put the rows back live (the window where R8 committed the archive but has not deleted), then edit live.
    a = sqlite3.connect(arch)
    for tbl in ("task_comments", "task_runs"):
        cols = [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % tbl)]
        for row in a.execute("SELECT %s FROM %s WHERE task_id=?" % (", ".join(cols), tbl), (tid,)).fetchall():
            conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (tbl, ", ".join(cols), ",".join("?" * len(cols))), row)
    a.close()
    conn.execute("UPDATE task_runs SET summary='LIVE summary' WHERE task_id=?", (tid,))
    conn.execute("UPDATE task_comments SET body='LIVE comment' WHERE task_id=?", (tid,))
    conn.commit()
    assert [r.summary for r in kar.runs_with_archive(conn, tid)] == ["LIVE summary"]
    assert [c.body for c in kar.comments_with_archive(conn, tid)] == ["LIVE comment"]
    ctx = kb.build_worker_context(conn, tid)
    assert "LIVE summary" in ctx and "LIVE comment" in ctx
    assert "handoff: shipped the parser" not in ctx and "thread note kept" not in ctx


def test_unreadable_archive_raises(conn, monkeypatch):
    tid = _done_card(conn)
    arch = _move_out(conn, tid)
    monkeypatch.setattr(kar, "_TIMEOUT_S", 0.2)
    holder = sqlite3.connect(arch, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(kar.ArchiveUnreadable):
            kb.build_worker_context(conn, tid)
        with pytest.raises(kar.ArchiveUnreadable):
            po.pr_cards(conn, TARGET)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_non_file_at_archive_path_is_unreadable_not_absent(conn):
    # Only a MISSING archive is absent; a directory there must reach sqlite and fail loudly (t_78dbef10).
    tid = _done_card(conn)
    main = next(p for _s, n, p in conn.execute("PRAGMA database_list") if n == "main")
    (Path(main).parent / kar.ARCHIVE_NAME).mkdir()
    with pytest.raises(kar.ArchiveUnreadable):
        kar.full_rows(conn, "task_runs", tid)
    with pytest.raises(kar.ArchiveUnreadable):
        po.pr_cards(conn, TARGET)
    assert kar.archived_rows(conn, tid).get("error")


def test_pr_cards_survives_a_move_between_archive_and_live_reads(conn, monkeypatch):
    # R8 lands its move right after the archive read: the live read must already have the card (t_78dbef10).
    tid = _done_card(conn)
    real = kar.task_ids_with_run_metadata_like

    def archive_then_move(c, patterns):
        out = real(c, patterns)
        _move_out(c, tid)
        return out

    monkeypatch.setattr(kar, "task_ids_with_run_metadata_like", archive_then_move)
    assert [c[0] for c in po.pr_cards(conn, TARGET)] == [tid]
