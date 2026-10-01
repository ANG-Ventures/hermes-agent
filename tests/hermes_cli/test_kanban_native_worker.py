"""Native foreign-lane worker (harness-parity spec 9.6, card t_3a8e4e30).

A profile with ``foreign_lane.worker_command`` is spawned as the no-LLM
runner ``hermes_cli.kanban_native_worker`` instead of ``hermes chat``;
the runner execs the lane and makes the receipt's one board call itself. A
profile without the knob keeps the shim argv unchanged.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

MODULE = "hermes_cli.kanban_native_worker"
_TREE = Path(__file__).resolve().parents[2]


def _make_task(kb, **over):
    fields = dict(
        id="t_native", title="native", body=None, assignee="lane-worker",
        status="running", priority=0, created_by="test", created_at=1,
        started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=7,
    )
    fields.update(over)
    return kb.Task(**fields)


def _profile(root: Path, lane_yaml: str) -> Path:
    profile = root / "profiles" / "lane-worker"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "model:\n  default: claude-bpr/claude-haiku-4-5\n  provider: claude-bpr\n" + lane_yaml,
        encoding="utf-8",
    )
    return profile


def _spawn(monkeypatch, tmp_path, lane_yaml: str, **task_over):
    root = tmp_path / ".hermes"
    _profile(root, lane_yaml)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    kbd._default_spawn(_make_task(kb, **task_over), str(workspace))
    return captured


def _module_tail(cmd):
    """argv after the dispatcher's platform prefix (taskpolicy on darwin)."""
    return cmd[cmd.index(sys.executable):] if sys.executable in cmd else cmd


# --------------------------------------------------------------------------
# Dispatcher: knob set / unset
# --------------------------------------------------------------------------

def test_knob_unset_keeps_the_llm_shim(monkeypatch, tmp_path):
    got = _spawn(monkeypatch, tmp_path, "foreign_lane:\n  harness: claude-code\n")
    assert "chat" in got["cmd"] and "-q" in got["cmd"]
    assert MODULE not in got["cmd"]
    assert "PYTHONPATH" not in got["env"]


def test_knob_set_spawns_native_runner_with_worker_env(monkeypatch, tmp_path):
    got = _spawn(monkeypatch, tmp_path,
                 "foreign_lane:\n  harness: claude-code\n  worker_command: [/bin/true]\n")
    cmd = _module_tail(got["cmd"])
    assert cmd[:3] == [sys.executable, "-m", MODULE]
    # The profile's route, as a shim's argv would state it (the lane's model gate reads it).
    assert cmd[3:] == ["-m", "claude-bpr/claude-haiku-4-5", "--provider", "claude-bpr"]
    assert "chat" not in cmd
    env = got["env"]
    assert env["PYTHONPATH"] == str(_TREE)
    assert env["HERMES_KANBAN_TASK"] == "t_native"
    assert env["HERMES_KANBAN_RUN_ID"] == "7"
    from agent.delegation_context import KANBAN_OWNER_PID_ENV, KANBAN_OWNER_PID_PENDING

    assert env[KANBAN_OWNER_PID_ENV] == KANBAN_OWNER_PID_PENDING


def test_knob_set_carries_card_override_and_effort(monkeypatch, tmp_path):
    got = _spawn(monkeypatch, tmp_path,
                 "foreign_lane:\n  worker_command: [/bin/true]\n",
                 model_override="claude-sonnet-5", provider_override="claude-bpr",
                 reasoning_effort="high")
    cmd = _module_tail(got["cmd"])
    assert cmd[3:] == ["-m", "claude-sonnet-5", "--provider", "claude-bpr", "--reasoning", "high"]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_tests_command_requires_exactly_one():
    from hermes_cli.kanban_native_worker import tests_command

    assert tests_command("x\nTest command: `pytest -q a.py`\nreview: human")[0] == "pytest -q a.py"
    assert tests_command("Test command: `true`\nTest command: `true`")[0] == "true"
    assert tests_command("no command here")[0] is None
    cmd, why = tests_command("Test command: `a`\nTest command: `b`")
    assert cmd is None and "2 different" in why


def test_build_argv_substitutes_per_argument():
    from hermes_cli.kanban_native_worker import build_argv

    argv = build_argv(["run_lane.py", "--task-id", "{task_id}", "--workspace", "{workspace}",
                       "--tests-cmd", "{tests_cmd}"],
                      task_id="t_1", workspace="/w", tests_cmd="pytest -q {x}")
    assert argv == ["run_lane.py", "--task-id", "t_1", "--workspace", "/w", "--tests-cmd", "pytest -q {x}"]
    with pytest.raises(ValueError):
        build_argv("run_lane.py --task-id {task_id}", task_id="t", workspace="/w", tests_cmd=None)


