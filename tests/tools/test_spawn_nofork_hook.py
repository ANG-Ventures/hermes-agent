"""The gateway's process-wide no-fork hook keeps every subprocess call shape identical (t_aa2e2d4e).

``tools.spawn_nofork.install_process_hook`` replaces ``subprocess.Popen._execute_child`` so a
launch from anywhere in the gateway process starts with one ``posix_spawn`` on macOS. Every
test runs the SAME call twice, once with plain CPython (hook off) and once with the hook on,
and compares everything the caller can observe: return value, return code, captured
stdout/stderr, raised exception type / errno / filename / attributes. These are the shapes the
gateway's forking callers use (kanban_pr_gate ``gh``/``git`` reads, kanban_owner_wake, gate-tick
``ssh`` probes, cron script runs, blackbox alerts): bare names resolved on PATH,
``capture_output`` + ``text`` + ``timeout``, ``check=True``, an exact ``env``, ``cwd``, timeout
expiry, ``start_new_session``, ``process_group=0``, ``pass_fds``, ``shell=True``.

On macOS the hook arm must ALSO prove it took the native path: ``fork_exec`` raises there and at
least one ``posix_spawn`` must have returned 0, so no assertion passes vacuously. Launches the hook
cannot express (``preexec_fn``, a relative path, ``HERMES_SPAWN_LEGACY=1``, ``close_fds=False``)
must reach ``fork_exec`` instead. Off macOS the hook is never installed (fork_exec uses vfork
there), so the hook arm IS plain Popen and the comparison proves the fallback.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

import tools.spawn_nofork as nofork

pytestmark = pytest.mark.platforms("posix")

NATIVE_HOST = sys.platform == "darwin"


class Spawns:
    def __init__(self):
        self.native_ok = 0
        self.forks = 0


@pytest.fixture
def spawns(monkeypatch):
    """Counts native posix_spawn successes and fork_exec calls; uninstalls the hook afterwards."""
    import _posixsubprocess

    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    counts = Spawns()
    real_native = nofork._native_spawn

    def native(*args, **kwargs):
        rc, pid = real_native(*args, **kwargs)
        counts.native_ok += rc == 0
        return rc, pid

    monkeypatch.setattr(nofork, "_native_spawn", native)
    owner = subprocess if hasattr(subprocess, "_fork_exec") else _posixsubprocess  # 3.12+ binds it
    name = "_fork_exec" if owner is subprocess else "fork_exec"
    real_fork = getattr(owner, name)

    def fork(*args, **kwargs):
        counts.forks += 1
        return real_fork(*args, **kwargs)

    monkeypatch.setattr(owner, name, fork)
    yield counts
    nofork.uninstall_process_hook()


def _observe(fn):
    """Everything a caller sees from one call: the result, or the exception's identity."""
    try:
        value = fn()
    except subprocess.TimeoutExpired as exc:
        return {"raised": "TimeoutExpired", "timeout": exc.timeout, "cmd": exc.cmd}
    except subprocess.CalledProcessError as exc:
        return {"raised": "CalledProcessError", "rc": exc.returncode, "cmd": exc.cmd,
                "out": exc.stdout, "err": exc.stderr}
    except OSError as exc:
        return {"raised": type(exc).__name__, "errno": exc.errno, "filename": exc.filename}
    if isinstance(value, subprocess.CompletedProcess):
        return {"rc": value.returncode, "out": value.stdout, "err": value.stderr, "args": value.args}
    return {"value": value}


