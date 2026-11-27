"""A signal sent to a gateway fork must not reach the gateway's loop (t_5e351ab7).

Each case runs in a fresh interpreter: an asyncio parent with ``add_signal_handler(SIGTERM)``
forks a sleeping child (no exec) and SIGTERMs the child only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork only")

REPO = Path(__file__).resolve().parents[2]

_HARNESS = textwrap.dedent(r"""
    import asyncio, json, os, signal, sys, time
    sys.path.insert(0, sys.argv[1])
    install = sys.argv[2] == "1"

    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        if install:
            from gateway.fork_signal_reset import install_fork_signal_reset
            assert install_fork_signal_reset()
        loop.add_signal_handler(signal.SIGTERM, lambda: fired.append(1))
        pid = os.fork()
        if pid == 0:
            time.sleep(5)
            os._exit(7)
        await asyncio.sleep(0.3)
        os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(0.8)
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done:  # child ignored SIGTERM
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            child = "survived"
        elif os.WIFSIGNALED(status):
            child = "signal:%d" % os.WTERMSIG(status)
        else:
            child = "exit:%d" % os.WEXITSTATUS(status)
        print(json.dumps({"parent_fired": bool(fired), "child": child}))

    asyncio.run(main())
""")


def _run(install: bool, env_extra=None) -> dict:
    env = {**os.environ, **(env_extra or {})}
    out = subprocess.run([sys.executable, "-c", _HARNESS, str(REPO), "1" if install else "0"],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_registration_keeps_child_sigterm_out_of_parent_loop():
    res = _run(install=True)
    assert res["parent_fired"] is False, res


def test_child_handles_its_own_sigterm_after_reset():
    res = _run(install=True)
    assert res["child"] == "signal:%d" % 15, res


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="CPython 3.12+ resets the wakeup fd after fork itself; the leak only exists < 3.12")
def test_without_registration_child_sigterm_fires_parent_handler():
    """The mutation: no registration reproduces the 2026-11-11 gateway shutdown."""
    res = _run(install=False)
    assert res["parent_fired"] is True, res


_WAKEUP_HARNESS = textwrap.dedent(r"""
    import asyncio, os, signal, sys
    sys.path.insert(0, sys.argv[1])
    install = sys.argv[2] == "1"

    async def main():
        loop = asyncio.get_running_loop()
        if install:
            from gateway.fork_signal_reset import install_fork_signal_reset
            assert install_fork_signal_reset()
        loop.add_signal_handler(signal.SIGTERM, lambda: None)
        pid = os.fork()
        if pid == 0:
            prev = signal.set_wakeup_fd(-1)  # what the child inherited after the at-fork hooks ran
            os._exit(0 if prev == -1 else 3)
        _, status = os.waitpid(pid, 0)
        print("child_exit=%d" % os.WEXITSTATUS(status))

    asyncio.run(main())
""")


def _child_wakeup_exit(install: bool) -> int:
    out = subprocess.run([sys.executable, "-c", _WAKEUP_HARNESS, str(REPO), "1" if install else "0"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1].split("=")[1])


def test_registration_clears_wakeup_fd_in_child():
    """Holds the ``set_wakeup_fd(-1)`` line alone: the child sees no inherited wakeup fd."""
    assert _child_wakeup_exit(install=True) == 0


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="CPython 3.12+ resets the wakeup fd after fork itself; the leak only exists < 3.12")
def test_without_registration_child_inherits_parent_wakeup_fd():
    assert _child_wakeup_exit(install=False) == 3


def test_start_gateway_installs_reset_before_add_signal_handler():
    """Wiring contract: start_gateway calls install_fork_signal_reset() before its first add_signal_handler."""
    import ast
    tree = ast.parse((REPO / "gateway" / "run.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_gateway")

    def call_lines(name):
        return sorted(c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and (
            (isinstance(c.func, ast.Name) and c.func.id == name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == name)))

    installs, adds = call_lines("install_fork_signal_reset"), call_lines("add_signal_handler")
    assert installs, "start_gateway no longer calls install_fork_signal_reset()"
    assert adds, "start_gateway no longer calls add_signal_handler"
    assert installs[0] < adds[0], (installs, adds)


def test_kill_switch_skips_registration(monkeypatch):
    from gateway import fork_signal_reset as mod
    monkeypatch.setattr(mod, "_registered", False)
    monkeypatch.setenv(mod.KILL_SWITCH_ENV, "0")
    calls = []
    monkeypatch.setattr(os, "register_at_fork", lambda **kw: calls.append(kw))
    assert mod.install_fork_signal_reset() is False
    assert calls == []


# A gateway that owns a host role: asyncio SIGTERM handler, then host_rendezvous.cleanup_on_exit()
# wraps it (start_gateway's order). argv[2] == "stub" disables the new after-fork step (mutation arm).
_RENDEZVOUS_HARNESS = textwrap.dedent(r"""
    import asyncio, fcntl, json, os, signal, sys, tempfile, time
    sys.path.insert(0, sys.argv[1])
    os.environ["HERMES_GATEWAY_LOCK_DIR"] = tempfile.mkdtemp()
    from gateway import host_rendezvous as hr
    from gateway.fork_signal_reset import install_fork_signal_reset
    if sys.argv[2] == "stub":
        hr.forget_owner_state_in_forked_child = lambda: None

    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        assert install_fork_signal_reset()
        loop.add_signal_handler(signal.SIGTERM, lambda: fired.append(1))
        assert hr.claim_host_lock(hr.ROLE_SERVE)[0] is hr.HostLockOutcome.ACQUIRED
        hr.publish_record(hr.ROLE_SERVE, host="127.0.0.1", port=9119, token="live-session-token")
        hr.cleanup_on_exit(hr.ROLE_SERVE)
        pid = os.fork()
        if pid == 0:
            time.sleep(5)
            sys.stdout.flush()
            os._exit(7)
        await asyncio.sleep(0.3)
        os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(0.8)
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            child = "survived"
        elif os.WIFSIGNALED(status):
            child = "signal:%d" % os.WTERMSIG(status)
        else:
            child = "exit:%d" % os.WEXITSTATUS(status)
        with open(hr.lock_path(hr.ROLE_SERVE), "a+") as fh:  # a fresh open contends with the parent's flock
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held = False
            except OSError:
                lock_held = True
        print(json.dumps({"parent_fired": bool(fired), "child": child, "lock_held": lock_held,
                          "record": hr.record_path(hr.ROLE_SERVE).exists(),
                          "token": hr.token_path(hr.ROLE_SERVE).exists(),
                          "wrapper": signal.getsignal(signal.SIGTERM) is hr._handle_terminating_signal}))

    asyncio.run(main())
