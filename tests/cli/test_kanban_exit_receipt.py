"""Exercise the real CLI result -> process exit -> durable receipt boundary."""
import json
import os
from types import SimpleNamespace

import pytest

import cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def cli_worker(tmp_path, monkeypatch):
    path = tmp_path / "run.exit.json"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(os.getpid()))
    monkeypatch.setenv("HERMES_KANBAN_EXIT_FILE", str(path))
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli.atexit, "register", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: None)
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a: False)
    # Image enrichment has no card to read; this is not a real board worker.
    monkeypatch.setattr(kb, "get_task", lambda *a: None)
    return path


def fake_cli(result):
    class FakeCLI:
        def __init__(self, **kwargs):
            self.provider = "test"
            self.model = "test"
            self.session_id = "test-session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"
            self.agent = SimpleNamespace(session_id=self.session_id,
                                         run_conversation=lambda **kw: result)

        def _claim_active_session(self, *a, **kw):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, *a):
            return {"signature": "same", "model": None, "runtime": None, "request_overrides": None}

        def _init_agent(self, **kwargs):
            return True
    return FakeCLI


@pytest.mark.parametrize("reason,error,code", [
    ("rate_limit", "HTTP 429 rate limit", 75),
    ("billing", "insufficient credits", 75),
    ("pool_exhausted", 'HTTP 503 {"error":"no eligible sub"}', 75),
    ("overloaded", 'HTTP 503 {"error":"no eligible sub"}', 75),
    ("overloaded", "HTTP 503 service unavailable", 1),
    ("tool_error", "tool execution failed", 1),
    (None, "", 0),
])
def test_cli_publishes_terminal_result(cli_worker, monkeypatch, capsys, reason, error, code):
    result = {"failed": reason is not None, "failure_reason": reason,
              "error": error, "final_response": ""}
    monkeypatch.setattr(cli, "HermesCLI", fake_cli(result))
    # A quota failure must not run the judge and sticky-block a healthy task.
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    judge_calls = []
    monkeypatch.setattr(cli, "_run_kanban_goal_loop_q", lambda *a: judge_calls.append(True))
    with pytest.raises(SystemExit) as exc:
        cli.main(query="work", quiet=True, toolsets="terminal")
    assert exc.value.code == code
    assert "session_id: test-session" in capsys.readouterr().err
    payload = json.loads(cli_worker.read_text())
    assert payload["exit_code"] == code
    assert payload["failure_reason"] == reason
    assert isinstance(payload["ts"], (int, float))
    assert "error" not in payload
    assert judge_calls == ([] if reason else [True])


def test_goal_continuation_quota_escapes_before_judge_block(cli_worker, monkeypatch):
    from hermes_cli import goals
    task = SimpleNamespace(title="task", body="", goal_max_turns=3)
    monkeypatch.setattr(kb, "get_task", lambda *a: task)
    monkeypatch.setattr(cli, "_goal_loop_run_id", lambda *a: 1)
    monkeypatch.setattr(kb, "goal_run_status", lambda *a: "running")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "more", False, None, False))
    blocked = []
    monkeypatch.setattr(kb, "block_task", lambda *a, **kw: blocked.append(kw))
    instance = fake_cli({"failed": True, "failure_reason": "pool_exhausted",
                         "error": "no eligible sub"})()
    with pytest.raises(SystemExit) as exc:
        cli._run_kanban_goal_loop_q(instance, "working")
    assert exc.value.code == 75
    assert blocked == []


@pytest.mark.parametrize("reason", ["rate_limit", "billing", "pool_exhausted", "overloaded"])
@pytest.mark.parametrize("task_grant", [None, ""])
@pytest.mark.parametrize("owner_marker", [False, True])
def test_quiet_cli_without_task_keeps_failure_exit_one(
    cli_worker, monkeypatch, reason, task_grant, owner_marker,
):
    # No Kanban environment is the ordinary CLI path. Empty task grants and
    # an otherwise valid owner marker must not opt it into worker exit codes.
    for name in list(os.environ):
        if name.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(name)
    if task_grant is not None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_grant)
    if owner_marker:
        monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(os.getpid()))
    result = {"failed": True, "failure_reason": reason,
              "error": 'HTTP 503 {"error":"no eligible sub"}', "final_response": ""}
    monkeypatch.setattr(cli, "HermesCLI", fake_cli(result))
    with pytest.raises(SystemExit) as exc:
        cli.main(query="ordinary quiet CLI", quiet=True, toolsets="terminal")
    assert exc.value.code == 1
    assert not cli_worker.exists()


@pytest.mark.parametrize("reason", ["rate_limit", "billing", "pool_exhausted", "overloaded"])
def test_inherited_child_cannot_overwrite_worker_receipt(cli_worker, monkeypatch, reason):
    from hermes_cli.kanban_worker_exit import WorkerExit, report_exit
    cli_worker.write_text('{"exit_code":75}', encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(os.getpid() + 1))
    exc = WorkerExit({"failed": True, "failure_reason": reason, "error": "no eligible sub"})
    assert exc.code == 1
    report_exit(exc)
    assert json.loads(cli_worker.read_text(encoding="utf-8")) == {"exit_code": 75}
