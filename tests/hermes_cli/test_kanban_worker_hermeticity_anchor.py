"""Dispatcher worker spawn must export a correct ``HERMES_REAL_HOME`` anchor.

Card t_43d5c42d / the 2026-07-24 WAL incident. ``tests/conftest.py``'s
hermeticity canary decides whether a resolved ``state.db`` is the PRODUCTION
one by comparing against ``os.environ.get("HERMES_REAL_HOME", Path.home() /
".hermes")``. Under container / profile-home / custom-HOME layouts
``Path.home()`` is *not* the prod root, so the anchor has to be exported by
whoever spawns the process. Every other subprocess spawn already does that via
``hermes_constants.apply_subprocess_home_env`` (see
``tools/environments/local.py``); ``kanban_db._default_spawn`` was the one hot
path that omitted it, so a worker whose task is "run the suite" handed the
canary a guessed anchor.

The paired regression guard matters just as much: the fix must NOT reach for
the naive reading of the incident ("give workers a temp HERMES_HOME"). A
dispatcher worker is a real production agent — it reads the profile's
``config.yaml`` and persists to the profile's real ``state.db``. Repointing its
``HERMES_HOME`` at a throwaway dir would make every worker silently lose its
work. Test-run hermeticity is owned by the suite's own autouse fixture, which
re-hermeticizes inside the worker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not a stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state or path pins."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _spawn_and_capture_env(fresh_home, monkeypatch, task_id: str) -> dict:
    """Run ``_default_spawn`` with ``Popen`` stubbed; return the child env."""
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = kb.Task(
        id=task_id,
        title="hermeticity anchor",
        body=None,
        assignee="teknium",
        status="ready",
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
    kb._default_spawn(task, str(fresh_home / "ws"), board=None)
    return captured["env"]


def test_default_spawn_exports_real_home_anchor(fresh_home, monkeypatch):
    """The worker env carries a HERMES_REAL_HOME the canary can trust."""
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    env = _spawn_and_capture_env(fresh_home, monkeypatch, "t_anchor")
    real_home = env.get("HERMES_REAL_HOME")
    assert real_home, "worker env is missing the HERMES_REAL_HOME anchor"
    # It must name a real OS-account home, not the Hermes profile state dir.
    assert real_home != str(fresh_home)


def test_default_spawn_keeps_the_profile_hermes_home(fresh_home, monkeypatch):
    """Regression guard: the anchor must not hijack HERMES_HOME.

    A per-task tempdir here would make the worker read the wrong config.yaml
    and persist its session to a directory that is deleted after the run.
    """
    env = _spawn_and_capture_env(fresh_home, monkeypatch, "t_home")
    assert env["HERMES_HOME"] == str(fresh_home)
