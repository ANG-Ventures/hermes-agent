"""Delegated-child terminal commands start under darwin background QoS (t_ba9c58b0)."""
from __future__ import annotations

import pytest

from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, delegated_child_context
from tools.environments import delegated_child_qos as qos


@pytest.fixture
def darwin(monkeypatch, tmp_path):
    """Pretend to be macOS with taskpolicy present and the off switch under tmp_path."""
    monkeypatch.setattr(qos, "_IS_DARWIN", True)
    monkeypatch.setattr(qos.os, "access", lambda path, mode: path == qos.TASKPOLICY)
    switch = tmp_path / "state" / "spawn-guard" / qos.OFF_SWITCH_NAME
    monkeypatch.setattr(qos, "off_switch_path", lambda: switch)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    return switch


def test_delegated_child_gets_background_prefix(darwin):
    with delegated_child_context():
        assert qos.background_prefix() == [qos.TASKPOLICY, "-b"]


def test_process_env_marker_also_counts(darwin, monkeypatch):
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "/tmp/root")
    assert qos.background_prefix() == [qos.TASKPOLICY, "-b"]


def test_operator_session_is_untouched(darwin):
    assert qos.background_prefix() == []


def test_off_switch_is_read_per_call(darwin):
    with delegated_child_context():
        darwin.parent.mkdir(parents=True)
        darwin.write_text("off\n")
        assert qos.background_prefix() == []
        darwin.unlink()
        assert qos.background_prefix() == [qos.TASKPOLICY, "-b"]


def test_noop_off_macos(darwin, monkeypatch):
    monkeypatch.setattr(qos, "_IS_DARWIN", False)
    with delegated_child_context():
        assert qos.background_prefix() == []


def test_noop_without_taskpolicy(darwin, monkeypatch):
    monkeypatch.setattr(qos.os, "access", lambda path, mode: False)
    with delegated_child_context():
        assert qos.background_prefix() == []


def test_default_off_switch_lives_under_the_root_spawn_guard_dir():
    path = qos.off_switch_path()
    assert path.parts[-3:] == ("state", "spawn-guard", "delegated-background.disabled")


def test_run_bash_argv_starts_with_taskpolicy(darwin, monkeypatch, tmp_path):
    from tools.environments import local

    seen = {}

    class _Proc:
        pid = 0

    def fake_spawn(args, **kwargs):
        seen["args"] = list(args)
        return _Proc()

    monkeypatch.setattr(local, "spawn_popen", fake_spawn)
    monkeypatch.setattr(local, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 0)
    env = local.LocalEnvironment.__new__(local.LocalEnvironment)
    env.env = {}
    env.cwd = str(tmp_path)
    with delegated_child_context():
        env._run_bash("echo hi")
    assert seen["args"][:2] == [qos.TASKPOLICY, "-b"]
    assert seen["args"][-2:] == ["-c", "echo hi"]
    env._run_bash("echo hi")
    assert seen["args"][0] != qos.TASKPOLICY


def test_background_process_argv_starts_with_taskpolicy(darwin, monkeypatch):
    from tools import process_registry
    from tools.process_registry import ProcessRegistry, ProcessSession

    monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/bash")

    reg = ProcessRegistry.__new__(ProcessRegistry)
    session = ProcessSession(id="proc_x", command="sleep 1")
    with delegated_child_context():
        argv = reg._scope_argv(session, "sleep 1", "proc_x", "Local")
    assert argv[:2] == [qos.TASKPOLICY, "-b"]
    assert argv[-1] == "set +m; sleep 1"
