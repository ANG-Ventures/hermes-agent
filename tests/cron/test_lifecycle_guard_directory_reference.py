"""Regression: a referenced path that is a DIRECTORY must not fail the guard closed.

pc-fb1bd018: `./scripts/acceptance.sh` was hard-blocked inside the gateway because
the script's env-default line names a directory path
(REOLINK_DIR="$HOME/.hermes/skills-shared/smart-home/reolink-audio"); the
referenced-script walk opened it, saw a non-regular file, and returned
unsafe=True. A directory is not executable script content — only
FIFOs/devices/sockets warrant the fail-closed verdict.
"""

import os
import stat
from pathlib import Path

import pytest

from cron.lifecycle_guard import (
    _read_referenced_script,
    contains_gateway_lifecycle_command_or_referenced_script,
)


def test_directory_reference_is_not_unsafe(tmp_path):
    d = tmp_path / "some-skill-dir"
    d.mkdir()
    text, unsafe = _read_referenced_script(d)
    assert text is None
    assert unsafe is False


def test_fifo_reference_stays_fail_closed(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    text, unsafe = _read_referenced_script(fifo)
    assert text is None
    assert unsafe is True


def test_script_naming_a_directory_passes_full_walk(tmp_path):
    """The end-to-end shape that bit: an innocent script whose body names a dir."""
    skill_dir = tmp_path / "skills" / "reolink-audio"
    skill_dir.mkdir(parents=True)
    script = tmp_path / "acceptance.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'REOLINK_DIR="${{REOLINK_DIR:-{skill_dir}}}"\n'
        'echo "running acceptance against $REOLINK_DIR"\n'
    )
    script.chmod(0o755)
    assert not contains_gateway_lifecycle_command_or_referenced_script(
        f"{script} --room theater", cwd=str(tmp_path)
    )


def test_script_naming_a_directory_but_containing_lifecycle_still_blocked(tmp_path):
    """Mutation leg: the directory exemption must not mask a REAL lifecycle line."""
    skill_dir = tmp_path / "skills" / "reolink-audio"
    skill_dir.mkdir(parents=True)
    script = tmp_path / "bad.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'REOLINK_DIR="{skill_dir}"\n'
        "hermes gateway restart\n"
    )
    script.chmod(0o755)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"{script}", cwd=str(tmp_path)
    )
