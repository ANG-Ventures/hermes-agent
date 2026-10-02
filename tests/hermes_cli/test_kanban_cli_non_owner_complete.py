"""A non-owning process must not close a running card through the CLI.

``_worker_run_id_for`` correctly returns ``None`` for a process that carries a
worker's inherited ``HERMES_KANBAN_*`` env but does not hold the owner grant
(``HERMES_KANBAN_OWNER_PID``). ``complete_task`` reads ``expected_run_id=None``
as "human operator, no run guard", so before this fix the non-owner closed
the card unconditionally: the 2026-08-12 incident class (card t_09b90233, a
nested process closed its parent's card while the parent was still running).

These tests drive the real ``hermes kanban`` CLI in subprocesses against a
temp ``HERMES_HOME``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

_WORKER_VARS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_OWNER_PID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
)


def _run(home: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in _WORKER_VARS:
        env.pop(name, None)
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(home / "workspaces")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60,
    )


def _running_card(home: Path) -> tuple[str, int]:
    created = _run(home, "create", "non-owner complete probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run(home, "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    status, run_id = _state(home, task_id)
    assert status == "running" and run_id is not None
    return task_id, run_id


def _state(home: Path, task_id: str) -> tuple[str, int | None]:
    """(status, id of the open run) read back through ``kanban show``."""
    shown = _run(home, "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    data = json.loads(shown.stdout)
    open_runs = [r["id"] for r in data.get("runs") or [] if r.get("ended_at") is None]
    return data["task"]["status"], (open_runs[-1] if open_runs else None)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes"
    h.mkdir()
    return h


def _worker_env(task_id: str, run_id: int, owner: str) -> dict:
    return {
        "HERMES_KANBAN_TASK": task_id,
        "HERMES_KANBAN_RUN_ID": str(run_id),
        "HERMES_KANBAN_OWNER_PID": owner,
    }


def test_non_owner_child_cannot_complete_parents_running_card(home):
    """Grant names ANOTHER pid (a nested process of the live worker)."""
    task_id, run_id = _running_card(home)

    out = _run(home, "complete", task_id, "--summary", "impostor",
               extra=_worker_env(task_id, run_id, str(os.getpid())))

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)


def test_non_owner_child_cannot_block_or_review_parents_running_card(home):
    """Sibling terminal writes on the same CLI path share the hole."""
    task_id, run_id = _running_card(home)
    env = _worker_env(task_id, run_id, str(os.getpid()))

    blocked = _run(home, "block", task_id, "impostor block", extra=env)
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    assert _state(home, task_id) == ("running", run_id)

    reviewed = _run(home, "request-review", task_id, "--summary", "impostor", extra=env)
    assert reviewed.returncode != 0, reviewed.stdout + reviewed.stderr
    assert _state(home, task_id) == ("running", run_id)


def test_owner_still_completes_its_own_card(home):
    """Positive control: the process that binds a ``pending`` grant is the worker."""
    task_id, run_id = _running_card(home)

    out = _run(home, "complete", task_id, "--summary", "owner done",
               "--metadata", '{"tests_run": 1}',
               extra=_worker_env(task_id, run_id, "pending"))

    assert out.returncode == 0, out.stdout + out.stderr
    assert _state(home, task_id)[0] == "done"


def test_operator_without_worker_env_still_completes(home):
    """Documented operator override: a shell with no worker env closes the card."""
    task_id, _ = _running_card(home)

    out = _run(home, "complete", task_id, "--summary", "operator close")

    assert out.returncode == 0, out.stdout + out.stderr
    assert _state(home, task_id)[0] == "done"
