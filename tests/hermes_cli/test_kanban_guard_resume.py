"""An open PR is not a duplicate when a worker explicitly waits on parents."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def test_worker_dependency_wait_promoted_continues_pr_once(board):
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="implement", assignee="worker")
        claim = kb.claim_task(conn, child)
        assert claim is not None
        kb.add_comment(conn, child, "worker", "https://github.com/o/r/pull/9")
        parent = kb.create_task(conn, title="prerequisite", assignee="worker")
        kb.link_tasks(conn, parent, child, expected_child_run_id=claim.current_run_id)
        assert kb.block_task(conn, child, kind="dependency", reason="resume then complete")
        assert kb.complete_task(conn, parent, summary="prerequisite complete")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kbd.check_respawn_guard(conn, child) is None
        assert kb.claim_task(conn, child)
        with kb.write_txn(conn):
            kb._append_event(conn, child, "spawned", {"pid": 42})
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL WHERE id=?", (child,))
        assert kbd.check_respawn_guard(conn, child) == "active_pr"


def test_operator_requeue_ready_card(board):
    from hermes_cli import kanban as kc
    from hermes_cli.kanban_parser import build_parser
    import argparse

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="retry", assignee="worker")
        kb.add_comment(conn, task_id, "worker", "https://github.com/o/r/pull/9")
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        assert kb.requeue_task(conn, task_id, actor="operator", reason=" ") == (False, "a reason is required")
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="cmd"))
    assert kc.kanban_command(parser.parse_args(["kanban", "requeue", task_id, "continue", "PR"])) == 0
    with kbc.connect() as conn:
        assert kbd.check_respawn_guard(conn, task_id) is None
        kb.add_comment(conn, task_id, "worker", "new https://github.com/o/r/pull/10")
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        assert kb.requeue_task(conn, task_id, actor="operator", reason="again") == (True, None)
        assert kbd.check_respawn_guard(conn, task_id) is None
        kb.add_comment(conn, task_id, "worker", "status update without a PR")
        assert kbd.check_respawn_guard(conn, task_id) is None
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "spawned", {"pid": 42})
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        assert kb.block_task(conn, task_id, kind="needs_input", reason="wait")
        assert kb.requeue_task(conn, task_id, actor="operator", reason="again")[0] is False


def test_ordinary_comment_after_wait_does_not_cancel_pr_resume(board):
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="implement", assignee="worker")
        claim = kb.claim_task(conn, child)
        assert claim is not None
        kb.add_comment(conn, child, "worker", "https://github.com/o/r/pull/9")
        parent = kb.create_task(conn, title="prerequisite", assignee="worker")
        kb.link_tasks(conn, parent, child, expected_child_run_id=claim.current_run_id)
        assert kb.block_task(conn, child, kind="dependency", reason="resume")
        kb.add_comment(conn, child, "worker", "parent still pending")
        assert kb.complete_task(conn, parent, summary="prerequisite complete")
        kb.recompute_ready(conn)
        assert kbd.check_respawn_guard(conn, child) is None


def test_new_pr_comment_after_wait_does_not_resume(board):
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="implement", assignee="worker")
        claim = kb.claim_task(conn, child)
        assert claim is not None
        kb.add_comment(conn, child, "worker", "https://github.com/o/r/pull/9")
        parent = kb.create_task(conn, title="prerequisite", assignee="worker")
        kb.link_tasks(conn, parent, child, expected_child_run_id=claim.current_run_id)
        assert kb.block_task(conn, child, kind="dependency", reason="resume")
        kb.add_comment(conn, child, "worker", "https://github.com/o/r/pull/10")
        assert kb.complete_task(conn, parent, summary="prerequisite complete")
        kb.recompute_ready(conn)
        assert kbd.check_respawn_guard(conn, child) == "active_pr"
