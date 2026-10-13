"""N placed workers to ONE pool host in one tick (t_3d487402).

Six workers placed on ci-box in the same tick shared one ControlMaster socket;
the one that lost the creation race printed ``ControlSocket <path> already
exists, disabling multiplexing`` ahead of its real output, and that line rode
into the card's handback. Contract: each placed run gets its own control
socket, and ssh's own diagnostics land in the run log, never in a tool result.

The ``ssh`` on PATH is a stand-in that keeps ControlMaster's ownership rule:
the first worker to reach a ControlPath owns it, and any OTHER worker reaching
the same path gets ssh's real collision line on stderr (or in the ``-E`` file).
It runs the remote command locally. Each worker is a separate process driving
the real chain: ``apply_placement`` -> terminal env config -> ``SSHEnvironment``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
N_WORKERS = 6
COLLISION = "already exists, disabling multiplexing"

FAKE_SSH = textwrap.dedent(r'''
    #!{python}
    import os, subprocess, sys
    args = sys.argv[1:]
    opts, log_file, rest = {{}}, None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "-p", "-i", "-E", "-O"):
            v = args[i + 1]
            if a == "-o":
                k, _, val = v.partition("=")
                opts[k] = val
            elif a == "-E":
                log_file = v
            elif a == "-O":
                sys.exit(0)
            i += 2
            continue
        rest = args[i + 1:]  # args[i] is user@host
        break
    path = opts.get("ControlPath")
    me = os.environ["FAKE_WORKER_ID"]
    diag = []
    if path and opts.get("ControlMaster") == "auto":
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, me.encode()); os.close(fd)
        except FileExistsError:
            if open(path).read() != me:
                diag.append(f"ControlSocket {{path}} already exists, disabling multiplexing")
    for line in diag:
        if log_file:
            with open(log_file, "a") as f:
                f.write(line + "\r\n")
        else:
            sys.stderr.write(line + "\r\n")
    sys.stderr.flush()
    # Like sshd: the argv is joined and handed to the remote user's shell.
    sys.exit(subprocess.call(["bash", "-c", " ".join(rest)]))
''').lstrip()

WORKER = textwrap.dedent(r'''
    import json, os, sys
    from hermes_cli import kanban_worker_hosts as kwh
    from hermes_cli import kanban_worker_pool as kwp
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _create_environment, _ssh_config_from_config

    host = kwp.PoolHost(name="ci-box", ssh_host="ci-box", ssh_user="kanbanw", slots=6,
                        capacity_pct=0.8, absence="required", profiles=("daedalus",),
                        state="active", enabled=True, priority=0)
    run_id = int(os.environ["FAKE_RUN_ID"])
    if os.environ.get("FAKE_LEGACY") == "1":
        kwh.apply_placement(os.environ, host, os.environ["FAKE_WS"])
    else:
        kwh.apply_placement(os.environ, host, os.environ["FAKE_WS"], run_id=run_id,
                            log_file=os.environ["FAKE_LOG"])
    cfg = _get_env_config()
    env = _create_environment("ssh", "", cfg["cwd"], 30, ssh_config=_ssh_config_from_config(cfg))
    if os.environ.get("FAKE_PRESEED") == "1":
        env.control_socket.write_text("stale")  # a socket some other process owns
    out = env.execute(f"echo RESULT-{run_id}")
    print(json.dumps({"socket": str(env.control_socket), "output": out["output"],
                      "rc": out["returncode"]}))
''')


def _spawn_workers(tmp_path: Path, *, legacy: bool, preseed: bool = False,
                   n: int = N_WORKERS) -> list[dict]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH.format(python=sys.executable))
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    scp = bindir / "scp"
    scp.write_text("#!/bin/sh\nexit 0\n")
    scp.chmod(0o755)
    tmpdir = tmp_path / "t"  # short: the control socket path has a 104-byte cap
    tmpdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    def one(i: int) -> dict:
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TERMINAL_", "HERMES_"))}
        env.update({
            "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
            "TMPDIR": str(tmpdir), "HERMES_HOME": str(home), "PYTHONPATH": str(REPO),
            "FAKE_WORKER_ID": str(i), "FAKE_RUN_ID": str(1000 + i), "FAKE_WS": str(ws),
            "FAKE_LOG": str(tmp_path / f"run{i}.log"), "FAKE_LEGACY": "1" if legacy else "0",
            "FAKE_PRESEED": "1" if preseed else "0",
        })
        proc = subprocess.run([sys.executable, "-c", WORKER], env=env, cwd=str(REPO),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        rec = json.loads(proc.stdout.strip().splitlines()[-1])
        rec["log"] = (tmp_path / f"run{i}.log").read_text() if (tmp_path / f"run{i}.log").exists() else ""
        rec["run_id"] = 1000 + i
        return rec

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(one, range(n)))


@pytest.mark.platforms("posix")
def test_six_workers_to_one_host_get_distinct_sockets_and_clean_results(tmp_path):
    results = _spawn_workers(tmp_path, legacy=False)
    sockets = {r["socket"] for r in results}
    assert len(sockets) == N_WORKERS, sockets
    for r in results:
        assert r["rc"] == 0, r
        assert r["output"].strip() == f"RESULT-{r['run_id']}", r["output"]
        assert COLLISION not in r["output"]
        assert COLLISION not in r["log"]


@pytest.mark.platforms("posix")
def test_harness_reproduces_the_shared_socket_leak(tmp_path):
    """The stand-in is honest: without a run scope every worker shares one socket and
    the losers' results start with the collision line (the t_55256078 burst)."""
    results = _spawn_workers(tmp_path, legacy=True)
    assert len({r["socket"] for r in results}) == 1
    leaked = [r for r in results if COLLISION in r["output"]]
    assert leaked, [r["output"] for r in results]
    assert all(r["output"].startswith("ControlSocket ") for r in leaked)


@pytest.mark.platforms("posix")
def test_ssh_diagnostics_go_to_the_run_log_not_the_result(tmp_path):
    """Any ssh-side diagnostic (here: a stale socket at the run's own path) lands in the
    run log; the tool result carries only the remote command's output."""
    [r] = _spawn_workers(tmp_path, legacy=False, preseed=True, n=1)
    assert r["output"].strip() == f"RESULT-{r['run_id']}", r["output"]
    assert COLLISION in r["log"]
