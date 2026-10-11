"""``kanban_identity`` states the caller identity the create guard reads (t_f4c584e2).

The real CLI in a subprocess, stdin not a tty (the CI/off-box shape): with the fixture the
create is homed to the fixture's session; with it the guard is armed, so dropping the
identity reproduces the off-box refusal regardless of the shell running the suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _create(kanban_identity, env: dict | None = None) -> subprocess.CompletedProcess:
    env = kanban_identity.env if env is None else env
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "create", "probe", "--json"],
        env={**env, "PYTHONPATH": str(ROOT)}, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120)


def test_fixture_create_is_homed_to_the_stated_session(kanban_identity):
    r = _create(kanban_identity)
    assert r.returncode == 0, r.stderr
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, json.loads(r.stdout)["id"])
    assert task.session_id == kanban_identity.session_id


def test_fixture_arms_the_guard_so_a_sessionless_create_is_refused(kanban_identity):
    env = {k: v for k, v in kanban_identity.env.items() if k != "HERMES_SESSION_ID"}
    r = _create(kanban_identity, env)
    assert r.returncode == 2
    assert "refused create (script): no home session" in r.stderr
