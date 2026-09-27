"""A dispatcher-spawned worker must import the runtime tree, never an inherited PYTHONPATH.

Incident 2026-09-25 (t_e8c867d3): the default gateway's launchd plist pinned
``PYTHONPATH=<runtime>/releases/<sha>`` (registry-pins v0.2 side-by-side release).
``_default_spawn`` copied ``os.environ`` wholesale and exec'd ``venv/bin/hermes``
directly (not the ``~/.local/bin/hermes`` shim that unsets PYTHONPATH), so every
worker imported the pinned release. PYTHONPATH outranks the venv's editable finder,
so fixes deployed to the runtime tree (#1075 review-policy board-home resolution)
never reached any worker: 25/25 post-cutover handoffs routed to review/human,
0 review_skipped. Probed live in a worker: kanban_db.__file__ was the release dir.
"""

import os
import subprocess
import sys

import pytest


def _task(kb):
    return kb.Task(
        id="t_e8c867d3",
        title="slice",
        body=None,
        assignee="default",
        status="in_progress",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


@pytest.fixture()
def spawn_env(monkeypatch, tmp_path):
    """Run the REAL spawn env builder under a release-pinned dispatcher env."""
    from hermes_cli import kanban_db as kb

    decoy = tmp_path / "decoy_release"
    pkg = decoy / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "kanban_db.py").write_text("DECOY = True\n")
    monkeypatch.setenv("PYTHONPATH", str(decoy))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "decoy_home"))

    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _Proc()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Fakes scoped to the spawn call only: the probe arm needs the real Popen.
    with monkeypatch.context() as m:
        m.setattr("subprocess.Popen", _fake_popen)
        m.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
        m.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
        kb._default_spawn(_task(kb), str(workspace))
    return captured["env"], decoy


def test_worker_env_drops_inherited_pythonpath_and_pythonhome(spawn_env):
    env, _decoy = spawn_env
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env


def test_worker_interpreter_does_not_import_the_pinned_release(spawn_env):
    """Real interpreter, real spawn env: the decoy release on the dispatcher's
    PYTHONPATH must not shadow the installed hermes_cli."""
    env, decoy = spawn_env
    env = dict(env)
    env.pop("PYTHONHOME", None)  # a bogus PYTHONHOME would kill the probe itself
    out = subprocess.run(
        [sys.executable, "-c", "import hermes_cli.kanban_db as k; print(k.__file__)"],
        env=env, capture_output=True, text=True, cwd="/", timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert str(decoy) not in out.stdout, out.stdout
