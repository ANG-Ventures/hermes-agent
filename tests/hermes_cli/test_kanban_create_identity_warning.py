"""``hermes kanban create`` says so on stderr when a session create will finish silently.

Regression for t_be44b437: creates from a session child that lost the gateway identity
(``HERMES_SESSION_ID`` set, no ``HERMES_SESSION_PLATFORM``/``CHAT_ID``) wrote no notify
subscription and the only signal was a dispatcher line the caller never read. Runs the real
CLI in a child process against a temp board and reads its stderr.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.kanban import _SILENT_CREATE_WARNING

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def child_env(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  cli_auto_subscribe: true\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "HERMES_SESSION_", "HERMES_UI_SESSION",
                                "HERMES_CRON", "HERMES_DELEGATED", "_HERMES_GATEWAY"))}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO))
    return env


def _create(env: dict, title: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "create", title,
         "--assignee", "worker1", "--home", "operator"],
        env=env, capture_output=True, text=True, timeout=180, cwd=str(REPO))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert re.search(r"Created\s+t_[0-9a-f]+", proc.stdout), proc.stdout
    return proc


def test_session_without_platform_identity_warns_on_stderr(child_env):
    child_env["HERMES_SESSION_ID"] = "20261007_112633_f9beb5f8"
    proc = _create(child_env, "lost identity")
    assert _SILENT_CREATE_WARNING in proc.stderr
    assert _SILENT_CREATE_WARNING not in proc.stdout


def test_full_gateway_identity_subscribes_and_stays_quiet(child_env):
    child_env.update(HERMES_SESSION_ID="s1", HERMES_SESSION_PLATFORM="discord",
                     HERMES_SESSION_CHAT_ID="1554668201428918292")
    proc = _create(child_env, "has identity")
    assert _SILENT_CREATE_WARNING not in proc.stderr
    assert "Subscribed the calling session" in proc.stdout


def test_bare_cli_create_without_session_stays_quiet(child_env):
    """#19718: a plain CLI/cron create has no session and no chat to tell."""
    proc = _create(child_env, "bare cli")
    assert _SILENT_CREATE_WARNING not in proc.stderr
