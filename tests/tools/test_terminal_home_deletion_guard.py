"""The terminal tool's pre-exec chain refuses deletes of the Hermes home's protected roots.

Wiring test for ``agent.home_deletion_guard``: proves the guard sits in
``tools.terminal_tool._pre_exec_block`` (which ``force=True`` cannot bypass), sees the
backend's own env overlay, and leaves sub-path deletes alone. Temp-dir roots only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.terminal_tool as terminal_module


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "u" / ".hermes"
    (root / "profiles" / "aegis").mkdir(parents=True)
    (root / "cache" / "scratch").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    # Not inside a supervised gateway: the lifecycle guard is a no-op here.
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    return root


def _run(command: str, *, env_type: str = "local", env_overlay=None, cwd: str = "/tmp"):
    env = SimpleNamespace(env=env_overlay or {}, cwd=cwd)
    terminal_module._pre_exec_block(
        command, env=env, env_type=env_type, cwd=cwd, workdir=None, session_key="",
    )


def test_rm_rf_of_hermes_home_is_rejected(fake_home):
    with pytest.raises(terminal_module._Rejected) as exc:
        _run('rm -rf "$HERMES_HOME"')
    payload = json.loads(exc.value.result_json)
    assert payload["status"] == "blocked"
    assert payload.get("blocked_by") == "home_deletion_guard"
    assert str(fake_home.resolve()) in payload["error"]
    assert "mktemp -d" in payload["error"]


def test_script_invoked_by_path_is_rejected(fake_home, tmp_path):
    script = tmp_path / "rd.sh"
    script.write_text('#!/usr/bin/env bash\nrm -rf "$HERMES_HOME"\n')
    with pytest.raises(terminal_module._Rejected):
        _run(f"bash {script}")


def test_backend_env_overlay_is_used_for_expansion(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(terminal_module._Rejected):
        _run('rm -rf "$HERMES_HOME"', env_overlay={"HERMES_HOME": str(fake_home)})


def test_sub_path_delete_runs(fake_home):
    _run(f'rm -rf "$HERMES_HOME/cache/scratch"')  # no exception


def test_remote_backend_is_not_analyzed_against_local_paths(fake_home):
    _run('rm -rf "$HERMES_HOME"', env_type="ssh")  # remote filesystem: not this guard's job


def test_mutation_control_without_the_guard_the_incident_command_passes(fake_home, monkeypatch):
    """Discrimination check: with ``home_deletion_block`` stubbed out, nothing else in the
    pre-exec chain stops the incident command, so the refusal above is this guard's."""
    monkeypatch.setattr(terminal_module, "home_deletion_block", lambda **_k: None)
    _run('rm -rf "$HERMES_HOME"')  # no exception


@pytest.mark.platforms("posix")
def test_a_variable_exported_in_an_earlier_call_is_seen(fake_home, tmp_path):
    """Prism #2006: the guard analyzes the env the command really runs with, including the
    session snapshot that restores exports from earlier terminal calls."""
    from tools.environments.local import LocalEnvironment

    (fake_home / "runtime").mkdir(exist_ok=True)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        assert env._snapshot_ready
        env.execute('export D="$HERMES_HOME/runtime"')
        assert str(fake_home / "runtime") in env.execute('printf %s "$D"')["output"]
        cwd = str(tmp_path)
        with pytest.raises(terminal_module._Rejected):
            terminal_module._pre_exec_block('rm -rf "$D"', env=env, env_type="local", cwd=cwd,
                                            workdir=None, session_key="")
        terminal_module._pre_exec_block('rm -rf "$D/sub"', env=env, env_type="local", cwd=cwd,
                                        workdir=None, session_key="")  # below a root: allowed
    finally:
        env.cleanup()



def test_background_launch_does_not_see_the_foreground_snapshot(fake_home, tmp_path):
    """Prism 3f7b14131e14: a background spawn (``ProcessRegistry.spawn_local``) starts a fresh login
    shell from the process env + overlay and never sources the foreground snapshot. An earlier
    foreground ``export D=/tmp/cache`` must NOT be used to approve a background ``rm -rf "$D"`` when
    the inherited env says D is the live home."""
    import os
    from tools.environments.local import LocalEnvironment

    (fake_home / "runtime").mkdir(exist_ok=True)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        assert env._snapshot_ready
        env.execute('export D="%s"' % str(tmp_path / "cache"))     # foreground snapshot: harmless D
        env.env["D"] = str(fake_home / "runtime")                   # inherited overlay: the live home
        cwd = str(tmp_path)
        # foreground: the snapshot wins (restored last by the wrapper): D=/tmp/cache, allowed
        terminal_module._pre_exec_block('rm -rf "$D"', env=env, env_type="local", cwd=cwd,
                                        workdir=None, session_key="")
        # background: no snapshot, D is the live home: refused
        with pytest.raises(terminal_module._Rejected):
            terminal_module._pre_exec_block('rm -rf "$D"', env=env, env_type="local", cwd=cwd,
                                            workdir=None, session_key="", background=True)
    finally:
        env.cleanup()
