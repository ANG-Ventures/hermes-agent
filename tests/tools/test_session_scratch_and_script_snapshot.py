"""Card t_f4377203: per-session scratch dir + background runs of a shared-/tmp script execute a
SNAPSHOT. A sibling session rewrote /tmp/act-1300.sh between Apollo's write and run (2026-09-24).
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
import uuid

import pytest

import hermes_constants as hc
from tools.script_snapshot import snapshot_tmp_script_command


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hh"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(hc, "get_default_hermes_root", lambda: h)
    monkeypatch.setattr(hc, "_session_scratch_pruned_once", True)
    return h


@pytest.fixture
def tmp_script():
    path = f"/tmp/land-{uuid.uuid4().hex[:10]}.sh"
    with open(path, "w") as fh:
        fh.write("echo landing\n")
    os.chmod(path, 0o755)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_session_scratch_dir_private_and_confined(home):
    d = hc.get_session_scratch_dir("20260927_134117_ac3d6a")
    assert d == home / "var" / "scratch" / "20260927_134117_ac3d6a"
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    escaped = hc.get_session_scratch_dir("../../etc")
    assert escaped.parent == home / "var" / "scratch"
    assert hc.get_session_scratch_dir("") is None


def test_apply_session_scratch_env_follows_the_bridged_session(home):
    env = {"HERMES_SESSION_ID": "sess-a"}
    assert hc.apply_session_scratch_env(env) is True
    assert env[hc.SESSION_SCRATCH_ENV] == str(home / "var" / "scratch" / "sess-a")
    # no session: a stale value from the parent is removed, never inherited
    env = {hc.SESSION_SCRATCH_ENV: "/elsewhere/sess-b"}
    assert hc.apply_session_scratch_env(env) is False
    assert hc.SESSION_SCRATCH_ENV not in env


def test_terminal_child_env_carries_session_scratch(home, monkeypatch):
    from tools.environments import local
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-env")
    env = local._make_run_env({})
    assert env.get(hc.SESSION_SCRATCH_ENV) == str(home / "var" / "scratch" / "sess-env")


def test_prune_reaps_idle_session_dirs_only(home):
    root = home / "var" / "scratch"
    old = root / "old-sess"
    live = root / "live-sess"
    (old / "deep").mkdir(parents=True)
    (live / "deep").mkdir(parents=True)
    (old / "deep" / "f").write_text("x")
    (live / "deep" / "f").write_text("x")
    past = time.time() - 8 * 24 * 3600
    for p in (old / "deep" / "f", old / "deep", old, live, live / "deep"):
        os.utime(p, (past, past))
    # live-sess has an old dir mtime but a fresh deep write: it must survive
    assert hc.prune_session_scratch(root) == 1
    assert not old.exists() and live.exists()


def test_snapshot_rewrites_and_isolates_from_later_clobber(home, tmp_script):
    cmd, info = snapshot_tmp_script_command(f"bash {tmp_script} --flag", "sess-snap")
    assert info and info["source"] == tmp_script
    snap = info["snapshot"]
    assert snap.startswith(str(home / "var" / "scratch" / "sess-snap" / "snapshots"))
    assert cmd == f"bash {snap} --flag"
    with open(tmp_script, "w") as fh:  # the sibling's clobber, after spawn
        fh.write("echo CLOBBERED\n")
    out = subprocess.run(["bash", "-c", cmd.replace(" --flag", "")], capture_output=True, text=True).stdout
    assert out.strip() == "landing"


@pytest.mark.parametrize("shape", [
    "{p}", "sh -x {p}", "python3 {p} a b", "/bin/bash {p}",
])
def test_snapshot_shapes(home, tmp_script, shape):
    cmd, info = snapshot_tmp_script_command(shape.format(p=tmp_script), "s")
    assert info is not None, shape
    assert tmp_script not in cmd


@pytest.mark.parametrize("shape", [
    "bash {p}; rm -rf x", "bash {p} && echo ok", "bash {p} | tee log", "cat {p}",
    "bash /tmp/sub/{b}", "bash ./{b}", "bash {p} > out.log",
])
def test_snapshot_leaves_other_commands_alone(home, tmp_script, shape):
    command = shape.format(p=tmp_script, b=os.path.basename(tmp_script))
    assert snapshot_tmp_script_command(command, "s") == (command, None)


def test_snapshot_needs_a_session_and_an_existing_file(home, tmp_script):
    assert snapshot_tmp_script_command(f"bash {tmp_script}", "") == (f"bash {tmp_script}", None)
    missing = "/tmp/does-not-exist-%s.sh" % uuid.uuid4().hex
    assert snapshot_tmp_script_command(f"bash {missing}", "s") == (f"bash {missing}", None)
