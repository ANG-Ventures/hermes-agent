"""macOS: spawn a process that is its own *responsible process*.

macOS charges a child to the process that spawned it (its "responsible process", normally the
launchd job at the root of the tree). Local Network Privacy is evaluated against that process.
A detached delegation child that outlives its gateway is still charged to the dead gateway pid,
and from then on its non-platform binaries (Homebrew Python, ping, curl from Anaconda) get
``EHOSTUNREACH`` (errno 65) to the home LAN while ``/usr/bin`` tools keep working (t_f8b497c9).

``posix_spawn`` with ``responsibility_spawnattrs_setdisclaim`` makes the child responsible for
itself, the same standing a launchd job (the gateway) has, so the gateway's restart no longer
changes what the child can reach. The child stays our child: the caller can ``waitpid`` it.

Private libsystem API, present since macOS 10.14; :func:`available` says whether it resolves.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import signal
import sys
from typing import Dict, List, Optional

IS_DARWIN = sys.platform == "darwin"

# <sys/spawn.h>
POSIX_SPAWN_SETSIGDEF = 0x0004
POSIX_SPAWN_SETSID = 0x0400
POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000

_libc = None


def _lib():
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(None, use_errno=True)
        _libc = lib
    return _libc


def available() -> bool:
    """True on macOS when the disclaim spawn API resolves."""
    if not IS_DARWIN:
        return False
    try:
        lib = _lib()
        return all(hasattr(lib, name) for name in (
            "responsibility_spawnattrs_setdisclaim", "posix_spawn",
            "posix_spawn_file_actions_addchdir_np"))
    except OSError:
        return False


def responsible_pid(pid: int) -> Optional[int]:
    """The pid macOS charges ``pid`` to (privacy prompts, Local Network Privacy), or None if unknown."""
    if not IS_DARWIN:
        return None
    try:
        fn = _lib().responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError):
        return None
    fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_int]
    value = fn(int(pid))
    return value if value > 0 else None


def lan_privacy_note(text: str, boot_responsible: Optional[int]) -> Optional[str]:
    """A note for a result that reports errno 65 (``No route to host``) while this process is still
    charged to a responsible process that has exited (a Popen child of a gateway that restarted).
    That is Local Network Privacy refusing an orphan, not a network outage. None otherwise.

    ``boot_responsible`` is :func:`responsible_pid` read at startup: once the responsible process
    exits, macOS answers the query with the child's own pid, so it cannot be read afterwards."""
    if not text or not _EHOSTUNREACH_RE.search(text):
        return None
    me = os.getpid()
    if boot_responsible is None or boot_responsible == me:
        return None
    try:
        os.kill(boot_responsible, 0)
        return None  # the process this child is charged to is alive: not this failure
    except ProcessLookupError:
        pass
    except OSError:
        return None
    return (f"[note: 'No route to host' (errno 65) here is macOS Local Network Privacy, not a network "
            f"outage: this detached process is charged to pid {boot_responsible} (the gateway that "
            f"spawned it), which has exited, so LAN connections from non-system binaries are refused.]")


_EHOSTUNREACH_RE = re.compile(r"No route to host|Errno 65\b|errno 65\b|EHOSTUNREACH")


class SpawnedProcess:
    """The part of ``subprocess.Popen`` callers of :func:`spawn_disclaimed` use: pid, poll(), wait(), returncode."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: Optional[int] = None

    def _reap(self, flags: int) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, flags)
        except ChildProcessError:
            self.returncode = 0  # reaped elsewhere; subprocess.Popen reports 0 here too
            return 0
        if pid == 0:
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def poll(self) -> Optional[int]:
        return self._reap(os.WNOHANG)

    def wait(self) -> int:
        code = self._reap(0)
        assert code is not None
        return code


def _cstr_array(items: List[str]):
    arr = (ctypes.c_char_p * (len(items) + 1))()
    arr[:-1] = [os.fsencode(s) for s in items]
    arr[-1] = None
    return arr


def _check(rc: int, what: str) -> None:
    if rc != 0:
        raise OSError(rc, f"{what}: {os.strerror(rc)}")


def spawn_disclaimed(argv: List[str], *, env: Dict[str, str], cwd: Optional[str],
                     stdout_fd: int) -> SpawnedProcess:
    """``Popen(argv, start_new_session=True, stdin=DEVNULL, stdout=stderr=stdout_fd, env, cwd)``,
    but the child is its own responsible process. Raises OSError when the spawn fails."""
    lib = _lib()
    path = argv[0]
    if os.sep not in path:
        found = shutil.which(path, path=env.get("PATH", os.defpath))
        if found is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        path = found

    attr = ctypes.c_void_p()  # posix_spawnattr_t is a pointer on Darwin
    actions = ctypes.c_void_p()  # so is posix_spawn_file_actions_t
    _check(lib.posix_spawnattr_init(ctypes.byref(attr)), "posix_spawnattr_init")
    try:
        _check(lib.posix_spawn_file_actions_init(ctypes.byref(actions)), "posix_spawn_file_actions_init")
        try:
            # Popen(restore_signals=True): Python ignores SIGPIPE/SIGXFSZ; the child starts with defaults.
            sigdef = ctypes.c_uint32(0)  # sigset_t is a 32-bit mask on Darwin
            for sig in (signal.SIGPIPE, signal.SIGXFSZ):
                sigdef.value |= 1 << (int(sig) - 1)
            _check(lib.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(sigdef)),
                   "posix_spawnattr_setsigdefault")
            # SETSID = start_new_session; CLOEXEC_DEFAULT = close_fds (only 0/1/2 below are inherited).
            flags = POSIX_SPAWN_SETSID | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT
            _check(lib.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(flags)),
                   "posix_spawnattr_setflags")
            _check(lib.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), ctypes.c_int(1)),
                   "responsibility_spawnattrs_setdisclaim")
            _check(lib.posix_spawn_file_actions_addopen(
                ctypes.byref(actions), 0, os.fsencode(os.devnull), os.O_RDONLY, 0),
                "posix_spawn_file_actions_addopen")
            for fd in (1, 2):
                _check(lib.posix_spawn_file_actions_adddup2(ctypes.byref(actions), stdout_fd, fd),
                       "posix_spawn_file_actions_adddup2")
            if cwd:
                _check(lib.posix_spawn_file_actions_addchdir_np(ctypes.byref(actions), os.fsencode(cwd)),
                       "posix_spawn_file_actions_addchdir_np")
            pid = ctypes.c_int(0)
            envp = _cstr_array([f"{k}={v}" for k, v in env.items()])
            rc = lib.posix_spawn(ctypes.byref(pid), os.fsencode(path), ctypes.byref(actions),
                                 ctypes.byref(attr), _cstr_array(list(argv)), envp)
            _check(rc, f"posix_spawn {path}")
            return SpawnedProcess(pid.value)
        finally:
            lib.posix_spawn_file_actions_destroy(ctypes.byref(actions))
    finally:
        lib.posix_spawnattr_destroy(ctypes.byref(attr))
