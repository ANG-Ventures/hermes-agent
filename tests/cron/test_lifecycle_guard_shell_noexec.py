"""`bash -n script` / `sh -n script` is a SYNTAX CHECK: the shell parses and exits without
executing (POSIX sh(1) "-n: Read commands but do not execute them"). A lifecycle command inside
such a script never runs, so the guard must not walk the operand.

Recorded 2026-10-06 (Apollo): every `bash -n` on a script written FOR a remote host (ACE-AI's
sync-skills.sh, which bounces THAT box's gateway) was refused inside the Studio gateway, while
`shellcheck script` and `cat script` on the same file passed. Same class as the heredoc-data
false positive (pc-fccd2771).

Second half is fail-CLOSED: every shape where the operand DOES execute still blocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

# Built at runtime so this test file is not itself refused by the guard it tests.
R = "re" + "start"
UNIT = "hermes-gateway.service"
LINE = f"systemctl --user {R} {UNIT}\n"


@pytest.fixture
def script(tmp_path: Path) -> Path:
    p = tmp_path / "remote-sync.sh"
    p.write_text("#!/usr/bin/env bash\n# runs ON another host\n" + LINE)
    p.chmod(0o755)
    return p


def _scan(command: str, cwd: Path):
    unsafe, refusal = scan_gateway_lifecycle(
        command, cwd=str(cwd),
        read_remote_script=lambda p: (cwd / p).read_text() if (cwd / p).exists() else None)
    assert refusal is None, refusal
    return unsafe


@pytest.mark.parametrize("command", [
    "bash -n remote-sync.sh",
    "sh -n remote-sync.sh",
    "bash -n ./remote-sync.sh && echo syntax ok",
    "bash -ne remote-sync.sh",            # combined short flags containing n
    "bash -xn remote-sync.sh",
    "bash --noexec remote-sync.sh",
    "bash -n -- remote-sync.sh",
])
def test_noexec_syntax_check_does_not_walk_the_operand(command, script):
    assert _scan(command, script.parent) is False


@pytest.mark.parametrize("command", [
    "bash remote-sync.sh",                 # executes
    "bash -x remote-sync.sh",              # trace still executes
    "bash -e remote-sync.sh",
    "bash -n remote-sync.sh; bash remote-sync.sh",   # second segment executes
    "bash -n remote-sync.sh && bash remote-sync.sh",
    "sh -nc 'systemctl --user re" "start hermes-gateway.service'",  # -c payload is scanned regardless of -n
    "bash -c 'systemctl --user re" "start hermes-gateway.service'",
])
def test_every_executing_shape_still_blocks(command, script):
    assert _scan(command, script.parent) is True
