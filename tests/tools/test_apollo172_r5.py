"""Apollo round-5 probes for PR 172 at c6a73e5f49: the no-fork exec helper against plain Popen.

Two things are checked.

1. The comparison is not vacuous. Where ``os.posix_spawn(setsid=True)`` raises
   NotImplementedError the no-fork class falls back to plain Popen, so a parity test that only
   counts ATTEMPTED helper spawns compares Popen with Popen and passes. Every probe here records
   whether a helper spawn RETURNED a pid, and SKIPS (never passes) when none did.
2. The target sees what plain Popen gives it in cases the committed oracle does not cover: the
   helper is a Python interpreter started with the COMMAND's environment, so variables that
   steer interpreter startup (locale, __PYVENV_LAUNCHER__, loader paths), odd argv and cwd
   values, launch errors, signals and descriptors are compared directly.

Arms: plain = ``_FORK_COPIES_PARENT`` False (main's Popen); head = True (as on macOS).
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys

import pytest

import tools.spawn_nofork as nofork
from tests.tools import test_spawn_parity as oracle

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def spawns(monkeypatch):
    """(is_native, outcome) for every native libc posix_spawn call; outcome is 'ok' (returned 0),
    'errno N' or the exception name. (Round 6: the helper is gone; the native call is the path.)"""
    log = []
    real = nofork._native_spawn

    def recorded(*args, **kwargs):
        try:
            rc, pid = real(*args, **kwargs)
        except BaseException as exc:
            log.append((True, type(exc).__name__))
            raise
        log.append((True, "ok" if rc == 0 else f"errno {rc}"))
        return rc, pid

    monkeypatch.setattr(nofork, "_native_spawn", recorded)
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    return log


def _helper_ran(log) -> bool:
    """A native posix_spawn call that RETURNED 0 (a child was started by it)."""
    return any(native and outcome == "ok" for native, outcome in log)


def _run(monkeypatch, spawns, nofork_on, argv, *, new_session=False, stdin_bytes=None, **kwargs):
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", nofork_on)
    spawns.clear()
    try:
        proc = nofork.spawn_popen(
            argv, new_session=new_session, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL, **kwargs)
    except OSError as exc:
        return {"raised": type(exc).__name__, "errno": exc.errno, "filename": exc.filename}
    out, err = proc.communicate(stdin_bytes, timeout=60)
    return {"rc": proc.returncode, "out": out, "err": err}


def _pair(monkeypatch, spawns, argv, **kwargs):
    plain = _run(monkeypatch, spawns, False, argv, **kwargs)
    assert not any(helper for helper, _ in spawns), "plain arm used the helper"
    head = _run(monkeypatch, spawns, True, argv, **kwargs)
    if "raised" not in head and not _helper_ran(spawns):
        pytest.skip(f"no helper spawn returned a pid here ({spawns}): this would compare Popen with Popen")
    return plain, head


def test_r5_report_host(spawns):
    try:
        pid = os.posix_spawn("/bin/sh", ["/bin/sh", "-c", "exit 0"], os.environ, setsid=True)
        os.waitpid(pid, 0)
        setsid = "supported"
    except NotImplementedError as exc:
        setsid = f"NOT supported ({exc})"
    print(f"\nR5 host: {platform.system()} {platform.release()} python {platform.python_version()} "
          f"({sys.executable}); posix_spawn setsid {setsid}; production no-fork path "
          f"{'ON' if nofork._FORK_COPIES_PARENT else 'OFF (plain Popen)'} on this platform")


# ---------------------------------------------------------------- the committed oracle, strictly

def _oracle_extra(case, tmp_path, monkeypatch):
    if case == "all":
        return {k: v for env in oracle._hostile(tmp_path).values() for k, v in env.items()}
    if case == "lang-c-no-lc":  # interpreter start may coerce LC_CTYPE when only LANG=C is set
        for name in ("LC_ALL", "LC_CTYPE"):
            monkeypatch.delenv(name, raising=False)
        return {"LANG": "C"}
    if case == "pyvenv-launcher":
        return {"__PYVENV_LAUNCHER__": "/nonexistent/bin/python3"}
    if case == "python-startup-vars":
        return {"PYTHONHOME": "/nonexistent", "PYTHONUTF8": "0", "PYTHONMALLOC": "bogus",
                "PYTHONCOERCECLOCALE": "0", "PYTHONWARNINGS": "error", "PYTHONDEVMODE": "1"}
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["all", "lang-c-no-lc", "pyvenv-launcher", "python-startup-vars"])
def test_r5_terminal_oracle_through_a_real_helper_spawn(monkeypatch, tmp_path, spawns, case):
    """LocalEnvironment.execute (new session) on both arms; skipped unless the helper really ran."""
    extra = _oracle_extra(case, tmp_path, monkeypatch)
    plain = oracle._observe(monkeypatch, tmp_path, extra, nofork_on=False, tag="plain")
    spawns.clear()
    head = oracle._observe(monkeypatch, tmp_path, extra, nofork_on=True, tag="nofork")
    if not _helper_ran(spawns):
        pytest.skip(f"helper spawn did not succeed here ({spawns}): the committed oracle is vacuous on this host")
    differences = {k: (plain.get(k), head.get(k)) for k in plain.keys() | head.keys() if plain.get(k) != head.get(k)}
    assert not differences, differences


# ---------------------------------------------------------------- direct spawn_popen vs Popen

_ENVS = {
    "empty": {},
    "lang-c": {"LANG": "C"},
    "lc-all-c": {"LC_ALL": "C"},
    "lc-ctype-empty": {"LC_CTYPE": ""},
    "lc-ctype-utf8": {"LC_CTYPE": "UTF-8", "LANG": "en_US.UTF-8"},
    "python-startup-vars": {"PYTHONHOME": "/nonexistent", "PYTHONUTF8": "0", "PYTHONMALLOC": "bogus",
                            "PYTHONCOERCECLOCALE": "0", "PYTHONWARNINGS": "error", "PYTHONDEVMODE": "1",
                            "PYTHONIOENCODING": "latin-1", "PYTHONPATH": "/nonexistent"},
    "pyvenv-launcher": {"__PYVENV_LAUNCHER__": "/nonexistent/bin/python3"},
    "loader-paths": {"DYLD_LIBRARY_PATH": "/nonexistent", "DYLD_FALLBACK_LIBRARY_PATH": "/nonexistent",
                     "LD_LIBRARY_PATH": "/nonexistent"},
    "odd-values": {"A": "line1\nline2", "B": " ", "C": "", "D": os.fsdecode(b"caf\xe9\xff"), "E": "=x="},
}


@pytest.mark.parametrize("new_session", [False, True], ids=["same-session", "new-session"])
@pytest.mark.parametrize("name", sorted(_ENVS))
def test_r5_environment_reaches_the_target_unchanged(monkeypatch, spawns, name, new_session):
    env_tool = next(p for p in ("/usr/bin/env", "/bin/env") if os.path.exists(p))
    plain, head = _pair(monkeypatch, spawns, [env_tool, "-0"], env=dict(_ENVS[name]), new_session=new_session)
    assert plain["rc"] == 0, plain
    as_set = lambda r: sorted(r["out"].split(b"\0"))  # noqa: E731
    assert (head["rc"], as_set(head), head["err"]) == (plain["rc"], as_set(plain), plain["err"]), (
        f"env differs: only-plain={set(as_set(plain)) - set(as_set(head))} "
        f"only-head={set(as_set(head)) - set(as_set(plain))} err={head['err'][:300]!r} rc={head['rc']}")


def test_r5_argv_reaches_the_target_unchanged(monkeypatch, spawns):
    argv = ["/bin/sh", "-c", 'printf "[%s]\\n" "$0" "$#" "$@"', "zero", "", "-c", "--", "-I", "b c",
            "new\nline", os.fsdecode(b"caf\xe9"), "'q\"q'"]
    plain, head = _pair(monkeypatch, spawns, argv)
    assert head == plain


@pytest.mark.parametrize("dirname", ["plain", "with space", "-I", "--", "new\nline", "quote'\"s", b"caf\xe9"],
                         ids=["plain", "space", "dash-I", "dashdash", "newline", "quotes", "non-utf8"])
def test_r5_cwd_reaches_the_target_unchanged(monkeypatch, spawns, tmp_path, dirname):
    try:
        cwd = os.path.join(os.fsencode(tmp_path), os.fsencode(dirname))
        os.mkdir(cwd)
    except OSError as exc:
        pytest.skip(f"filesystem refuses this name: {exc}")
    tool = os.path.join(cwd, b"tool")
    with open(tool, "wb") as fh:
        fh.write(b"#!/bin/sh\npwd -P\n")
    os.chmod(tool, 0o755)
    for argv in (["/bin/sh", "-c", "pwd -P"], ["./tool"]):  # ./tool: resolved against the NEW cwd
        plain, head = _pair(monkeypatch, spawns, argv, cwd=os.fsdecode(cwd))
        assert plain.get("rc") == 0, plain
        assert head == plain, (argv, plain, head)


def _launch_error_cases(tmp_path):
    notexec = tmp_path / "notexec"
    notexec.write_text("#!/bin/sh\n")
    notexec.chmod(0o644)
    badformat = tmp_path / "badformat"
    badformat.write_bytes(b"\x00\x01\x02 not a program\n")
    badformat.chmod(0o755)
    afile = tmp_path / "afile"
    afile.write_text("x")
    return {
        "missing-executable": dict(argv=[str(tmp_path / "nope")]),
        "not-executable": dict(argv=[str(notexec)]),
        "directory-as-executable": dict(argv=[str(tmp_path)]),
        "bad-exec-format": dict(argv=[str(badformat)]),
        "missing-cwd": dict(argv=["/bin/sh", "-c", "exit 0"], cwd=str(tmp_path / "gone")),
        "cwd-is-a-file": dict(argv=["/bin/sh", "-c", "exit 0"], cwd=str(afile)),
    }


@pytest.mark.parametrize("case", ["missing-executable", "not-executable", "directory-as-executable",
                                  "bad-exec-format", "missing-cwd", "cwd-is-a-file"])
def test_r5_launch_errors_match_popen(monkeypatch, spawns, tmp_path, case):
    """Plain Popen raises from the constructor for every one of these."""
    spec = _launch_error_cases(tmp_path)[case]
    argv = spec.pop("argv")
    plain = _run(monkeypatch, spawns, False, argv, **spec)
    head = _run(monkeypatch, spawns, True, argv, **spec)
    print(f"\nR5 launch-error {case}: plain={plain} head={ {k: (v[:120] if isinstance(v, bytes) else v) for k, v in head.items()} }")
    assert "raised" in plain, plain
    assert (head.get("raised"), head.get("errno")) == (plain["raised"], plain["errno"]), (plain, head)
    assert os.fsdecode(head["filename"]) == os.fsdecode(plain["filename"]), (plain, head)


def test_r5_exit_status_signal_and_stdin(monkeypatch, spawns):
    for argv in (["/bin/sh", "-c", "exit 7"], ["/bin/sh", "-c", "kill -TERM $$"], ["/bin/sh", "-c", "kill -KILL $$"]):
        plain, head = _pair(monkeypatch, spawns, argv)
        assert head == plain, (argv, plain, head)
    payload = os.urandom(300_000)
    plain, head = _pair(monkeypatch, spawns, ["/bin/cat"], stdin_bytes=payload)
    assert plain["out"] == payload and head == plain


def test_r5_signal_mask_and_dispositions_reach_the_target(monkeypatch, spawns):
    """A blocked signal and ignored signals in the parent are inherited by a Popen child."""
    report = ("import signal;print(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK,[])),"
              "[int(signal.getsignal(s)==signal.SIG_IGN) for s in (signal.SIGUSR2,signal.SIGHUP,signal.SIGTERM)])")
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGUSR2, signal.SIGHUP)}
    signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGUSR1])
    try:
        plain, head = _pair(monkeypatch, spawns, [sys.executable, "-I", "-S", "-c", report])
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGUSR1])
        for s, handler in old.items():
            signal.signal(s, handler)
    assert plain["rc"] == 0, plain
    assert head == plain


def test_r5_descriptors_and_umask(monkeypatch, spawns, tmp_path):
    leak = os.open(tmp_path / "leak", os.O_CREAT | os.O_WRONLY)
    os.set_inheritable(leak, True)
    previous = os.umask(0o027)
    try:
        plain, head = _pair(monkeypatch, spawns, ["/bin/sh", "-c", "umask; ls /dev/fd/ | sort -n | tr '\\n' ' '"])
    finally:
        os.umask(previous)
        os.close(leak)
    assert plain["rc"] == 0, plain
    assert head == plain
