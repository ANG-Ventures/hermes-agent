"""Per-execution identity markers must never persist into the shell snapshot.

A single long-lived backend environment serves many executions: the messaging
gateway, TUI and dashboard collapse the terminal to one "default" environment,
and ``delegate_task`` children plus dispatcher-owned Kanban workers all run
their commands through it.

``HERMES_DELEGATED_CHILD_CONTEXT`` and ``HERMES_KANBAN_*`` describe WHO is
executing right now, not the user's shell state. If ``export -p`` captures
them into the shared snapshot, every LATER command sources them back --
including commands belonging to the ordinary parent session, long after the
child that set them exited.

Observed live: an orchestrator's own shell reported
``HERMES_DELEGATED_CHILD_CONTEXT=1`` hours after its delegated child finished,
so ``hermes kanban comment/complete`` refused every write with "delegate_task
child contexts cannot mutate Kanban tasks via the CLI". Unsetting the variable
in the caller could not fix it: the snapshot re-exported it on the next source.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)

IDENTITY_VARS = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_CLAIM_LOCK",
)


def _bash() -> str:
    return "/bin/bash" if os.path.exists("/bin/bash") else "bash"


@pytest.mark.parametrize("name", IDENTITY_VARS)
def test_identity_marker_matches_python_side_exclusion_contract(name: str):
    """The declared exclusion regex must recognise both shell dump forms."""
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    assert rx.search(f'declare -x {name}="1"')
    assert rx.search(f"export {name}=1")


def test_exclusion_regex_still_spares_ordinary_user_vars():
    """Prefix matching must not swallow unrelated names the user owns."""
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for keep in (
        'declare -x HERMES_HOME_BACKUP="/x"',
        'declare -x HERMES_DELEGATED_CHILD_CONTEXT_BACKUP="1"',
        'declare -x PATH="/usr/bin"',
    ):
        assert not rx.search(keep), keep


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_identity_markers_absent_from_real_dump_and_ordinary_vars_survive(
    tmp_path: Path,
):
    """End-to-end: run the real dump snippet under bash with markers exported.

    This is the assertion that actually gates the bug -- the Python-side regex
    is only a declared contract, while the dump path unsets by name/prefix.
    """
    snap = tmp_path / "snap"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))

    env = os.environ.copy()
    for name in IDENTITY_VARS:
        env[name] = "1"
    env["HERMES_HOME_BACKUP"] = "/keep/me"

    proc = subprocess.run(
        [_bash(), "-c", dump],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    body = snap.read_text()
    for name in IDENTITY_VARS:
        assert f"{name}=" not in body, f"{name} leaked into the shared snapshot"

    # The snapshot must still carry genuine user shell state.
    assert re.search(r"^declare -x PATH=", body, re.M)
    assert "HERMES_HOME_BACKUP=" in body
