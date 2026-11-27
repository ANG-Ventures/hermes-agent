"""Terminal-tool spawns must not fork the gateway where fork copies it (t_afc525a2).

``Popen(cwd=..., start_new_session=True)`` and the default ``close_fds=True`` send CPython
to ``_posixsubprocess.fork_exec``, which on macOS forks the whole (6 GB) gateway with the
GIL held. On macOS ``tools.spawn_nofork`` makes one libc ``posix_spawn`` call instead.
Tests of that native path make fork_exec raise, require a native spawn that RETURNED 0, and
SKIP with a reason off macOS (there is no such path there; they never pass vacuously). The
off-macOS arm asserts plain Popen / subprocess.run. Real subprocesses throughout: a mock
cannot show group membership or fd inheritance. GIL-hold numbers:
``tests/tools/spawn_gil_bench.py``. Parity with plain Popen: ``test_spawn_parity.py`` and
``test_apollo172_r5.py``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

import pytest

import tools.spawn_nofork as nofork
from tools.environments.local import LocalEnvironment
from tools.file_operations_lint_shell import _parse_check, _parse_bash

pytestmark = pytest.mark.platforms("posix")

_NATIVE_HOST = sys.platform == "darwin"


@pytest.fixture
def force_nofork(monkeypatch):
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", True)
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)


@pytest.fixture
def native(monkeypatch, force_nofork):
    """The native path only: fork_exec raises, and each test must start >= 1 child natively.

    Yields the list of native posix_spawn return codes.
    """
    if not _NATIVE_HOST:
        pytest.skip(f"native posix_spawn path is macOS-only; {sys.platform} uses plain Popen")
    assert nofork._bindings() is not None, "libc posix_spawn bindings failed to resolve on macOS"
    import _posixsubprocess

    def no_fork(*args, **kwargs):
        raise AssertionError("fork_exec called: this spawn forks the whole parent under the GIL")

    monkeypatch.setattr(_posixsubprocess, "fork_exec", no_fork)
    if hasattr(subprocess, "_fork_exec"):  # 3.12+ binds it at import
        monkeypatch.setattr(subprocess, "_fork_exec", no_fork)
    codes = []
    real = nofork._native_spawn

    def counted(*args, **kwargs):
        rc, pid = real(*args, **kwargs)
        codes.append(rc)
        return rc, pid

    monkeypatch.setattr(nofork, "_native_spawn", counted)
    yield codes
    assert 0 in codes, f"no native posix_spawn returned 0 ({codes}): the test did not exercise the path"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # an unreaped zombie still answers kill(0)
        out = nofork.run_nofork(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return True
    return bool(out.strip()) and not out.strip().startswith("Z")


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "work dir"
    work.mkdir()
    environment = LocalEnvironment(cwd=str(work), timeout=30)
    yield environment
    environment.cleanup()


def test_execute_does_not_fork_and_keeps_cwd_env_stdin(native, env, tmp_path):
    env.env["T_AFC525A2_MARK"] = "ok-env"
    result = env.execute('pwd -P; echo "$T_AFC525A2_MARK"; cat')
    assert result["returncode"] == 0, result
    lines = result["output"].strip().splitlines()
    assert lines[0] == os.path.realpath(tmp_path / "work dir")
    assert lines[1] == "ok-env"

    piped = env.execute("cat", stdin_data="line one\nline two")
    assert piped["returncode"] == 0, piped
    assert piped["output"].strip() == "line one\nline two"


def test_run_bash_leads_new_session_in_cwd(native, env, tmp_path):
    proc = env._run_bash('echo "$$"; pwd -P; sleep 30')
    try:
        assert int(proc.stdout.readline()) == proc.pid, "bash must be the spawned pid (no program in between)"
        assert proc.stdout.readline().strip() == os.path.realpath(tmp_path / "work dir")
        assert os.getpgid(proc.pid) == proc.pid != os.getpgrp()
        assert os.getsid(proc.pid) == proc.pid
    finally:
        env._kill_process(proc)


def test_kill_process_takes_down_grandchildren(native, env):
    proc = env._run_bash("sleep 300 & echo $!; sleep 300")
    grandchild = int(proc.stdout.readline())
    assert _alive(grandchild)
    env._kill_process(proc)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild), "process-group kill left the grandchild running"
    assert proc.poll() is not None


def test_timeout_returns_124_and_reaps_the_group(native, env, tmp_path):
    marker = tmp_path / "gc.pid"
    started = time.monotonic()
    result = env.execute(f"printf 'before-timeout\\377'; sleep 300 & echo $! > '{marker}'; sleep 300", timeout=2)
    assert result["returncode"] == 124, result
    assert "before-timeout" in result["output"]
    assert time.monotonic() - started < 20
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild)


@pytest.mark.parametrize("new_session", [False, True])
def test_inheritable_fds_are_not_passed_to_the_child(native, tmp_path, new_session):
    leak = os.open(tmp_path / "leak", os.O_CREAT | os.O_WRONLY)
    os.set_inheritable(leak, True)  # a leak close_fds=True used to stop
    high = os.dup2(leak, 200, inheritable=True)
    try:
        proc = nofork.spawn_popen(["/bin/sh", "-c", "ls /dev/fd | sort -n | tr '\\n' ' '"],
                                  new_session=new_session, stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate(timeout=10)
    finally:
        os.close(high)
        os.close(leak)
    listed = {int(x) for x in out.split()}
    assert leak not in listed and 200 not in listed, out
    assert {0, 1, 2} <= listed, out


def test_new_session_and_same_session(native):
    for new_session in (True, False):
        proc = nofork.spawn_popen(["/bin/sleep", "30"], new_session=new_session)
        try:
            if new_session:
                assert os.getsid(proc.pid) == proc.pid == os.getpgid(proc.pid)
            else:
                assert os.getsid(proc.pid) == os.getsid(0) and os.getpgid(proc.pid) == os.getpgrp()
        finally:
            proc.kill()
            proc.wait(timeout=10)


def test_parse_check_does_not_fork(native):
    _parse_bash.cache_clear()
    try:
        assert _parse_check("echo ok\n", "bash")[0] is True
        ok, msg = _parse_check("if then fi (\n", "bash")
        assert ok is False and "syntax error" in msg
    finally:
        _parse_bash.cache_clear()


def test_run_nofork_timeout_does_not_wait_for_descendants(native, monkeypatch):
    """subprocess.run's POSIX contract: kill, wait, re-raise with the output so far.

    A second communicate() would block until the backgrounded sleep (reparented after
    the kill, so it ends on its own) closes the pipe.
    """
    calls = []
    real_communicate = subprocess.Popen.communicate

    def counted(self, input=None, timeout=None):
        calls.append(timeout)
        return real_communicate(self, input, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", counted)
    with pytest.raises(subprocess.TimeoutExpired) as info:
        nofork.run_nofork(["/bin/sh", "-c", "printf partial; sleep 5 & wait"],
                          capture_output=True, timeout=1)
    assert calls == [1], "communicate() must not run again after the timeout"
    assert info.value.stdout == b"partial"


def _launch_error(argv, **kwargs):
    try:
        nofork.spawn_popen(argv, stdout=subprocess.PIPE, **kwargs).communicate(timeout=10)
    except OSError as exc:
        return type(exc).__name__, exc.errno, os.fsdecode(exc.filename)
    return None


def _native_only():
    if not _NATIVE_HOST:
        pytest.skip(f"native posix_spawn path is macOS-only; {sys.platform} uses plain Popen")


@pytest.mark.parametrize("new_session", [False, True])
def test_launch_errors_raise_from_the_constructor_like_popen(monkeypatch, tmp_path, new_session):
    """Each case raises what plain Popen raises (type, errno, filename), from the native call."""
    _native_only()
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    codes = []
    real = nofork._native_spawn

    def counted(*args, **kwargs):
        rc, pid = real(*args, **kwargs)
        codes.append(rc)
        return rc, pid

    monkeypatch.setattr(nofork, "_native_spawn", counted)
    notexec = tmp_path / "not-executable"
    notexec.write_text("#!/bin/sh\necho ran\n")
    badformat = tmp_path / "badformat"
    badformat.write_bytes(b"\x00\x01\x02 not a program\n")
    badformat.chmod(0o755)
    afile = tmp_path / "afile"
    afile.write_text("x")
    cases = [([str(tmp_path / "missing")], {}), ([str(notexec)], {}), ([str(tmp_path)], {}),
             ([str(badformat)], {}), (["/bin/sh", "-c", "exit 0"], {"cwd": str(tmp_path / "gone")}),
             (["/bin/sh", "-c", "exit 0"], {"cwd": str(afile)})]
    for argv, kwargs in cases:
        monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", False)
        plain = _launch_error(argv, new_session=new_session, **kwargs)
        assert not codes
        monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", True)
        head = _launch_error(argv, new_session=new_session, **kwargs)
        assert len(codes) == 1 and codes[0] != 0, ("the native call did not refuse", argv, codes)
        codes.clear()
        assert plain is not None and head == plain, (argv, kwargs, plain, head)


def test_off_macos_the_spawn_is_plain_popen(monkeypatch):
    """Off macOS fork_exec does not copy the parent: the terminal tool is main's Popen and the
    shell lint is main's subprocess.run."""
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", False)
    assert not nofork.eligible(["/bin/true"], {"cwd": "/"})
    proc = nofork.spawn_popen(["/bin/sh", "-c", "exit 0"], new_session=True)
    proc.wait(timeout=10)
    assert type(proc) is subprocess.Popen
    calls = []
    real_run = subprocess.run

    def recorded(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    result = nofork.run_nofork(["/bin/echo", "x"], capture_output=True, timeout=5)
    assert result.stdout == b"x\n"
    assert calls == [((["/bin/echo", "x"],), {"input": None, "timeout": 5, "capture_output": True})]


def test_production_flag_is_macos_only(monkeypatch):
    import importlib
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    importlib.reload(nofork)
    assert nofork._FORK_COPIES_PARENT is (sys.platform == "darwin")


def _reset_bindings(monkeypatch):
    monkeypatch.setattr(nofork, "_libc", nofork._UNRESOLVED)


def test_missing_binding_means_plain_popen_and_logs_once(force_nofork, monkeypatch, caplog):
    import ctypes
    real_libc = ctypes.CDLL(None)

    class WithoutInherit:
        def __getattr__(self, name):
            if name == "posix_spawn_file_actions_addinherit_np":
                raise AttributeError(name)
            return getattr(real_libc, name)

    _reset_bindings(monkeypatch)
    monkeypatch.setattr(nofork, "_load_libc", WithoutInherit)
    with caplog.at_level(logging.WARNING, logger=nofork.__name__):
        assert not nofork.eligible(["/bin/true"], {})
        assert not nofork.eligible(["/bin/true"], {})
        proc = nofork.spawn_popen(["/bin/sh", "-c", "echo ok"], new_session=True, stdout=subprocess.PIPE)
        assert proc.communicate(timeout=10)[0] == b"ok\n"
    assert type(proc) is subprocess.Popen
    assert nofork._libc is None
    assert len([r for r in caplog.records if "bindings unavailable" in r.getMessage()]) == 1


def test_setup_failure_falls_back_for_the_life_of_the_process(force_nofork, monkeypatch, caplog):
    _native_only()
    _reset_bindings(monkeypatch)

    def failing(*args, **kwargs):
        raise nofork._SetupFailed("addchdir")

    monkeypatch.setattr(nofork, "_native_spawn", failing)
    with caplog.at_level(logging.WARNING, logger=nofork.__name__):
        proc = nofork.spawn_popen(["/bin/sh", "-c", "echo ok"], new_session=True, stdout=subprocess.PIPE)
        assert proc.communicate(timeout=10)[0] == b"ok\n"
    assert nofork._libc is None and not nofork.eligible(["/bin/true"], {})
    assert [r.getMessage() for r in caplog.records if "setup failed" in r.getMessage()] == [
        "spawn_nofork: posix_spawn setup failed (addchdir); using Popen for the rest of this process"]


def test_relative_executable_is_plain_popen(force_nofork):
    assert not nofork.eligible(["./tool"], {"cwd": "/tmp"})
    assert not nofork.eligible(["bash"], {})


def test_ineligible_calls_keep_plain_popen_semantics(force_nofork, tmp_path):
    assert not nofork.eligible(["/bin/sh"], {"pass_fds": ()})
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'pwd -P; echo "$$ $(ps -o pgid= -p $$)"'], new_session=True,
                              cwd=str(tmp_path), stdout=subprocess.PIPE, text=True, pass_fds=())
    out = proc.communicate(timeout=10)[0].split("\n")
    assert out[0] == os.path.realpath(tmp_path)
    pid, pgid = (int(x) for x in out[1].split())
    assert pid == pgid