# --------------------------------------------------------------------------
# Runner: the handback path against a real (isolated) board
# --------------------------------------------------------------------------

def _board_with_running_card(monkeypatch, tmp_path, lane_script: str, body: str):
    home = tmp_path / ".hermes"
    profile = _profile(home, "")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lane = tmp_path / "lane.py"
    lane.write_text(textwrap.dedent(lane_script), encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        "foreign_lane:\n  worker_command: [" + json.dumps(sys.executable) + ", " + json.dumps(str(lane))
        + ", '{task_id}', '{workspace}', '{tests_cmd}']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_PROFILE", "lane-worker")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="lane card", body=body, assignee="lane-worker",
                             workspace_kind="scratch", workspace_path=str(workspace))
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        run_id = kb.get_task(conn, tid).current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim.claim_lock or "lock")
    from agent.delegation_context import KANBAN_OWNER_PID_ENV, KANBAN_OWNER_PID_PENDING

    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, KANBAN_OWNER_PID_PENDING)
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
    return kb, tid, lane


def _card(kb, tid):
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        events = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]
        return task, events
    finally:
        conn.close()


_REVIEW_LANE = """
    import json, sys
    task_id, ws, tests = sys.argv[1:4]
    print("lane working on", task_id, "tests:", tests)
    print(json.dumps({"verdict": "ok", "handback": {"tool": "kanban_request_review",
        "args": {"summary": "lane ok: " + tests, "metadata": {"harness": "fake", "tests_cmd": tests}}}}))
"""

_BLOCK_LANE = """
    import json, sys
    print(json.dumps({"verdict": "block", "handback": {"tool": "kanban_block",
        "args": {"reason": "tests_match_claim false [run metadata: x/receipt.json]"}, "metadata": {}}}))
    sys.exit(3)
"""

_SILENT_LANE = """
    import sys
    sys.stderr.write("setup: foreign_lane.harness missing\\n")
    sys.exit(2)
"""


def test_runner_hands_back_request_review(monkeypatch, tmp_path):
    kb, tid, _ = _board_with_running_card(monkeypatch, tmp_path, _REVIEW_LANE,
                                          "do it\nTest command: `pytest -q t.py`\n")
    from hermes_cli import kanban_native_worker as nw

    assert nw.run() == 0
    task, events = _card(kb, tid)
    assert task.status != "running"
    kinds = [k for k, _ in events]
    assert "review_requested" in kinds or "review_skipped" in kinds or task.status in ("review", "done")
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    assert run.summary == "lane ok: pytest -q t.py"
    assert (run.metadata or {}).get("tests_cmd") == "pytest -q t.py"


def test_runner_hands_back_block_reason_verbatim(monkeypatch, tmp_path):
    kb, tid, _ = _board_with_running_card(monkeypatch, tmp_path, _BLOCK_LANE,
                                          "Test command: `true`")
    from hermes_cli import kanban_native_worker as nw

    assert nw.run() == 0
    task, events = _card(kb, tid)
    assert task.status == "blocked"
    reasons = [(p or {}).get("reason") for k, p in events if k == "blocked"]
    assert reasons == ["tests_match_claim false [run metadata: x/receipt.json]"]


def test_runner_blocks_when_lane_exits_without_reporting(monkeypatch, tmp_path):
    kb, tid, _ = _board_with_running_card(monkeypatch, tmp_path, _SILENT_LANE,
                                          "Test command: `true`")
    from hermes_cli import kanban_native_worker as nw

    assert nw.run() == 0
    task, events = _card(kb, tid)
    assert task.status == "blocked"
    reason = [(p or {}).get("reason") for k, p in events if k == "blocked"][0]
    assert "rc=2 without reporting" in reason
    assert "setup: foreign_lane.harness missing" in reason


def test_runner_blocks_card_without_test_command_and_never_starts_lane(monkeypatch, tmp_path):
    kb, tid, lane = _board_with_running_card(monkeypatch, tmp_path, _REVIEW_LANE, "no command")
    lane.unlink()  # starting it would now fail loudly with a different reason
    from hermes_cli import kanban_native_worker as nw

    assert nw.run() == 0
    task, events = _card(kb, tid)
    assert task.status == "blocked"
    reason = [(p or {}).get("reason") for k, p in events if k == "blocked"][0]
    assert "card names no test command" in reason


def test_runner_refuses_a_handback_tool_outside_the_allowlist(monkeypatch, tmp_path):
    kb, tid, _ = _board_with_running_card(monkeypatch, tmp_path, """
        import json
        print(json.dumps({"handback": {"tool": "kanban_complete", "args": {"summary": "done"}}}))
    """, "Test command: `true`")
    from hermes_cli import kanban_native_worker as nw

    assert nw.run() == 0
    task, _ = _card(kb, tid)
    assert task.status == "blocked"
