"""A refused block is loud and never counts as a block (t_0809e21a).

t_791348ae run 4: every ``kanban_block`` call was refused (a renamed ``reason``
key) and the CLI fallback was refused for the delegate-child marker. Nothing on
the card said so: it sat ``running`` with an idle worker. A refusal now leaves
a comment on the card, and it leaves the status and the unblock-loop counter
exactly as they were.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
_KANBAN_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
               "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACES_ROOT",
               "HERMES_KANBAN_OWNER_PID", "HERMES_DELEGATED_CHILD_CONTEXT")


@pytest.fixture
def running_card(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in _KANBAN_ENV:
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="block probe", assignee="builder", workspace_kind="scratch")
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return home, tid


def _card(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        comments = [c.body for c in kb.list_comments(conn, tid)]
    return task, comments


def test_refused_tool_block_is_commented_and_not_counted(running_card):
    _home, tid = running_card
    from tools import kanban_tools  # noqa: F401  registers the handlers
    from tools.registry import registry

    # The shape seen on t_791348ae: the reason key arrived renamed.
    result = json.loads(registry.dispatch(
        "kanban_block", {"dpx_v1_p_4e3d91cdb6b0985c3cf7e5a6": "waiting", "kind": "transient"}))
    assert "unknown parameter" in result.get("error", ""), result

    task, comments = _card(tid)
    assert task.status == "running"
    assert (task.block_recurrences or 0) == 0
    refusals = [c for c in comments if "kanban_block REFUSED" in c]
    assert len(refusals) == 1, comments
    assert "dpx_v1_p_4e3d91cdb6b0985c3cf7e5a6" in refusals[0]

    # The documented schema still blocks, and the refusal did not count toward the loop breaker.
    ok = json.loads(registry.dispatch("kanban_block", {"reason": "waiting on X", "kind": "transient"}))
    assert ok.get("ok"), ok
    task, _ = _card(tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.block_recurrences == 1


def test_successful_tool_block_posts_no_refusal(running_card):
    _home, tid = running_card
    from tools import kanban_tools  # noqa: F401
    from tools.registry import registry

    ok = json.loads(registry.dispatch("kanban_block", {"reason": "need input", "kind": "needs_input"}))
    assert ok.get("ok"), ok
    _task, comments = _card(tid)
    assert not [c for c in comments if "REFUSED" in c], comments


def test_refused_cli_block_from_delegate_child_is_commented(running_card):
    home, tid = running_card
    env = {k: v for k, v in os.environ.items() if k not in _KANBAN_ENV}
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_DELEGATED_CHILD_CONTEXT="1",
               PYTHONPATH=str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""))
    res = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "block", tid, "waiting", "--kind", "transient"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60)
    assert res.returncode != 0
    assert "delegate_task child contexts cannot mutate" in res.stderr

    task, comments = _card(tid)
    assert task.status == "running"
    assert (task.block_recurrences or 0) == 0
    refusals = [c for c in comments if "kanban block REFUSED" in c]
    assert len(refusals) == 1, (comments, res.stderr)
    assert "delegate_task child contexts cannot mutate" in refusals[0]
