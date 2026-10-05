"""Worker placement on a pool host (t_5981ff03, KWLB v0.1 PRD 5.6).

Contract: a placed worker's terminal and file tools run on the pool host
because the dispatcher's placement is re-applied over the profile's
``terminal.backend`` after every config bridge. A re-apply that fails on a
worker placed at boot is LOUD (RC-9): one WARN per run and the run's
``placement_reapply_failed`` counter. Which hosts and cards:
``test_kanban_worker_pool.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp

HOST = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user="kanbanw", slots=2,
                    capacity_pct=0.8, absence="required", profiles=("alpha",),
                    state="active", enabled=True, priority=0)


def test_placement_env_survives_profile_terminal_backend():
    env: dict = {}
    kwh.apply_placement(env, HOST, "/Volumes/fleet-scratch/workspaces/default/t_x")
    # The profile's config.yaml bridge has pulled the backend back to local.
    env.update({"TERMINAL_ENV": "local", "TERMINAL_CWD": "."})
    assert kwh.reapply_placement_env(env) == "ace-ai"
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_USER"] == "kanbanw"
    assert env["TERMINAL_CWD"] == "/Volumes/fleet-scratch/workspaces/default/t_x"


def test_terminal_tool_config_bridge_keeps_placement(monkeypatch):
    import tools.terminal_tool as tt
    env: dict = {}
    kwh.apply_placement(env, HOST, "/tmp/ws")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def fake_bridge(env=None, override=False):
        import os
        os.environ["TERMINAL_ENV"] = "local"

    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"terminal": {"backend": "local"}})
    monkeypatch.setattr("hermes_cli.config.apply_terminal_config_to_env", fake_bridge)
    cfg = tt._get_env_config()
    assert cfg["env_type"] == "ssh"
    assert cfg["ssh_user"] == "kanbanw"


@pytest.fixture
def reapply_state(monkeypatch):
    recorded = []
    monkeypatch.setattr(kwh, "_reapply", {"failed": 0, "warned": False})
    monkeypatch.setattr(kwh, "_record_reapply_failure", recorded.append)
    return recorded


@pytest.mark.parametrize("boot,placed,expect_host,expect_failures", [
    ("ace-ai", True, "ace-ai", 0),   # placed at boot, re-apply holds
    ("ace-ai", False, None, 1),      # placed at boot, variable gone: loud
    (None, False, None, 0),          # an unplaced worker: the normal local case
])
def test_rc9_reapply_table(reapply_state, caplog, boot, placed, expect_host, expect_failures):
    caplog.set_level(logging.WARNING, logger=kwh.__name__)
    environ: dict = {}
    if placed:
        kwh.apply_placement(environ, HOST, "/tmp/ws")
    for _ in range(2):
        assert kwh.reapply_or_record("terminal", environ, boot_host=boot) == expect_host
    assert kwh.reapply_failures() == 2 * expect_failures
    assert reapply_state == list(range(1, 2 * expect_failures + 1))
    warns = [r for r in caplog.records if "re-apply failed" in r.getMessage()]
    assert len(warns) == (1 if expect_failures else 0)


def test_rc9_reapply_that_raises_is_counted_not_propagated(reapply_state, monkeypatch):
    def boom(environ=None):
        raise KeyError("TERMINAL_ENV")
    monkeypatch.setattr(kwh, "reapply_placement_env", boom)
    assert kwh.reapply_or_record("file", {}, boot_host="ace-ai") is None
    assert reapply_state == [1]


def test_rc9_counter_lands_on_the_run_metadata(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha")
        run_id = kb.claim_task(conn, tid).current_run_id
        kb.merge_run_metadata(conn, run_id, {"other": 1})
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    kwh._record_reapply_failure(3)
    with kb.connect_closing() as conn:
        meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?",
                                       (run_id,)).fetchone()[0])
    assert meta == {"other": 1, kwh.REAPPLY_FAILED_KEY: 3}
    # Prism r4 b64f07c0: a concurrent call's older count landing last never
    # lowers the stored counter.
    kwh._record_reapply_failure(2)
    with kb.connect_closing() as conn:
        meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?",
                                       (run_id,)).fetchone()[0])
    assert meta[kwh.REAPPLY_FAILED_KEY] == 3


def test_rc9_reapply_table_after_boot_capture(reapply_state, monkeypatch):
    """Apollo r4 (C): with the host recorded by ``capture_boot_placement``
    (no explicit ``boot_host``), the None path still counts every call."""
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    environ: dict = {}
    kwh.apply_placement(environ, HOST, "/tmp/ws")
    assert kwh.capture_boot_placement(environ) == HOST.name
    assert kwh.reapply_or_record("terminal", environ) == HOST.name
    environ.pop(kwh.PLACEMENT_ENV)
    for _ in range(2):
        assert kwh.reapply_or_record("terminal", environ) is None
    assert kwh.reapply_failures() == 2 and reapply_state == [1, 2]


def test_r6_malformed_placement_counts_as_a_failure(reapply_state, caplog):
    """Prism 361860f7dee5: a placement whose TERMINAL_* value is not a str is
    NOT applied, and reapply_or_record must count it, not report the host."""
    caplog.set_level(logging.WARNING, logger=kwh.__name__)
    environ: dict = {}
    kwh.apply_placement(environ, HOST, "/tmp/ws")
    data = json.loads(environ[kwh.PLACEMENT_ENV])
    data["env"]["TERMINAL_CWD"] = 7
    environ[kwh.PLACEMENT_ENV] = json.dumps(data)
    fresh: dict = {kwh.PLACEMENT_ENV: environ[kwh.PLACEMENT_ENV]}
    assert kwh.reapply_placement_env(fresh) is None
    assert "TERMINAL_ENV" not in fresh            # nothing partially applied
    assert kwh.reapply_or_record("terminal", environ, boot_host="ace-ai") is None
    assert reapply_state == [1]


def test_r6_close_never_lowers_the_reapply_counter(tmp_path, monkeypatch):
    """Prism 94214e112dc1: _carry_run_counters keeps max(stored, incoming)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha")
        run_id = kb.claim_task(conn, tid).current_run_id
        kb.merge_run_metadata(conn, run_id, {kwh.REAPPLY_FAILED_KEY: 5})
        low = kb._carry_run_counters(conn, run_id, {kwh.REAPPLY_FAILED_KEY: 2, "x": 1})
        absent = kb._carry_run_counters(conn, run_id, {"x": 1})
        high = kb._carry_run_counters(conn, run_id, {kwh.REAPPLY_FAILED_KEY: 9})
    assert low[kwh.REAPPLY_FAILED_KEY] == 5 and low["x"] == 1
    assert absent[kwh.REAPPLY_FAILED_KEY] == 5
    assert high[kwh.REAPPLY_FAILED_KEY] == 9


def test_r8_placement_without_terminal_env_is_not_a_success(reapply_state):
    environ = {kwh.PLACEMENT_ENV: json.dumps({"host": "ace-ai", "env": {"UNRELATED": "x"}})}
    assert kwh.reapply_placement_env(environ) is None
    assert kwh.reapply_or_record("terminal", environ, boot_host="ace-ai") is None
    assert reapply_state == [1]


def test_unreadable_local_workspace_fails_closed(tmp_path, monkeypatch):
    assert kwh.local_workspace_has_content(None) is False
    assert kwh.local_workspace_has_content(str(tmp_path / "missing")) is False

    def boom(path):
        raise PermissionError(13, "denied", path)

    monkeypatch.setattr(kwh.os, "scandir", boom)
    assert kwh.local_workspace_has_content(str(tmp_path)) is True
