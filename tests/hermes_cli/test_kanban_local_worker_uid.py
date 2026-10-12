"""``kanban.local_worker_uid`` (Q8 (a), t_405acdf0): a LOCAL worker's tools run as a dedicated uid.

Contract: unset / empty / the dispatcher's own login spawns exactly as before (no placement env);
a login name makes ``_default_spawn`` place the worker's terminal + file tools on
``<login>@localhost`` through the same placement env the pool hosts use, read from the live
config on every spawn so the flip and its rollback are one edit.
"""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_local_worker_uid as klu


@pytest.mark.parametrize("cfg", [None, {}, {"local_worker_uid": None}, {"local_worker_uid": ""},
                                 {"local_worker_uid": "  "}, {"local_worker_uid": False}])
def test_unset_keeps_dispatcher_uid(cfg):
    assert klu.resolve_user(cfg, me="alice") is None


def test_own_login_and_own_uid_keep_dispatcher_uid():
    assert klu.resolve_user({"local_worker_uid": "alice"}, me="alice") is None
    assert klu.resolve_user({"local_worker_uid": os.getuid()}, me="alice") is None
    assert klu.resolve_user({"local_worker_uid": str(os.getuid())}, me="alice") is None


def test_dedicated_login_resolves():
    assert klu.resolve_user({"local_worker_uid": "_kanbanw"}, me="alice") == "_kanbanw"
    assert klu.resolve_user({"local_worker_uid": " kanbanw "}, me="alice") == "kanbanw"


@pytest.mark.parametrize("bad", ["root; rm -rf /", "Kanban W", "-oProxyCommand=x", "a" * 40])
def test_malformed_value_falls_back_never_strands(bad):
    assert klu.resolve_user({"local_worker_uid": bad}, me="alice") is None


def test_unknown_numeric_uid_falls_back():
    assert klu.resolve_user({"local_worker_uid": "4000000000"}, me="alice") is None


def test_local_host_targets_localhost_as_the_user():
    host = klu.local_host("_kanbanw")
    assert host.target == "_kanbanw@localhost"
    assert host.name == klu.LOCAL_HOST_NAME


def _task(kb):
    return kb.Task(
        id="t_405acdf0", title="slice", body=None, assignee="default", status="in_progress",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None,
    )


@pytest.fixture()
def spawn(monkeypatch, tmp_path):
    """Run the REAL ``_default_spawn`` env builder with a config value and a fake Popen."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_hosts as kwh

    def run(value):
        captured = {"mkdir": []}

        class _Proc:
            pid = 4321

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return _Proc()

        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        with monkeypatch.context() as m:
            m.setattr("hermes_cli.config.load_config_readonly",
                      lambda: {"kanban": {"local_worker_uid": value}})
            m.setattr(kwh, "prepare_remote_workspace",
                      lambda host, w, **_: captured["mkdir"].append((host.target, w)))
            m.setattr("subprocess.Popen", _fake_popen)
            m.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
            m.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
            kbd._default_spawn(_task(kb), str(ws))
        return captured, str(ws)

    return run


def test_spawn_flag_off_is_unplaced(spawn):
    captured, _ = spawn(None)
    env = captured["env"]
    assert "KANBAN_WORKER_PLACEMENT" not in env
    assert env.get("TERMINAL_ENV") != "ssh" or "TERMINAL_SSH_USER" not in env
    assert captured["mkdir"] == []


def test_spawn_flag_on_places_tools_as_the_dedicated_uid(spawn):
    captured, ws = spawn("_kanbanw")
    env = captured["env"]
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_USER"] == "_kanbanw"
    assert env["TERMINAL_SSH_HOST"] == "localhost"
    assert env["TERMINAL_CWD"] == ws
    assert env["TERMINAL_SSH_SYNC_FILES"] == "false"
    assert json.loads(env["KANBAN_WORKER_PLACEMENT"])["host"] == klu.LOCAL_HOST_NAME
    assert captured["mkdir"] == [("_kanbanw@localhost", ws)]


def test_rollback_is_one_edit(spawn):
    on, _ = spawn("_kanbanw")
    off, _ = spawn("")
    assert "KANBAN_WORKER_PLACEMENT" in on["env"]
    assert "KANBAN_WORKER_PLACEMENT" not in off["env"]