def _pair(spawns, fn, *, native_expected=True):
    """(plain, hooked) observations of ``fn``; on macOS assert which path the hooked arm took.

    ``native_expected``: True = a native posix_spawn succeeded and nothing forked; False = it fell
    back to fork_exec; None = neither (a launch error the hook raises itself, as Popen would)."""
    nofork.uninstall_process_hook()
    plain = _observe(fn)
    installed = nofork.install_process_hook()
    assert installed is NATIVE_HOST, "the hook installs on macOS only"
    before_native, before_forks = spawns.native_ok, spawns.forks
    try:
        hooked = _observe(fn)
    finally:
        nofork.uninstall_process_hook()
    if NATIVE_HOST and native_expected is not None:
        if native_expected:
            assert spawns.native_ok > before_native, "hooked arm never returned from a native posix_spawn"
            assert spawns.forks == before_forks, "hooked arm forked the parent"
        else:
            assert spawns.forks > before_forks, "an ineligible launch must keep CPython's fork_exec"
    return plain, hooked


# ------------------------------------------------------------------ the callers' call shapes

def test_capture_output_text_timeout_on_path_name(spawns):
    """kanban_owner_wake._gh_json / kanban_worker_pool._probe_once: a bare name, captured as text."""
    def call():
        return subprocess.run(["sh", "-c", "printf 'out\\n'; printf 'err\\n' >&2; exit 3"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=10, stdin=subprocess.DEVNULL, check=False)
    plain, hooked = _pair(spawns, call)
    assert plain == {"rc": 3, "out": "out\n", "err": "err\n", "args": plain["args"]}
    assert hooked == plain


def test_stdout_pipe_stderr_devnull(spawns):
    """kanban_pr_gate.query_pr / _git: stdout=PIPE, stderr=DEVNULL, text, timeout."""
    def call():
        return subprocess.run(["sh", "-c", "echo merged; echo noise >&2"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                              errors="replace", timeout=10, check=False)
    plain, hooked = _pair(spawns, call)
    assert plain["out"] == "merged\n" and plain["err"] is None
    assert hooked == plain


def test_check_true_raises_called_process_error(spawns):
    def call():
        return subprocess.run(["sh", "-c", "echo partial; echo why >&2; exit 4"],
                              capture_output=True, text=True, check=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert plain["raised"] == "CalledProcessError" and plain["rc"] == 4
    assert hooked == plain


def test_check_output_and_call(spawns):
    plain, hooked = _pair(spawns, lambda: subprocess.check_output(["echo", "x"]))
    assert plain == {"value": b"x\n"} and hooked == plain
    plain, hooked = _pair(spawns, lambda: subprocess.call(["sh", "-c", "exit 9"]))
    assert plain == {"value": 9} and hooked == plain
    plain, hooked = _pair(spawns, lambda: subprocess.check_output(["sh", "-c", "exit 2"]))
    assert plain["raised"] == "CalledProcessError" and hooked == plain


def test_env_passed_through_exactly(spawns):
    env = {"PATH": "/usr/bin:/bin", "T_AA2E2D4E": "va lue=with=equals", "EMPTY": "",
           "NON_UTF8": os.fsdecode(b"caf\xe9\xff")}

    def call():
        return subprocess.run(["/usr/bin/env", "-0"], env=env, capture_output=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert sorted(plain["out"].split(b"\0")[:-1]) == sorted(
        os.fsencode(k) + b"=" + os.fsencode(v) for k, v in env.items())
    assert hooked == plain


def test_env_none_is_the_live_os_environ(spawns, monkeypatch):
    """A variable set in os.environ after import reaches an env=None child on both paths."""
    monkeypatch.setenv("T_AA2E2D4E_LIVE", "set-at-runtime")
    monkeypatch.delenv("T_AA2E2D4E_GONE", raising=False)

    def call():
        return subprocess.run(["/usr/bin/env", "-0"], capture_output=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert b"T_AA2E2D4E_LIVE=set-at-runtime" in plain["out"].split(b"\0")
    assert sorted(hooked["out"].split(b"\0")) == sorted(plain["out"].split(b"\0"))


def test_env_path_decides_the_bare_name(spawns, tmp_path):
    """Popen searches the CHILD env's PATH (os.get_exec_path(env)); so must the hook."""
    first, second = tmp_path / "a", tmp_path / "b"
    for d, word in ((first, "first"), (second, "second")):
        d.mkdir()
        (d / "t-aa2-tool").write_text(f"#!/bin/sh\necho {word}\n")
        (d / "t-aa2-tool").chmod(0o755)
    (first / "t-aa2-tool").chmod(0o644)  # not executable: EACCES, the search goes on to b/

    def call():
        return subprocess.run(["t-aa2-tool"], env={"PATH": f"{first}:{second}"},
                              capture_output=True, text=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert plain["out"] == "second\n" and hooked == plain


def test_cwd(spawns, tmp_path):
    real = tmp_path / "real dir"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    def call():
        return subprocess.run(["/bin/sh", "-c", "pwd -P; pwd"], cwd=str(link), capture_output=True,
                              text=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert plain["out"].splitlines()[0] == str(real.resolve())
    assert hooked == plain


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return reaped == 0


def test_timeout_kills_the_child_and_raises(spawns, tmp_path):
    pids = []

    def call():
        marker = tmp_path / f"pid-{len(pids)}"
        pids.append(marker)
        return subprocess.run(["/bin/sh", "-c", f'echo $$ > "{marker}"; exec sleep 30'],
                              capture_output=True, text=True, timeout=0.5)
    started = time.monotonic()
    plain, hooked = _pair(spawns, call)
    assert time.monotonic() - started < 20
    assert plain["raised"] == "TimeoutExpired" and plain["timeout"] == 0.5
    assert (hooked["raised"], hooked["timeout"]) == (plain["raised"], plain["timeout"])
    for marker in pids:
        pid = int(marker.read_text())
        assert not _alive(pid), f"child {pid} survived its timeout"


def test_popen_streams_and_communicate_input(spawns):
    payload = os.urandom(200_000)

    def call():
        with subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
            out, _ = proc.communicate(payload, timeout=10)
        return (proc.returncode, out)
    plain, hooked = _pair(spawns, call)
    assert plain == {"value": (0, payload)} and hooked == plain
    text_in = subprocess.run  # input= on run, the blackbox / cron stdin shape
    plain, hooked = _pair(spawns, lambda: text_in(["cat"], input="héllo", capture_output=True,
                                                  text=True, timeout=10))
    assert plain["out"] == "héllo" and hooked == plain


def test_stderr_to_stdout_and_devnull_stdin(spawns):
    def call():
        return subprocess.run(["sh", "-c", "echo a; echo b >&2; cat"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert sorted(plain["out"].split()) == [b"a", b"b"] and plain["rc"] == 0
    assert sorted(hooked["out"].split()) == sorted(plain["out"].split()) and hooked["rc"] == 0


def test_start_new_session_and_process_group(spawns):
    """cron scripts run in their own session; shell hooks / git probes in their own group."""
    report = "echo $$ $(ps -o pgid= -p $$) $(ps -o sess= -p $$ 2>/dev/null || echo -)"

    def shape(**kw):
        def call():
            out = subprocess.run(["/bin/sh", "-c", report], capture_output=True, text=True,
                                 timeout=10, **kw).stdout.split()
            pid, pgid = out[0], out[1]
            return {"leads_group": pid == pgid, "parent_group": int(pgid) == os.getpgrp()}
        return call
    for kw in ({"start_new_session": True}, {"process_group": 0}, {}):
        plain, hooked = _pair(spawns, shape(**kw))
        assert hooked == plain, kw
    assert _observe(shape(process_group=0))["value"] == {"leads_group": True, "parent_group": False}


def test_pass_fds_reach_the_child_and_nothing_else_does(spawns, tmp_path):
    keep_r, keep_w = os.pipe()
    stray = os.open(tmp_path / "stray", os.O_CREAT | os.O_WRONLY)
    os.set_inheritable(stray, True)
    try:
        def call():
            # One line per fd in a Python child: portable to any fd number (a /bin/sh
            # redirection like >&12 only takes single digits in POSIX sh, dash on Linux).
            script = ("import os, sys\n"
                      f"os.write({keep_w}, b'kept\\n')\n"
                      f"for fd in ({keep_w}, {stray}):\n"
                      "    try:\n        os.fstat(fd); print(fd, 'open')\n"
                      "    except OSError:\n        print(fd, 'closed')\n")
            return subprocess.run([sys.executable, "-c", script], pass_fds=(keep_w,), capture_output=True,
                                  text=True, timeout=30)
        plain, hooked = _pair(spawns, call)
        assert plain["out"] == f"{keep_w} open\n{stray} closed\n" and plain["rc"] == 0
        assert hooked == plain
        assert os.read(keep_r, 100) == b"kept\nkept\n"
    finally:
        for fd in (keep_r, keep_w, stray):
            os.close(fd)


def test_shell_true(spawns):
    def call():
        return subprocess.run("echo $0 && exit 5", shell=True, capture_output=True, text=True, timeout=10)
    plain, hooked = _pair(spawns, call)
    assert plain["rc"] == 5 and hooked == plain


@pytest.mark.parametrize("argv", [["t-aa2e2d4e-no-such-binary"], ["/nonexistent/t-aa2e2d4e"]])
def test_missing_executable_raises_the_same_error(spawns, argv):
    plain, hooked = _pair(spawns, lambda: subprocess.run(argv, capture_output=True), native_expected=None)
    assert plain["raised"] == "FileNotFoundError"
    assert hooked == plain


def test_missing_cwd_raises_the_same_error(spawns, tmp_path):
    def call():
        return subprocess.run(["/bin/sh", "-c", "true"], cwd=str(tmp_path / "gone"))
    plain, hooked = _pair(spawns, call, native_expected=None)
    assert plain["raised"] == "FileNotFoundError" and hooked == plain


@pytest.mark.parametrize("cwd_kind", ["gone", "a-file"])
def test_path_search_with_a_bad_cwd_names_the_cwd(spawns, tmp_path, cwd_kind):
    """CPython chdirs before it tries any PATH entry, so a bad cwd wins over a missing name."""
    afile = tmp_path / "afile"
    afile.write_text("x")
    cwd = str(tmp_path / "gone") if cwd_kind == "gone" else str(afile)
    for argv in (["t-aa2e2d4e-no-such-binary"], ["sh", "-c", "true"]):
        plain, hooked = _pair(spawns, lambda: subprocess.run(argv, cwd=cwd), native_expected=None)
        assert plain["raised"] in ("FileNotFoundError", "NotADirectoryError") and plain["filename"] == cwd
        assert hooked == plain, argv


def test_asyncio_subprocess_takes_the_hook(spawns):
    """asyncio.create_subprocess_exec builds a subprocess.Popen: covered without a call-site change."""
    import asyncio

    async def go():
        proc = await asyncio.create_subprocess_exec("echo", "aio", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return (proc.returncode, out)
    plain, hooked = _pair(spawns, lambda: asyncio.run(go()))
    assert plain == {"value": (0, b"aio\n")} and hooked == plain


# ------------------------------------------------------------------ what must keep fork_exec

def test_preexec_fn_keeps_plain_popen(spawns):
    plain, hooked = _pair(spawns, lambda: subprocess.call(["/bin/sh", "-c", "exit 0"], preexec_fn=os.getpid),
                          native_expected=False)
    assert plain == {"value": 0} and hooked == plain


def test_relative_executable_keeps_plain_popen(spawns, tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\npwd -P\n")
    tool.chmod(0o755)
    plain, hooked = _pair(spawns, lambda: subprocess.run(["./tool"], cwd=str(tmp_path), capture_output=True,
                                                         text=True, timeout=10), native_expected=False)
    assert plain["out"].strip() == str(tmp_path.resolve()) and hooked == plain


def test_close_fds_false_keeps_plain_popen(spawns):
    """close_fds=False is CPython's own decision: its os.posix_spawn branch (no fork) or fork_exec."""
    before = spawns.native_ok
    plain, hooked = _pair(spawns, lambda: subprocess.call(["/bin/sh", "-c", "exit 0"], close_fds=False),
                          native_expected=None)
    assert hooked == plain and spawns.native_ok == before
    plain, hooked = _pair(spawns, lambda: subprocess.call(["/bin/sh", "-c", "exit 0"], close_fds=False,
                                                          cwd="/"), native_expected=False)
    assert hooked == plain


def test_kill_switch_keeps_plain_popen(spawns, monkeypatch):
    monkeypatch.setenv("HERMES_SPAWN_LEGACY", "1")
    plain, hooked = _pair(spawns, lambda: subprocess.call(["/bin/sh", "-c", "exit 0"]), native_expected=False)
    assert hooked == plain


# ------------------------------------------------------------------ install / uninstall

def test_install_is_idempotent_and_uninstall_restores():
    stdlib = nofork._stdlib_popen()
    assert stdlib is not None and stdlib.__module__ == "subprocess"
    original = stdlib.__dict__["_execute_child"]
    try:
        first, second = nofork.install_process_hook(), nofork.install_process_hook()
        assert first is second is NATIVE_HOST
        assert nofork.process_hook_installed() is NATIVE_HOST
    finally:
        nofork.uninstall_process_hook()
    assert stdlib.__dict__["_execute_child"] is original
    assert not nofork.process_hook_installed()


def test_plan_refuses_what_posix_spawn_cannot_express():
    """Pure decision table, checked on every host (CI is Linux)."""
    base = dict(args=["/bin/true"], executable=None, preexec_fn=None, close_fds=True, pass_fds=(),
                cwd=None, env=None, startupinfo=None, creationflags=0, shell=False, p2cread=-1,
                p2cwrite=-1, c2pread=-1, c2pwrite=-1, errread=-1, errwrite=-1, restore_signals=True,
                gid=None, gids=None, uid=None, umask=-1, start_new_session=False, process_group=-1)
    assert nofork._plan(dict(base)) is not None
    plan = nofork._plan({**base, "args": "echo hi", "shell": True})
    assert plan is not None
    refused = [dict(preexec_fn=os.getpid), dict(close_fds=False), dict(restore_signals=False),
               dict(uid=0), dict(gid=0), dict(gids=[0]), dict(umask=0o22),
               dict(start_new_session=True, process_group=0), dict(p2cread=0), dict(errwrite=2),
               dict(pass_fds=(1,)), dict(args=["./rel"]),
               dict(args=["x"], env={"PATH": "relative/bin"})]
    for change in refused:
        assert nofork._plan({**base, **change}) is None, change
    argv, candidates, _, pgroup, _ = plan
    assert argv == ["/bin/sh", "-c", "echo hi"] and candidates == [b"/bin/sh"] and pgroup == -1
    plan = nofork._plan({**base, "args": ["git"], "env": {"PATH": "/a:/b"}})
    assert plan is not None and plan[1] == [b"/a/git", b"/b/git"]
    plan = nofork._plan({**base, "process_group": 0})
    assert plan is not None and plan[3] == 0


def test_hook_survives_a_popen_subclass(spawns):
    """asyncio and libraries subclass Popen; the hook sits on the base method they inherit."""
    class Sub(subprocess.Popen):
        pass

    def call():
        with Sub(["/bin/sh", "-c", "echo sub"], stdout=subprocess.PIPE) as proc:
            return proc.communicate(timeout=10)[0]
    plain, hooked = _pair(spawns, call)
    assert plain == {"value": b"sub\n"} and hooked == plain


def test_signal_reset_matches(spawns):
    """SIGPIPE is default in the child on both paths whatever the parent's disposition."""
    old = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        plain, hooked = _pair(spawns, lambda: subprocess.call(["/bin/sh", "-c", "kill -PIPE $$"]))
    finally:
        signal.signal(signal.SIGPIPE, old)
    assert plain == {"value": -signal.SIGPIPE} and hooked == plain
