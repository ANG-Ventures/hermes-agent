"""A gateway test rig leaves no gateway behind when its pytest dies (card t_c791d60f).

The e2e rigs start ``hermes gateway run`` with ``start_new_session=True`` and end it only in
fixture teardown. That isolation keeps a group kill aimed at pytest away from the gateway, so a
pytest SIGKILLed or SIGTERMed mid-test (killed worker, OOM, ``timeout``, a runtime cap) left it at
ppid 1 for good. hermes-home measured four such gateways days old on the Studio (t_410c3080).

Each case runs a CHILD pytest that starts one real rig spawn site (unchanged helper code), ends that
child the way the field ended it, and asserts that within 30 s no process anywhere still names the
child's pytest temp dir (gateway, lookalike, watcher: env, argv or cwd).

  * kanban ``gateway()`` context manager: a real ``hermes gateway run`` (kanban-only, no platforms)
  * platforms ``GatewayUnderTest``: a real ``hermes gateway run``
  * the hermes_cli ``gateway run`` lookalike (was not detached; now its own session + watch)
  * a group the rig never stopped (boot failure before a fixture's yield): reaped at pytest exit

Each test kills whatever it finds on the way out, so a red run does not itself leak.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SURVIVE_S = 30          # the card's acceptance bound
READY_S = 240           # child rig boot under CI load

pytestmark = [
    pytest.mark.platforms("posix"),
    # Cleanup SIGKILLs leftovers that have been reparented to pid 1 (outside this test's subtree),
    # matched only by this test's own temp dir.
    pytest.mark.live_system_guard_bypass,
]

_CHILD = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    PIDS = {pids!r}

    def _ready(pid):
        Path(PIDS).write_text(json.dumps({{"pid": pid}}))
        time.sleep(900)

    def test_kanban_gateway(tmp_path):
        from tests.e2e.core.kanban import test_kanban_decompose_billing as K
        from tests.e2e.core.kanban._helpers import Board
        from tests.fakes.fake_llm_provider import FakeLLMServer
        with FakeLLMServer() as srv:
            b = Board(tmp_path, srv.base_url, extra_config=K.GATEWAY_CONFIG)
            with K.gateway(b) as proc:
                K.wait_until(lambda: proc.poll() is not None or (b.hermes_home / "gateway.pid").exists(),
                             120, "gateway pid file")
                assert proc.poll() is None, K.gateway_tail(b)
                _ready(proc.pid)

    def test_platforms_gateway(tmp_path):
        from tests.e2e.core.platforms._helpers import GatewayUnderTest
        from tests.fakes.fake_llm_provider import FakeLLMServer
        with FakeLLMServer() as srv:
            gw = GatewayUnderTest(tmp_path / "rig", llm_base_url=srv.base_url, config={{}}, env={{}},
                                  ready=lambda: True)
            try:
                gw.start()
                _ready(gw.proc.pid)
            finally:
                gw.stop()

    def test_cli_lookalike(tmp_path):
        from tests.hermes_cli.test_cross_profile_kill_refusal import _spawn_gateway_lookalike
        proc = _spawn_gateway_lookalike(tmp_path / "bin", tmp_path / "home" / "gateway.lock")
        try:
            _ready(proc.pid)
        finally:
            proc.kill()

    def test_unstopped_group_reaped_at_exit(tmp_path):
        # A rig whose boot raised before the fixture's yield: tracked, never stopped, pytest exits.
        import subprocess
        from tests import rig_parent_watch
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)", str(tmp_path)],
                                start_new_session=True)
        rig_parent_watch.track(proc.pid)
        Path(PIDS).write_text(json.dumps({{"pid": proc.pid}}))
        raise AssertionError("gateway did not become ready")
''')


def _procs_referencing(marker: str) -> dict[int, str]:
    """{pid: argv head} of every live process of this uid whose env, argv or cwd names ``marker``."""
    me, out = os.getpid(), {}
    if sys.platform.startswith("linux"):
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == me:
                continue
            try:
                cmd = Path(f"/proc/{d}/cmdline").read_bytes()
                env = Path(f"/proc/{d}/environ").read_bytes()
                cwd = os.readlink(f"/proc/{d}/cwd")
            except OSError:
                continue
            if not cmd:                                  # zombie / kernel thread
                continue
            if marker.encode() in cmd or marker.encode() in env or marker in cwd:
                out[int(d)] = cmd.replace(b"\0", b" ").decode(errors="replace")[:160]
        return out
    r = subprocess.run(["ps", "-E", "-ww", "-U", str(os.getuid()), "-o", "pid=,state=,command="],
                       capture_output=True, text=True, timeout=60)
    for line in r.stdout.splitlines():
        f = line.split(None, 2)
        if len(f) == 3 and f[0].isdigit() and int(f[0]) != me and not f[1].startswith("Z") and marker in f[2]:
            out[int(f[0])] = f[2][:160]
    return out


def _kill_all(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_gone(marker: str, timeout: float) -> dict[int, str]:
    end = time.time() + timeout
    left = _procs_referencing(marker)
    while left and time.time() < end:
        time.sleep(1)
        left = _procs_referencing(marker)
    return left


def _child(tmp_path: Path, name: str):
    bt = tmp_path / "child-basetemp"
    pids = tmp_path / "rig.pids"
    mod = tmp_path / "child_rig_kill.py"
    mod.write_text(_CHILD.format(repo=str(REPO), pids=str(pids)))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), env.get("PYTHONPATH", "")) if p)
    env["HERMES_TEST_RIG_WATCH_LOG"] = str(bt / "rig-watch.log")   # the watcher names bt, so it counts too
    env["HERMES_ALLOW_SHADOW_CWD"] = "1"
    log = open(tmp_path / "child.out", "w")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(mod), "-q", "-p", "no:cacheprovider", "--no-header",
         "--basetemp", str(bt), "--rootdir", str(tmp_path), "-k", name],
        cwd=str(tmp_path), env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)       # so ending it never touches THIS pytest's group
    return proc, bt, pids, log


def _out(tmp_path: Path) -> str:
    return (tmp_path / "child.out").read_text(errors="replace")[-3000:]


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM], ids=["SIGKILL", "SIGTERM"])
@pytest.mark.parametrize("rig", ["kanban_gateway", "platforms_gateway", "cli_lookalike"])
def test_pytest_killed_mid_test_leaves_no_gateway_after_30s(tmp_path, rig, sig):
    proc, bt, pids, log = _child(tmp_path, f"test_{rig}")
    try:
        end = time.time() + READY_S
        while not (pids.is_file() and pids.read_text().strip()) and time.time() < end:
            assert proc.poll() is None, _out(tmp_path)
            time.sleep(0.5)
        assert pids.is_file(), "child rig never became ready:\n" + _out(tmp_path)
        spawned = json.loads(pids.read_text())["pid"]
        assert spawned in _procs_referencing(str(bt)), "precondition: the rig's process is visible"
        os.kill(proc.pid, sig)                    # the pytest process only, as a dead worker leaves it
        proc.wait(30)
        left = _wait_gone(str(bt), SURVIVE_S)
        assert not left, (f"{len(left)} rig process(es) outlived their pytest by {SURVIVE_S}s "
                          f"(rig pid {spawned}): {left}")
    finally:
        if proc.poll() is None:
            proc.kill()
        _kill_all(_procs_referencing(str(bt)))
        log.close()


def test_group_never_stopped_is_reaped_when_pytest_exits(tmp_path):
    proc, bt, pids, log = _child(tmp_path, "test_unstopped_group_reaped_at_exit")
    try:
        rc = proc.wait(READY_S)
        assert rc == 1 and "gateway did not become ready" in _out(tmp_path), _out(tmp_path)
        spawned = json.loads(pids.read_text())["pid"]
        left = _wait_gone(str(bt), SURVIVE_S)
        assert not left, f"{len(left)} process(es) outlived a failed rig boot by {SURVIVE_S}s (rig pid {spawned}): {left}"
    finally:
        if proc.poll() is None:
            proc.kill()
        _kill_all(_procs_referencing(str(bt)))
        log.close()
