"""Parity oracle: the no-fork spawn must hand bash exactly what plain Popen hands it (t_afc525a2).

The same command runs through ``LocalEnvironment.execute`` twice, once on the no-fork
path (forced on, as on macOS) and once on plain ``Popen(cwd=, start_new_session=)``
(main's path), under a matrix of hostile environments, one at a time and all together.
Everything the command can observe is compared: the sorted ``env -0`` bytes, ``set -o``,
``shopt``, ``pwd -P``, ``$-``, ``$0``, startup-file output and the exit status. Any
difference is a bug: an earlier shell trampoline lost CDPATH, exported functions and
SHELLOPTS/BASHOPTS this way.
"""

from __future__ import annotations

import os
import sys

import pytest

import tools.spawn_nofork as nofork
from tools.environments.local import LocalEnvironment

pytestmark = pytest.mark.platforms("posix")


def _hostile(tmp_path):
    marker_file = tmp_path / "startup.sh"
    marker_file.write_text("echo STARTUP-FILE-RAN\n")
    other = tmp_path / "cdpath" / "work"
    other.mkdir(parents=True)
    previous = tmp_path / "previous"
    previous.mkdir()
    return {
        "shellopts-pipefail": {"SHELLOPTS": "pipefail"},
        "shellopts-errexit-pipefail": {"SHELLOPTS": "errexit:pipefail"},
        "bashopts-nullglob": {"BASHOPTS": "nullglob"},
        "oldpwd": {"OLDPWD": str(previous)},
        "pwd": {"PWD": str(previous)},
        "cdpath": {"CDPATH": str(tmp_path / "cdpath")},
        "bash-env": {"BASH_ENV": str(marker_file)},
        "env": {"ENV": str(marker_file)},
        "exported-function": {"BASH_FUNC_x%%": "() { echo function-x; }",
                              "BASH_FUNC_cd%%": "() { return 0; }"},
        "globignore": {"GLOBIGNORE": "*"},
        "posixly-correct": {"POSIXLY_CORRECT": "1"},
        "ifs": {"IFS": ":"},
        "ps4": {"PS4": "+trace+ "},
        "empty-value": {"T_AFC525A2_EMPTY": ""},
        "non-utf8-value": {"T_AFC525A2_BYTES": os.fsdecode(b"caf\xe9\xff")},
        "lc-ctype-c": {"LC_CTYPE": "C"},
        "python-vars": {"PYTHONPATH": "/nonexistent", "PYTHONSTARTUP": str(marker_file)},
    }


_MATRIX = ["shellopts-pipefail", "shellopts-errexit-pipefail", "bashopts-nullglob", "oldpwd", "pwd",
           "cdpath", "bash-env", "env", "exported-function", "globignore", "posixly-correct", "ifs",
           "ps4", "empty-value", "non-utf8-value", "lc-ctype-c", "python-vars", "all"]


NATIVE_SPAWNED: dict = {}  # tag -> did a native posix_spawn return 0 in the last _observe


def _observe(monkeypatch, tmp_path, extra_env: dict, nofork_on: bool, tag: str) -> dict:
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", nofork_on)
    spawns = []  # return codes of native libc posix_spawn calls
    real_spawn = nofork._native_spawn

    def counted(*args, **kwargs):
        rc, pid = real_spawn(*args, **kwargs)
        spawns.append(rc)
        return rc, pid

    monkeypatch.setattr(nofork, "_native_spawn", counted)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    out = tmp_path / f"out-{tag}"
    out.mkdir()
    environment = LocalEnvironment(cwd=str(work), timeout=30)
    try:
        environment.env.update(extra_env)
        spawns.clear()
        result = environment.execute(
            f"o='{out}'; env -0 | LC_ALL=C sort -z > \"$o/env\"; set -o > \"$o/set-o\"; "
            f"shopt > \"$o/shopt\"; pwd -P > \"$o/pwd\"; printf '%s\\n%s\\n' \"$-\" \"$0\" > \"$o/flags\"; "
            "cd - > \"$o/cd-dash\" 2>&1; x > \"$o/function\" 2>&1; false | true")
    finally:
        environment.cleanup()
    seen = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
    seen["returncode"] = result["returncode"]
    seen["output"] = result["output"]
    # Which path really started the probe: a native spawn that RETURNED 0, or plain Popen.
    NATIVE_SPAWNED[tag] = 0 in spawns
    if not nofork_on:
        assert not spawns, spawns
    return seen


@pytest.mark.parametrize("case", _MATRIX)
def test_nofork_spawn_matches_plain_popen(monkeypatch, tmp_path, case):
    matrix = _hostile(tmp_path)
    extra = {k: v for env in matrix.values() for k, v in env.items()} if case == "all" else matrix[case]
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    plain = _observe(monkeypatch, tmp_path, extra, nofork_on=False, tag="plain")
    spawned = _observe(monkeypatch, tmp_path, extra, nofork_on=True, tag="nofork")
    if not NATIVE_SPAWNED["nofork"]:
        # Off macOS there is no native binding: both arms were plain Popen, so a comparison
        # would prove nothing. On macOS the native path must have run.
        assert sys.platform != "darwin", "the no-fork arm did not start the probe natively on macOS"
        pytest.skip(f"no native posix_spawn on {sys.platform}: both arms are plain Popen")
    differences = {k: (plain.get(k), spawned.get(k)) for k in plain.keys() | spawned.keys()
                   if plain.get(k) != spawned.get(k)}
    assert not differences, differences