def test_legacy_env_switch_forces_plain_popen(force_nofork, monkeypatch):
    monkeypatch.setenv("HERMES_SPAWN_LEGACY", "1")
    assert not nofork.eligible(["/bin/true"], {})
    monkeypatch.delenv("HERMES_SPAWN_LEGACY")
    assert nofork.eligible(["/bin/true"], {}) is (nofork._bindings() is not None)


def test_popen_function_import_and_execute(monkeypatch, tmp_path):
    import importlib
    import tools.environments.local as local
    real = subprocess.Popen
    calls = []

    def replacement(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", replacement)
    importlib.reload(nofork)
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", True)
    importlib.reload(local)
    environment = local.LocalEnvironment(cwd=str(tmp_path), timeout=10)
    try:
        result = environment.execute("printf import-ok")
        assert result["returncode"] == 0 and "import-ok" in result["output"]
        assert calls
    finally:
        environment.cleanup()


def test_symlinked_cwd_matches_plain_popen(native, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    environment = LocalEnvironment(cwd=str(link), timeout=10)
    try:
        result = environment.execute("pwd -P")
        assert result["returncode"] == 0 and result["output"].strip() == str(real.resolve())
    finally:
        environment.cleanup()


def test_late_inheritable_fd_is_closed(native, monkeypatch, tmp_path):
    secret = os.open(tmp_path / "secret", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(secret, False)
    counted = nofork._native_spawn

    def race(*args, **kwargs):
        os.set_inheritable(secret, True)
        return counted(*args, **kwargs)

    monkeypatch.setattr(nofork, "_native_spawn", race)
    try:
        result = nofork.run_nofork(
            ["/bin/sh", "-c", f'[ -e /dev/fd/{secret} ] && echo leaked || echo closed'],
            capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout.strip() == "closed"
    finally:
        os.close(secret)


def test_explicit_standard_fd_keeps_new_session(force_nofork, tmp_path):
    output = tmp_path / "session"
    proc = nofork.spawn_popen(
        ["/bin/sh", "-c", f'echo "$$" > "{output}"; sleep 30'],
        stdout=1, new_session=True)
    try:
        assert os.getsid(proc.pid) == proc.pid
    finally:
        os.killpg(proc.pid, 9)
        proc.wait(timeout=10)


@pytest.mark.parametrize("disposition", ["SIG_DFL", "SIG_IGN"])
def test_child_signal_dispositions_match_popen(native, disposition):
    """restore_signals=True is the only eligible value: SIGPIPE is default in the child
    whatever the parent's disposition, as plain Popen leaves it."""
    old = signal.getsignal(signal.SIGPIPE)
    try:
        signal.signal(signal.SIGPIPE, getattr(signal, disposition))
        cmd = ["/bin/sh", "-c", "kill -PIPE $$; printf survived"]
        result = nofork.run_nofork(cmd, capture_output=True)
        assert result.returncode == -signal.SIGPIPE
    finally:
        signal.signal(signal.SIGPIPE, old)


def test_restore_signals_false_is_plain_popen(force_nofork):
    assert not nofork.eligible(["/bin/true"], {"restore_signals": False})


def test_exported_functions_preserved_without_overriding_cwd(native, env, tmp_path):
    env.env["BASH_FUNC_greet%%"] = "() { printf function-ok; }"
    env.env["BASH_FUNC_cd%%"] = "() { return 0; }"
    env.env["BASH_FUNC_exec%%"] = "() { return 0; }"
    result = env.execute("greet; printf '\\n'; pwd -P")
    assert result["returncode"] == 0, result
    assert result["output"].strip().splitlines() == [
        "function-ok", os.path.realpath(tmp_path / "work dir")]


def test_wrapped_execute_child_signature(native, monkeypatch):
    real = subprocess.Popen

    class WrappedPopen(real):
        def _execute_child(self, args, executable, *rest, **kw):
            return real._execute_child(self, args, executable, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", WrappedPopen)
    result = nofork.run_nofork(["/bin/cat"], input="wrapped", capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "wrapped"


def test_run_input_and_conflicts(native):
    result = nofork.run_nofork(["/bin/cat"], input="piped", capture_output=True, text=True)
    assert result.stdout == "piped"
    with pytest.raises(ValueError):
        nofork.run_nofork(["/bin/cat"], input="bad", stdin=subprocess.PIPE)


def test_module_imports_without_fcntl(monkeypatch):
    """Windows has no fcntl; local.py and the shell lint import this module at top level."""
    import importlib

    monkeypatch.setitem(sys.modules, "fcntl", None)
    try:
        importlib.reload(nofork)
        assert not nofork._is_open(987654)
        r, w = os.pipe()
        try:
            assert nofork._is_open(r)
        finally:
            os.close(r)
            os.close(w)
        assert not nofork._is_open(r)
    finally:
        monkeypatch.undo()
        importlib.reload(nofork)
