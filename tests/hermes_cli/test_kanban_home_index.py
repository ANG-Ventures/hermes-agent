"""Cross-board home index (t_11f2cf60): maintained on the kanban write path,
read by kanban-home-cards on a session's first turn instead of scanning every
board."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_home_index as hi

SID = "20260924_120000_aaaaaa"
OTHER = "20260924_120000_ffffff"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    hi._FLUSHED.clear()
    hi._SLUG_CACHE.clear()
    return h


def _conn(home: Path, slug: str = "default"):
    path = home / "kanban.db" if slug == "default" else home / "kanban" / "boards" / slug / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return kb.connect(path)


def _ids(session_ids=(SID,)):
    return {(c["board"], c["id"], c["status"]) for c in hi.open_cards(list(session_ids))}


def test_not_installed_write_path_is_inert_and_read_is_unavailable(home):
    with _conn(home) as conn:
        kb.create_task(conn, title="x", session_id=SID)
    assert not hi.index_path().exists()  # writers never create it
    with pytest.raises(hi.IndexUnavailable, match="no-index"):
        hi.open_cards([SID])


def test_backfill_then_write_path_tracks_create_and_status(home):
    conn = _conn(home)
    old = kb.create_task(conn, title="before install", session_id=SID)
    rep = hi.resync()
    assert rep["drift"] == 1 and rep["missing"] == 1  # first run reports what it adds
    assert _ids() == {("default", old, "ready")}
    new = kb.create_task(conn, title="after install", session_id=SID)
    assert ("default", new, "ready") in _ids()
    assert kb.block_task(conn, new, reason="r")
    assert ("default", new, "blocked") in _ids()
    assert kb.complete_task(conn, old, summary="done")
    assert {t for _, t, _ in _ids()} == {new}  # closed cards drop out of the read
    conn.close()
    assert hi.resync(check_only=True)["drift"] == 0


def test_cards_on_every_board_one_read(home):
    hi.resync()
    made = set()
    for slug in ("default", "b-one", "b-two"):
        with _conn(home, slug) as conn:
            made.add((slug, kb.create_task(conn, title=slug, session_id=SID), "ready"))
            kb.create_task(conn, title="foreign", session_id=OTHER)
            kb.create_task(conn, title="unstamped")
    assert _ids() == made
    assert hi.resync(check_only=True)["drift"] == 0


def test_restamp_moves_card_between_homes(home):
    hi.resync()
    with _conn(home) as conn:
        tid = kb.create_task(conn, title="x", session_id=SID)
        kb.set_task_session(conn, tid, OTHER)
    assert _ids() == set()
    assert {t for _, t, _ in _ids((OTHER,))} == {tid}


def test_pinned_non_board_db_is_not_indexed(home, tmp_path):
    hi.resync()
    conn = kb.connect(tmp_path / "scratch.db")
    kb.create_task(conn, title="x", session_id=SID)
    conn.close()
    assert _ids() == set()


def test_eventless_writer_is_caught_by_drift_check_not_trusted(home):
    """A writer that bypasses the event journal is invisible to the write
    path; the daily --check must report it (drift >= 1), and a sync fixes it."""
    hi.resync()
    with _conn(home) as conn:
        tid = kb.create_task(conn, title="x", session_id=SID)
    raw = sqlite3.connect(home / "kanban.db")
    raw.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    raw.commit(); raw.close()
    assert _ids() == {("default", tid, "ready")}  # stale until resync
    rep = hi.resync(check_only=True)
    assert rep["drift"] == 1 and rep["stale"] == 1
    assert _ids() == {("default", tid, "ready")}  # --check writes nothing
    assert hi.resync()["drift"] == 1
    assert _ids() == {("default", tid, "blocked")}


def test_raw_event_row_is_picked_up_by_next_commit(home):
    """Event-driven, not call-site driven: a raw writer that records its
    event is mirrored by the NEXT write_txn commit on that board."""
    hi.resync()
    conn = _conn(home)
    tid = kb.create_task(conn, title="x", session_id=SID)
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'raw', 1)", (tid,))
    conn.commit()
    other = kb.create_task(conn, title="y", session_id=OTHER)  # unrelated commit
    assert ("default", tid, "blocked") in _ids()
    assert {t for _, t, _ in _ids((OTHER,))} == {other}
    conn.close()


def test_older_snapshot_never_overwrites_newer(home):
    hi.resync()
    with _conn(home) as conn:
        tid = kb.create_task(conn, title="x", session_id=SID)
        kb.block_task(conn, tid, reason="r")
    idx = hi._open_rw(hi.index_path())
    ev = idx.execute("SELECT ev FROM cards WHERE task_id = ?", (tid,)).fetchone()[0]
    hi._apply_rows(idx, "default", [(tid, SID, "ready", "x", None, 1, ev - 1)])
    assert idx.execute("SELECT status FROM cards WHERE task_id = ?", (tid,)).fetchone()[0] == "blocked"
    idx.close()


def test_index_failure_never_fails_the_board_write(home, monkeypatch):
    hi.resync()
    monkeypatch.setattr(hi, "_open_rw", lambda p: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with _conn(home) as conn:
        tid = kb.create_task(conn, title="x", session_id=SID)
        assert kb.get_task(conn, tid) is not None


def test_vanished_board_is_extra_drift(home):
    with _conn(home, "gone") as conn:
        kb.create_task(conn, title="x", session_id=SID)
    hi.resync()
    import shutil

    shutil.rmtree(home / "kanban" / "boards" / "gone")
    rep = hi.resync()
    assert rep["extra"] == 1
    assert _ids() == set()


def test_sync_hook_lives_inside_write_txn_commit_path():
    """The mirror must run at the ONE commit boundary every guarded mutator
    and execution-lane writer shares (Apollo ruling 5), not beside it."""
    import ast
    import inspect

    src = inspect.getsource(kb.write_txn)
    tree = ast.parse(src)
    calls = [c.func.id for c in ast.walk(tree)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "_sync_home_index" in calls
    assert src.index("_sync_home_index") > src.index('"COMMIT"')


def test_cli_home_index_check_exit_codes(home, capsys):
    from hermes_cli import kanban as kc
    import argparse

    with _conn(home) as conn:
        kb.create_task(conn, title="x", session_id=SID)
    def run(argv):
        sub = argparse.ArgumentParser().add_subparsers(dest="_top")
        return kc.kanban_command(kc.build_parser(sub).parse_args(argv))

    assert run(["home-index", "--check"]) == 2  # missing index is an error
    assert run(["home-index"]) == 0
    assert run(["home-index", "--check"]) == 0
    assert "drift=0" in capsys.readouterr().out


# ── CLASS-SWEEP: writers that leave no surviving event (Argus r2 B2) ────────
# Each converges at its own write_txn commit (identity diff), or at the next
# write_txn commit on that board when it used a raw transaction.

def test_delete_task_leaves_no_ghost(home):
    hi.resync()
    with _conn(home) as conn:
        tid = kb.create_task(conn, title="x", session_id=SID)
        assert (("default", tid, "ready")) in _ids()
        assert kb.delete_task(conn, tid)
    assert _ids() == set()
    assert hi.resync(check_only=True)["drift"] == 0


def test_delete_archived_task_leaves_no_ghost(home):
    hi.resync()
    with _conn(home) as conn:
        keep = kb.create_task(conn, title="keep", session_id=SID)
        tid = kb.create_task(conn, title="x", session_id=SID, )
        assert kb.archive_task(conn, tid)
        assert kb.delete_archived_task(conn, tid)
    assert _ids() == {("default", keep, "ready")}
    assert hi.resync(check_only=True)["drift"] == 0


def test_raw_unstamp_converges_at_next_commit(home):
    """kanban_transfer's scrub sets session_id = NULL with no event."""
    hi.resync()
    conn = _conn(home)
    tid = kb.create_task(conn, title="x", session_id=SID)
    conn.execute("UPDATE tasks SET session_id = NULL WHERE id = ?", (tid,))
    conn.commit()
    kb.create_task(conn, title="unrelated", session_id=OTHER)
    conn.close()
    assert _ids() == set()
    assert hi.resync(check_only=True)["drift"] == 0


def test_raw_status_and_title_rewrite_inside_write_txn_converges(home):
    """kanban_swarm / quota_repair / dashboard raw UPDATEs inside write_txn."""
    hi.resync()
    conn = _conn(home)
    tid = kb.create_task(conn, title="old", session_id=SID)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked', title = 'new' WHERE id = ?", (tid,))
    got = hi.open_cards([SID])
    conn.close()
    assert [(c["id"], c["status"], c["title"]) for c in got] == [(tid, "blocked", "new")]
    assert hi.resync(check_only=True)["drift"] == 0


def test_raw_restamp_moves_home_at_next_commit(home):
    hi.resync()
    conn = _conn(home)
    tid = kb.create_task(conn, title="x", session_id=SID)
    conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (OTHER, tid))
    conn.commit()
    with kb.write_txn(conn):
        pass
    conn.close()
    assert _ids() == set()
    assert {t for _, t, _ in _ids((OTHER,))} == {tid}
