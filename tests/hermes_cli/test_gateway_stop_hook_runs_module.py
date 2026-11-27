"""The gateway unit's ExecStop hook runs its module from every launcher the unit may name (t_d9e5d1c2).

The generated unit runs ``<checkout>/.hermes/bin/hermes --run-module gateway.systemd_stop_mark``.
Hosts on the single-venv contract (ACE-AI) repoint every ``Exec*=`` at ``<checkout>/venv/bin/hermes``,
the plain ``hermes_cli.main:main`` console script, which did not know ``--run-module``: every stop
logged "'gateway.systemd_stop_mark' is not a `hermes` command", exited 2, and wrote no planned-stop
marker, so a planned ``systemctl restart`` ended "Failed with result 'exit-code'".

The hook is executed as written, against a fixture install, with the unit's environment
(HERMES_HOME, MAINPID). ExecStopPost (gateway.cgroup_cleanup) is only checked for shape: running it
here would SIGKILL every process in the test's own cgroup.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _exec_line(unit: str, key: str) -> list[str]:
    line = next(ln for ln in unit.splitlines() if ln.startswith(f"{key}="))
    return shlex.split(line[len(key) + 1:].lstrip("-"))


@pytest.fixture
def unit_text(monkeypatch, tmp_path):
    """The unit as `hermes gateway install` writes it for a PM-store checkout install."""
    from hermes_cli import _launchers
    from hermes_cli import gateway as gateway_cli

    monkeypatch.setattr(_launchers, "resolve_store_python", lambda root: tmp_path / "store" / "bin" / "python3")
    return gateway_cli.generate_systemd_unit(system=False)


def _venv_console_script(checkout: Path) -> Path:
    """What pip/uv write for ``hermes = hermes_cli.main:main`` in an editable checkout venv."""
    script = checkout / "venv" / "bin" / "hermes"
    script.parent.mkdir(parents=True)
    script.write_text(
        f"#!{sys.executable}\n"
        "import re, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})  # the editable install's .pth\n"
        "from hermes_cli.main import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_generated_hooks_use_the_run_module_verb(unit_text):
    stop = _exec_line(unit_text, "ExecStop")
    post = _exec_line(unit_text, "ExecStopPost")
    assert stop[0].endswith("/.hermes/bin/hermes") and stop[1:] == ["--run-module", "gateway.systemd_stop_mark"]
    assert post[0].endswith("/.hermes/bin/hermes") and post[1:] == ["--run-module", "gateway.cgroup_cleanup"]


def _store_launcher(checkout: Path) -> Path:
    """The `.hermes/bin/hermes` the unit names, minted the way install publishes it."""
    from hermes_cli import _launchers

    out = checkout / ".hermes" / "bin"
    out.mkdir(parents=True)
    path = _launchers.mint_launcher("hermes", REPO, out, Path(sys.executable), None)
    assert path is not None
    return path


@pytest.mark.parametrize("launcher", ["as_generated", "single_venv_repoint"])
def test_stop_hook_exits_0_and_marks_the_stop(unit_text, tmp_path, launcher):
    stop = _exec_line(unit_text, "ExecStop")
    # as_generated: the store launcher the unit names. single_venv_repoint: ACE-AI's `sed` of every
    # Exec*= onto <checkout>/venv/bin/hermes, same arguments.
    make = _store_launcher if launcher == "as_generated" else _venv_console_script
    argv = [str(make(tmp_path / "checkout")), *stop[1:]]
    home = tmp_path / "hermes-home"
    home.mkdir()
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(home),
               "MAINPID": str(target.pid), "HERMES_SUPERVISED_CHILD": "1"}
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not re.search(r"is not a `hermes` command", proc.stderr), proc.stderr
        marker = json.loads((home / ".gateway-planned-stop.json").read_text(encoding="utf-8"))
        assert marker["target_pid"] == target.pid
    finally:
        target.kill()
        target.wait()