""")


def _run_rendezvous(mode: str) -> dict:
    out = subprocess.run([sys.executable, "-c", _RENDEZVOUS_HARNESS, str(REPO), mode],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_rendezvous_owner_child_dies_of_sigterm_and_parent_keeps_its_role():
    """t_89c3a411: the cleanup wrapper no longer swallows the child's SIGTERM or drops the parent's lock."""
    res = _run_rendezvous("fix")
    assert res == {"parent_fired": False, "child": "signal:15", "lock_held": True,
                   "record": True, "token": True, "wrapper": True}, res


def test_without_rendezvous_step_child_survives_and_unlocks_parent():
    """The mutation: the wrapper chains to asyncio's no-op (child survives) and its cleanup's
    flock(LOCK_UN) on the inherited fd releases the LIVE parent's host lock. The record and token
    survive only because clear_record() refuses another pid's record."""
    res = _run_rendezvous("stub")
    assert res["child"] == "survived", res
    assert res["lock_held"] is False, res
    assert (res["record"], res["token"]) == (True, True), res


# The atexit path: cleanup_on_exit() also registers _cleanup_role with atexit, and a forked child that
# exits normally runs it. The loop holds the asyncio SIGTERM handler but is not running at fork, so the
# child's sys.exit(0) is a plain interpreter exit (unwinding a forked asyncio.run is version-dependent).
# argv[2] == "keepfds": the after-fork step without the lock-fd close / _lock_handles.clear() (mutation).
_RENDEZVOUS_ATEXIT_HARNESS = textwrap.dedent(r"""
    import asyncio, fcntl, json, os, signal, sys, tempfile, time
    sys.path.insert(0, sys.argv[1])
    os.environ["HERMES_GATEWAY_LOCK_DIR"] = tempfile.mkdtemp()
    from gateway import host_rendezvous as hr
    from gateway.fork_signal_reset import install_fork_signal_reset
    if sys.argv[2] == "keepfds":
        def _keep_fds():
            hr._cleanup_roles.clear()
            for signum, prev in tuple(hr._prev_signal_handlers.items()):
                if signal.getsignal(signum) is hr._handle_terminating_signal:
                    signal.signal(signum, signal.SIG_DFL if prev is None else prev)
            hr._prev_signal_handlers.clear()
        hr.forget_owner_state_in_forked_child = _keep_fds

    loop = asyncio.new_event_loop()
    assert install_fork_signal_reset()
    loop.add_signal_handler(signal.SIGTERM, lambda: None)
    assert hr.claim_host_lock(hr.ROLE_SERVE)[0] is hr.HostLockOutcome.ACQUIRED
    hr.publish_record(hr.ROLE_SERVE, host="127.0.0.1", port=9119, token="live-session-token")
    hr.cleanup_on_exit(hr.ROLE_SERVE)
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        sys.exit(0)  # normal interpreter exit: atexit runs whatever cleanup the child still holds
    deadline = time.monotonic() + 20
    done, status = os.waitpid(pid, os.WNOHANG)
    while not done and time.monotonic() < deadline:
        time.sleep(0.05)
        done, status = os.waitpid(pid, os.WNOHANG)
    if not done:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        child = "hung"
    elif os.WIFSIGNALED(status):
        child = "signal:%d" % os.WTERMSIG(status)
    else:
        child = "exit:%d" % os.WEXITSTATUS(status)
    with open(hr.lock_path(hr.ROLE_SERVE), "a+") as fh:  # a fresh open contends with the parent's flock
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_held = False
        except OSError:
            lock_held = True
    print(json.dumps({"child": child, "lock_held": lock_held,
                      "record": hr.record_path(hr.ROLE_SERVE).exists(),
                      "token": hr.token_path(hr.ROLE_SERVE).exists()}))
    sys.stdout.flush()
    os._exit(0)  # skip the parent's own atexit cleanup; the temp lock dir is throwaway
""")


def _run_rendezvous_atexit(mode: str) -> dict:
    out = subprocess.run([sys.executable, "-c", _RENDEZVOUS_ATEXIT_HARNESS, str(REPO), mode],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_rendezvous_owner_child_normal_exit_keeps_parent_lock():
    """A forked child that exits normally runs the atexit _cleanup_role it inherited; closing and
    forgetting its copies of the lock fds is what keeps that from unlocking the LIVE parent."""
    res = _run_rendezvous_atexit("fix")
    assert res == {"child": "exit:0", "lock_held": True, "record": True, "token": True}, res


def test_without_lock_fd_close_normal_child_exit_unlocks_parent():
    """The mutation: the after-fork step without the fd close / _lock_handles.clear()."""
    res = _run_rendezvous_atexit("keepfds")
    assert res["child"] == "exit:0", res
    assert res["lock_held"] is False, res
