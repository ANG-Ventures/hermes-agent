"""Closed cards keep a permanent pointer to their artifact (t_328f78fc).

Retention (hermes-home R8) moves a long-done card's comments/runs/events off the board; the
tasks row stays. ``tasks.artifact_pointer`` is stamped on close from those rows, so the link
survives the move. ``show`` prints it; the backfill only fills NULL pointers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_artifact_pointer as kap
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

DOC = "https://vast-ether-n35e.here.now/"
OBS = "/Users/alexgierczyk/Obsidian/Vault/Reports/kanban-retention.md"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _prune(conn, tid: str) -> None:
    """What retention leaves of a long-done card: the tasks row only (R8 move-out, no archive file)."""
    for tbl in ("task_comments", "task_runs", "task_events"):
        conn.execute("DELETE FROM %s WHERE task_id=?" % tbl, (tid,))
    conn.commit()


def _pointer(conn, tid: str):
    return kap.read(conn.execute("SELECT artifact_pointer FROM tasks WHERE id=?", (tid,)).fetchone()[0])


def test_link_in_comment_survives_prune(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="measure retention", assignee="daedalus")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "daedalus", f"report: {DOC} and notes at {OBS}.")
        kb.complete_task(conn, tid, summary="measured; see the report",
                         metadata={"pr_url": "https://github.com/o/r/pull/7"})
        _prune(conn, tid)
        assert conn.execute("SELECT count(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == 0
        assert _pointer(conn, tid) == ["https://github.com/o/r/pull/7", DOC, OBS]
    finally:
        conn.close()
    text = kc.run_slash(f"show {tid}")
    assert "Artifacts (3):" in text and DOC in text and OBS in text
    data = json.loads(kc.run_slash(f"show {tid} --json"))
    assert data["task"]["artifact_pointer"] == ["https://github.com/o/r/pull/7", DOC, OBS]


def test_card_with_nothing_keeps_empty_pointer(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="no output", assignee="daedalus")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="nothing to point at")
        _prune(conn, tid)
        assert conn.execute("SELECT artifact_pointer FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "[]"
    finally:
        conn.close()
    assert "Artifacts (0):" in kc.run_slash(f"show {tid}")


def test_archive_keeps_pointer_stamped_before_prune(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="done then archived", assignee="daedalus")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary=f"report {DOC}")
        _prune(conn, tid)
        assert kb.archive_task(conn, tid)
        assert _pointer(conn, tid) == [DOC]
    finally:
        conn.close()


def test_cap_and_workspace_paths_skipped(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="many links", assignee="daedalus")
        ws = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        kb.claim_task(conn, tid)
        links = " ".join(f"https://example.com/{i}" for i in range(15))
        kb.complete_task(conn, tid, summary=f"{links} scratch {ws}/out.txt" if ws else links)
        got = _pointer(conn, tid)
        assert got == [f"https://example.com/{i}" for i in range(kap.MAX_ENTRIES)]
        assert len(json.dumps(got)) <= kap.MAX_BYTES
    finally:
        conn.close()


def test_backfill_dry_run_then_apply_is_idempotent(kanban_home):
    conn = kbc.connect()
    try:
        with_link = kb.create_task(conn, title="old card with link", assignee="daedalus")
        kb.claim_task(conn, with_link)
        kb.complete_task(conn, with_link, summary=f"done, report {DOC}")
        bare = kb.create_task(conn, title="old card, nothing", assignee="daedalus")
        kb.claim_task(conn, bare)
        kb.complete_task(conn, bare, summary="done")
        open_card = kb.create_task(conn, title="still open", assignee="daedalus")
        # cards closed before the column existed
        conn.execute("UPDATE tasks SET artifact_pointer = NULL")
        conn.commit()

        dry = kap.backfill(conn)
        assert dry == {"scanned": 2, "gain": 1, "empty": 1, "applied": False}
        assert _pointer(conn, with_link) is None   # dry run writes nothing

        assert kap.backfill(conn, apply=True) == {"scanned": 2, "gain": 1, "empty": 1, "applied": True}
        assert _pointer(conn, with_link) == [DOC]
        assert _pointer(conn, bare) == []
        assert _pointer(conn, open_card) is None

        _prune(conn, with_link)   # rows gone later: a rerun must not touch the stamped pointer
        assert kap.backfill(conn, apply=True) == {"scanned": 0, "gain": 0, "empty": 0, "applied": True}
        assert _pointer(conn, with_link) == [DOC]
    finally:
        conn.close()


def test_scan_shapes():
    text = ("see https://x.io/a). path ~/notes/a.md, obsidian://open?vault=V&file=R "
            "and /Volumes/fleet-scratch/r.html; not relative/path or https://x.io/a again")
    assert kap._scan(text) == ["https://x.io/a", "~/notes/a.md", "obsidian://open?vault=V&file=R",
                               "/Volumes/fleet-scratch/r.html", "https://x.io/a"]
