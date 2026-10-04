"""``external`` as a first-class terminal outcome (t_768c9e91).

A card whose remaining step belongs to an outside party (an upstream
maintainer merge) had no honest close: blocking it re-dispatched a worker that
could only block again (block loop -> triage), and completing it demanded a
receipt/survivor for work the fleet cannot do. ``complete --external URL
--watcher ID`` closes it done with outcome ``external``; the watcher owns the
wait and reopens the card if the upstream closes unmerged.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

URL = "https://github.com/stephenschoettler/hermes-lcm/pull/617"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked_task(conn, title="waiting on upstream #617") -> str:
    tid = kb.create_task(conn, title=title, assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    assert kb.block_task(conn, tid, reason="EXTERNAL: upstream #617 still open")
    return tid


def test_external_closes_done_with_url_and_watcher_and_no_receipt(kanban_home):
    with kb.connect_closing() as conn:
        tid = _blocked_task(conn)
        assert kb.complete_task(conn, tid, external=URL, watcher="4209d31f3516") is True
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "done"
        run = kb.list_runs(conn, tid)[-1]
        assert run.outcome == "external"
        assert (run.metadata or {}).get("external") == {"url": URL, "watcher": "4209d31f3516"}
        assert URL in (run.summary or "")
        done = [e for e in kb.list_events(conn, tid) if e.kind == "completed"]
        assert (done[-1].payload or {}).get("external") == {"url": URL, "watcher": "4209d31f3516"}


def test_external_closes_a_triage_card(kanban_home):
    """The block-loop parks an external wait in triage; the verb must end it there."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="external wait", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()
        assert kb.complete_task(conn, tid, summary="x") is False   # triage stays gated for plain closes
        assert kb.complete_task(conn, tid, external=URL, watcher="w") is True
        assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("external,watcher,why", [
    ("", "w", "no --external"), ("   ", "w", "no --external"),
    ("NousResearch/hermes-agent#1", "w", "not http(s)"),
    (URL, "", "no --watcher"), (URL, None, "no --watcher"), (None, "w", "no --external"),
])
def test_external_without_evidence_is_refused_unmutated(kanban_home, external, watcher, why):
    with kb.connect_closing() as conn:
        tid = _blocked_task(conn)
        with pytest.raises(kb.ExternalCloseError, match=why.replace("(", r"\(").replace(")", r"\)")):
            kb.complete_task(conn, tid, external=external, watcher=watcher)
        assert kb.get_task(conn, tid).status == "blocked"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "completion_blocked_external" in kinds and "completed" not in kinds


def test_external_run_counts_as_success_downstream(kanban_home):
    with kb.connect_closing() as conn:
        parent = _blocked_task(conn)
        assert kb.complete_task(conn, parent, external=URL, watcher="w") is True
        child = kb.create_task(conn, title="child", assignee="coder", parents=[parent])
        assert kb.get_task(conn, child).status in ("ready", "todo")
        assert parent in kb.build_worker_context(conn, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        conn.commit()
        assert kbd.check_respawn_guard(conn, parent) is not None
        assert kb._RUN_OUTCOME_TERMINAL_STATUS["external"] == "done"


def test_external_close_is_reopenable(kanban_home):
    """The watcher's re-open on upstream close-unmerged is the existing reopen verb."""
    with kb.connect_closing() as conn:
        tid = _blocked_task(conn)
        assert kb.complete_task(conn, tid, external=URL, watcher="w") is True
        ok, err = kb.reopen_task(conn, tid, actor="external-card-watch",
                                 reason=f"{URL} CLOSED unmerged")
        assert ok, err
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.list_runs(conn, tid)[-1].outcome == "voided"


def _complete_args(**kw):
    ns = dict(task_ids=[], result=None, summary=None, metadata=None, force=False,
              superseded_by=None, draft_ok=None, external=None, watcher=None,
              survivor_ref=None, survivor_pr=None, survivor_unbound=None,
              survivor_none=False, reason=None)
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_cli_complete_external(kanban_home, capsys, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kb.connect_closing() as conn:
        tid = _blocked_task(conn)
    assert kcli._cmd_complete(_complete_args(task_ids=[tid], external=URL, watcher="4209d31f3516")) == 0
    assert f"Completed {tid} (external: {URL}, watcher 4209d31f3516)" in capsys.readouterr().out
    with kb.connect_closing() as conn:
        tid2 = _blocked_task(conn)
    assert kcli._cmd_complete(_complete_args(task_ids=[tid2], external=URL)) == 1
    assert "no --watcher" in capsys.readouterr().err
    # per-task flag: refused for a bulk close
    assert kcli._cmd_complete(_complete_args(task_ids=[tid, tid2], external=URL, watcher="w")) == 2
