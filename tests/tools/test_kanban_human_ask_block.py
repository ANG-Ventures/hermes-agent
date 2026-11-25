"""A block that waits on a human must carry Need: / Why: / How to reply: (t_2fecb5d0)."""

import argparse
import json

import pytest

GOOD = ("Need: pick the retry window.\nWhy: the relay keys are out of credit until 04:18.\n"
        "How to reply: A) 04:18 today  B) 16:00 today\nunblock_when: ruling:t_x-retry-window")


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid, home


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def test_tool_refuses_human_wait_without_fields(worker_env):
    from tools import kanban_tools as kt
    tid, _home = worker_env
    d = json.loads(kt._handle_block({"reason": "waiting on Ace: Dell specs decision",
                                     "kind": "needs_input"}))
    assert not d.get("ok"), d
    err = d["error"]
    assert "Missing: Need:, Why:, How to reply:" in err
    assert "  Need: <the one decision" in err and "  How to reply: <the exact reply" in err
    assert _status(tid) == "running"


def test_tool_names_only_the_missing_field(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Need: a time. Why: drill stops mem0.",
                                     "kind": "needs_input"}))
    assert "Missing: How to reply:." in d["error"], d


def test_tool_accepts_shaped_human_wait(worker_env):
    from tools import kanban_tools as kt
    tid, _home = worker_env
    d = json.loads(kt._handle_block({"reason": GOOD, "kind": "needs_input"}))
    assert d.get("ok") is True, d
    assert _status(tid) == "blocked"


@pytest.mark.parametrize("args", [
    {"reason": "waits on main's fix.\nunblock_when: pr_merged:ANG-Ventures/hermes-home#12721",
     "kind": "needs_input"},
    {"reason": "anchor closes on the experiment.\nunblock_when: card_done:t_e87a3e20",
     "kind": "needs_input"},
    {"reason": "no host carries the browser identity", "kind": "capability"},
    {"reason": "quota wall", "kind": "transient"},
])
def test_non_human_blocks_untouched(worker_env, args):
    from tools import kanban_tools as kt
    tid, _home = worker_env
    d = json.loads(kt._handle_block(args))
    assert d.get("ok") is True, d
    assert _status(tid) == "blocked"


def test_kill_switch_off(worker_env):
    from tools import kanban_tools as kt
    tid, home = worker_env
    (home / "config.yaml").write_text("kanban:\n  human_ask_shape: off\n")
    d = json.loads(kt._handle_block({"reason": "waiting on Ace", "kind": "needs_input"}))
    assert d.get("ok") is True, d
    assert _status(tid) == "blocked"


def test_cli_block_refuses_then_lands(worker_env, monkeypatch, capsys):
    from hermes_cli import kanban
    tid, _home = worker_env
    monkeypatch.setattr(kanban, "_bulk_ids", lambda args: [tid])

    def args(reason):
        return argparse.Namespace(task_id=tid, reason=[reason], why=False, kind="needs_input",
                                  until=None)

    assert kanban._cmd_block(args("waiting on Ace")) != 0
    assert "Missing: Need:, Why:, How to reply:" in capsys.readouterr().err
    assert _status(tid) == "running"
    assert kanban._cmd_block(args(GOOD)) == 0
    assert _status(tid) == "blocked"
