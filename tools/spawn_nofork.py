"""Popen without fork_exec where fork copies the parent (t_afc525a2).

On macOS ``_posixsubprocess.fork_exec`` does a real fork() of the whole multi-GB
gateway with the GIL held. There this module starts the child with ONE libc
``posix_spawn`` call through ctypes (which also releases the GIL for the call).
Apple's extensions do in the kernel what ``Popen(close_fds=True, cwd=,
start_new_session=)`` does in its forked child: POSIX_SPAWN_CLOEXEC_DEFAULT closes
every descriptor no file action names, ``addinherit_np`` keeps the parent's 0/1/2,
``adddup2`` places the pipe ends, ``addchdir`` enters the cwd, POSIX_SPAWN_SETSID
starts the session and POSIX_SPAWN_SETSIGDEF resets what ``restore_signals`` resets.
Nothing runs between the spawn call and the target: it gets exactly the argv and
environment it was given, and launch errors come back from the call as errno.

Off macOS fork_exec uses vfork and copies nothing: callers get plain Popen there. A
missing binding or a failed attribute setup means plain Popen for the rest of the
process (logged once).
"""
from __future__ import annotations

import ctypes
import errno
import functools
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Platforms where fork_exec copies the parent's address space under the GIL.
_FORK_COPIES_PARENT = sys.platform == "darwin"
_FORK_ONLY_KWARGS = frozenset({
    "preexec_fn", "pass_fds", "close_fds", "start_new_session", "process_group",
    "user", "group", "extra_groups", "umask", "shell", "executable", "startupinfo",
    "creationflags", "restore_signals",
})
# <sys/spawn.h>, macOS SDK 26. posix_spawnattr_setflags takes a short.
_POSIX_SPAWN_SETPGROUP = 0x0002
_POSIX_SPAWN_SETSIGDEF = 0x0004
_POSIX_SPAWN_SETSID = 0x0400
_POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000
# What Popen(restore_signals=True) resets to SIG_DFL in the child.
_RESET_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                       if s is not None)


class _SetupFailed(Exception):
    """An attribute or file-action call failed before posix_spawn itself ran."""


class _Libc:
    """libSystem's posix_spawn family; signatures from <spawn.h> (macOS SDK 26)."""

    def __init__(self, libc):
        handle = ctypes.c_void_p  # posix_spawnattr_t / posix_spawn_file_actions_t: void *
        ptr = ctypes.POINTER

        def fn(name, *argtypes):
            f = getattr(libc, name)
            f.argtypes, f.restype = list(argtypes), ctypes.c_int
            return f

        self.handle = handle
        self.attr_init = fn("posix_spawnattr_init", ptr(handle))
        self.attr_destroy = fn("posix_spawnattr_destroy", ptr(handle))
        self.attr_setflags = fn("posix_spawnattr_setflags", ptr(handle), ctypes.c_short)
        # sigset_t is __uint32_t on Darwin.
        self.attr_setsigdefault = fn("posix_spawnattr_setsigdefault", ptr(handle), ptr(ctypes.c_uint32))
        self.attr_setpgroup = fn("posix_spawnattr_setpgroup", ptr(handle), ctypes.c_int32)
        self.fa_init = fn("posix_spawn_file_actions_init", ptr(handle))
        self.fa_destroy = fn("posix_spawn_file_actions_destroy", ptr(handle))
        self.fa_dup2 = fn("posix_spawn_file_actions_adddup2", ptr(handle), ctypes.c_int, ctypes.c_int)
        self.fa_inherit = fn("posix_spawn_file_actions_addinherit_np", ptr(handle), ctypes.c_int)
        try:  # macOS 26 name; the _np spelling is deprecated there
            self.fa_chdir = fn("posix_spawn_file_actions_addchdir", ptr(handle), ctypes.c_char_p)
        except AttributeError:
            self.fa_chdir = fn("posix_spawn_file_actions_addchdir_np", ptr(handle), ctypes.c_char_p)
        self.spawn = fn("posix_spawn", ptr(ctypes.c_int32), ctypes.c_char_p, ptr(handle), ptr(handle),
                        ptr(ctypes.c_char_p), ptr(ctypes.c_char_p))


def _load_libc():
    return ctypes.CDLL(None)


_UNRESOLVED = object()
_libc: Any = _UNRESOLVED  # a _Libc, or None once unavailable/disabled
_libc_lock = threading.Lock()


def _bindings() -> "_Libc | None":
    global _libc
    if _libc is _UNRESOLVED:
        with _libc_lock:
            if _libc is _UNRESOLVED:
                try:
                    _libc = _Libc(_load_libc())
                except Exception as exc:  # noqa: BLE001 - any binding problem means plain Popen
                    _libc = None
                    logger.warning("spawn_nofork: libc posix_spawn bindings unavailable (%s); using Popen", exc)
    return _libc


def _disable(reason: str) -> None:
    global _libc
    with _libc_lock:
        if _libc is not None:
            _libc = None
            logger.warning("spawn_nofork: %s; using Popen for the rest of this process", reason)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _native_spawn(libc: _Libc, executable: bytes, argv: list, envp: list, cwd: "bytes | None",
                  new_session: bool, dup2s: list, pgroup: int = -1, keep: Sequence[int] = ()) -> tuple:
    """One posix_spawn call -> (return code, pid). _SetupFailed if setup fails before it.

    ``pgroup`` >= 0 is Popen's ``process_group``: setpgid(0, pgroup) in the child.
    ``keep`` is Popen's ``pass_fds``: descriptors (all > 2) the child inherits as they are."""
    attr, actions, pid = libc.handle(), libc.handle(), ctypes.c_int32(0)
    if libc.attr_init(ctypes.byref(attr)) != 0:
        raise _SetupFailed("posix_spawnattr_init")
    try:
        if libc.fa_init(ctypes.byref(actions)) != 0:
            raise _SetupFailed("posix_spawn_file_actions_init")
        try:
            sigs = ctypes.c_uint32(0)
            for sig in _RESET_SIGNALS:
                sigs.value |= 1 << (sig - 1)
            flags = _POSIX_SPAWN_SETSIGDEF | _POSIX_SPAWN_CLOEXEC_DEFAULT
            if new_session:
                flags |= _POSIX_SPAWN_SETSID
            steps = [("setsigdefault", libc.attr_setsigdefault(ctypes.byref(attr), ctypes.byref(sigs)))]
            if pgroup >= 0:
                flags |= _POSIX_SPAWN_SETPGROUP
                steps.append(("setpgroup", libc.attr_setpgroup(ctypes.byref(attr), pgroup)))
            steps.append(("setflags", libc.attr_setflags(ctypes.byref(attr), flags)))
            for fd, target in dup2s:
                steps.append(("adddup2", libc.fa_dup2(ctypes.byref(actions), fd, target)))
            redirected = {target for _, target in dup2s}
            for fd in keep:
                steps.append(("addinherit_np", libc.fa_inherit(ctypes.byref(actions), fd)))
            # Popen leaves an unredirected standard fd as it is: an inheritable one reaches the
            # child, a close-on-exec one is closed by the exec. CLOEXEC_DEFAULT closes every fd
            # not named here, so name only the inheritable ones.
            for fd in (0, 1, 2):
                if fd not in redirected and _is_open(fd) and os.get_inheritable(fd):
                    steps.append(("addinherit_np", libc.fa_inherit(ctypes.byref(actions), fd)))
            if cwd is not None:
                steps.append(("addchdir", libc.fa_chdir(ctypes.byref(actions), cwd)))
            failed = [name for name, rc in steps if rc != 0]
            if failed:
                raise _SetupFailed(", ".join(failed))
            c_argv = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
            c_envp = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
            rc = libc.spawn(ctypes.byref(pid), executable, ctypes.byref(actions), ctypes.byref(attr),
                            c_argv, c_envp)
            return rc, pid.value
        finally:
            libc.fa_destroy(ctypes.byref(actions))
    finally:
        libc.attr_destroy(ctypes.byref(attr))


def _encode(value, what: str) -> bytes:
    data = os.fsencode(value)
    if b"\0" in data:
        raise ValueError(f"embedded null byte in {what}")
    return data


def eligible(args: Any, kwargs: dict) -> bool:
    if not _FORK_COPIES_PARENT or os.name != "posix":
        return False
    if os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0"):
        return False
    if not isinstance(subprocess.Popen, type) or _FORK_ONLY_KWARGS.intersection(kwargs):
        return False
    # posix_spawn does no PATH search, and a relative path with a cwd is resolved
    # against different directories by Popen and posix_spawn: absolute only.
    if isinstance(args, (str, bytes, os.PathLike)) or not args or not os.path.isabs(os.fsdecode(args[0])):
        return False
    return _bindings() is not None


# Popen.__init__ calls _execute_child with the POSIX ABI positionally (3.11+).
_EXECUTE_CHILD_PARAMS = (
    "args", "executable", "preexec_fn", "close_fds", "pass_fds", "cwd", "env",
    "startupinfo", "creationflags", "shell", "p2cread", "p2cwrite", "c2pread",
    "c2pwrite", "errread", "errwrite", "restore_signals", "gid", "gids", "uid",
    "umask", "start_new_session", "process_group",
)
_PIPE_PARAMS = ("p2cread", "p2cwrite", "c2pread", "c2pwrite", "errread", "errwrite")


def _plan(bound: dict):
    """(argv, executable candidates, error filename, pgroup, pass_fds) for posix_spawn, or None.

    None means "not expressible as one posix_spawn with Popen's exact semantics": the caller
    replays the original fork_exec path with the original arguments. The argv / shell /
    executable / PATH handling copies ``Popen._execute_child`` (POSIX) line for line.
    """
    if (bound["preexec_fn"] is not None or not bound["close_fds"]
            or not bound["restore_signals"] or bound["gid"] is not None or bound["gids"] is not None
            or bound["uid"] is not None or bound["umask"] >= 0):
        return None
    pgroup = bound["process_group"]
    if pgroup is None:
        pgroup = -1
    if pgroup >= 0 and bound["start_new_session"]:
        return None  # CPython orders setsid() before setpgid(); posix_spawn does not promise that
    # Pipes can occupy 0/1/2 when standard descriptors are closed. Let CPython handle
    # those dup-order corner cases with its original flags.
    if any(0 <= bound[n] <= 2 for n in _PIPE_PARAMS):
        return None
    # pass_fds: kept open and inheritable in the child (addinherit_np). A standard descriptor
    # interacts with the dup2s, and a closed one is an error only one side reports: CPython's.
    keep = tuple(sorted({int(fd) for fd in bound["pass_fds"]}))
    if any(fd <= 2 or not _is_open(fd) for fd in keep):
        return None
    args, executable, shell = bound["args"], bound["executable"], bound["shell"]
    if isinstance(args, (str, bytes)):
        args = [args]
    elif isinstance(args, os.PathLike):
        if shell:
            return None  # CPython raises TypeError
        args = [args]
    else:
        args = list(args)
    if shell:
        args = ["/bin/sh", "-c"] + args
        if executable:
            args[0] = executable
    if not args:
        return None
    if executable is None:
        executable = args[0]
    exe = os.fsencode(executable)
    if os.path.dirname(exe):
        candidates = [exe]
    else:  # CPython builds this list in the parent too (os._execvpe order)
        candidates = [os.path.join(os.fsencode(d), exe) for d in os.get_exec_path(bound["env"])]
    # posix_spawn resolves a relative path against the parent's cwd, fork_exec against the
    # child's (after chdir): absolute only.
    if not candidates or not all(os.path.isabs(c) for c in candidates):
        return None
    return args, candidates, executable, pgroup, keep


def _execute_child_nofork(popen: subprocess.Popen, args: tuple, kwargs: dict, fallback) -> None:
    """Popen._execute_child via posix_spawn where Popen's semantics allow it, else ``fallback``."""
    libc = _bindings()
    if (libc is None or len(args) > len(_EXECUTE_CHILD_PARAMS)
            or os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0")):
        return fallback(*args, **kwargs)
    bound = dict(zip(_EXECUTE_CHILD_PARAMS, args))
    bound.update(kwargs)
    if set(bound) != set(_EXECUTE_CHILD_PARAMS):
        return fallback(*args, **kwargs)
    # Planning reads args and pass_fds; an iterator would reach the fallback consumed.
    # Materialize both ONCE (as CPython's own list(args) / set(pass_fds) would) and hand
    # the same values to the native path and to the fallback.
    if not isinstance(bound["args"], (str, bytes, os.PathLike)):
        bound["args"] = list(bound["args"])
    bound["pass_fds"] = tuple(bound["pass_fds"])
    args, kwargs = tuple(bound[n] for n in _EXECUTE_CHILD_PARAMS), {}
    try:
        plan = _plan(bound)
    except Exception:  # noqa: BLE001 - anything odd is CPython's to handle (and to raise)
        plan = None
    if plan is None:
        return fallback(*args, **kwargs)
    argv_in, candidates, orig_executable, pgroup, keep = plan
    cwd, env = bound["cwd"], bound["env"]
    sys.audit("subprocess.Popen", orig_executable, argv_in, cwd, env)
    argv = [_encode(a, "argument") for a in argv_in]
    if env is None:  # as CPython's own posix_spawn branch (Popen._posix_spawn): os.environ
        envp = [k + b"=" + v for k, v in os.environb.items()]
    else:
        envp = []
        for key, value in env.items():
            key = _encode(key, "environment variable name")
            if b"=" in key:
                raise ValueError("illegal environment variable name")
            envp.append(key + b"=" + _encode(value, "environment variable"))
    c_cwd = _encode(cwd, "cwd") if cwd is not None else None
    handles = [bound[n] for n in _PIPE_PARAMS]
    p2cread, _, _, c2pwrite, _, errwrite = handles
    dup2s = [(fd, target) for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)) if fd != -1]
    def cwd_unusable() -> bool:
        return cwd is not None and not (os.path.isdir(cwd) and os.access(cwd, os.X_OK))

    saved = rc = 0
    for candidate in candidates:
        if b"\0" in candidate:
            raise ValueError("embedded null byte")
        if len(candidates) > 1:  # PATH search: a missing entry is ENOENT/ENOTDIR either way
            try:
                os.stat(candidate)
            except (FileNotFoundError, NotADirectoryError) as exc:
                rc = exc.errno
                continue
            except OSError:
                pass  # the exec reports it (EACCES, ELOOP, ...)
        try:
            rc, pid = _native_spawn(libc, candidate, argv, envp, c_cwd,
                                    bool(bound["start_new_session"]), dup2s, pgroup, keep)
        except _SetupFailed as exc:
            _disable(f"posix_spawn setup failed ({exc})")
            return fallback(*args, **kwargs)
        if rc == 0:
            popen.pid = pid
            popen._child_created = True
            popen._close_pipe_fds(*handles)
            return None
        if cwd_unusable():
            # As Popen ("noexec:chdir"): the child chdirs before any exec, and names the cwd.
            raise OSError(rc, os.strerror(rc), cwd)
        # _posixsubprocess child_exec: keep going on ENOENT/ENOTDIR, remember the first other errno.
        if rc not in (errno.ENOENT, errno.ENOTDIR) and not saved:
            saved = rc
    if cwd_unusable():
        return fallback(*args, **kwargs)  # no spawn was attempted; CPython names the cwd's errno
    rc = saved or rc
    raise OSError(rc, os.strerror(rc), orig_executable)


@functools.lru_cache(maxsize=4)
def _popen_class(base):
    # Lazy: tools are also imported with Popen replaced by a recording function.
    # Instrumentation may wrap _execute_child with *rest/**kw, hiding its signature;
    # never bind to that wrapper. Keep its original argv for the ordinary-Popen fallback.
    class NoForkPopen(base):
        def _execute_child(self, *args, **kwargs):
            return _execute_child_nofork(self, args, kwargs, super()._execute_child)

    return NoForkPopen


# -- process-wide hook (t_aa2e2d4e) -------------------------------------------
_hook_lock = threading.Lock()
_original_execute_child: Any = None
_hooked_class: Any = None


def _hooked_execute_child(self, *args, **kwargs):
    original = _original_execute_child
    if original is None:  # uninstalled while a bound reference to this function survived
        return _hooked_class.__dict__["_execute_child"](self, *args, **kwargs)
    return _execute_child_nofork(self, args, kwargs, functools.partial(original, self))


def _stdlib_popen() -> Any:
    """The class that defines CPython's ``_execute_child`` (``subprocess.Popen`` may be a subclass)."""
    if not isinstance(subprocess.Popen, type):
        return None
    for klass in subprocess.Popen.__mro__:
        if "_execute_child" in klass.__dict__ and klass.__module__ == "subprocess":
            return klass
    return None


def install_process_hook() -> bool:
    """Route every ``subprocess.Popen`` in this process through posix_spawn where it can be.

    The gateway calls this once at start. It replaces only CPython's ``Popen._execute_child``:
    ``Popen``/``run``/``check_output`` stay the same objects, every keyword reaches
    ``Popen.__init__`` unchanged (pipes, text mode, encoding, timeouts, communicate, kill), a
    subclass that overrides ``_execute_child`` still reaches it through ``super()``, and a
    launch :func:`_plan` cannot express exactly (preexec_fn, close_fds=False, uid/gid/umask,
    restore_signals=False, relative paths, a cwd that cannot be entered, pipes on 0/1/2, ...)
    calls the original method with the original arguments. True when the hook is installed;
    a no-op (False) off macOS, where fork_exec already uses vfork.
    """
    global _original_execute_child, _hooked_class
    if not _FORK_COPIES_PARENT or os.name != "posix" or _bindings() is None:
        return False
    with _hook_lock:
        if _original_execute_child is None:
            target = _stdlib_popen()
            if target is None:
                return False
            _hooked_class, _original_execute_child = target, target.__dict__["_execute_child"]
            target._execute_child = _hooked_execute_child
    return True


def uninstall_process_hook() -> None:
    global _original_execute_child
    with _hook_lock:
        if _original_execute_child is not None:
            if _hooked_class.__dict__.get("_execute_child") is _hooked_execute_child:
                _hooked_class._execute_child = _original_execute_child
            _original_execute_child = None


def process_hook_installed() -> bool:
    return _original_execute_child is not None


def spawn_popen(args: Sequence[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """Popen(close_fds=True[, start_new_session]); no fork_exec where fork copies the parent."""
    cls = _popen_class(subprocess.Popen) if eligible(args, kwargs) else subprocess.Popen
    return cls(args, start_new_session=new_session, **kwargs)


def run_nofork(args: Sequence[str], *, input: "bytes | str | None" = None, timeout: "float | None" = None,
               **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run through spawn_popen where eligible; subprocess.run itself otherwise."""
    if not eligible(args, kwargs):
        return subprocess.run(args, input=input, timeout=timeout, **kwargs)
    check = kwargs.pop("check", False)
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with spawn_popen(args, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            # subprocess.run's POSIX path: communicate already put the output so far on
            # the exception. Communicating again would block until every descendant
            # holding the pipes exits.
            proc.kill()
            proc.wait()
            raise
        except BaseException:
            proc.kill()
            raise
        result = subprocess.CompletedProcess(args, proc.poll(), stdout, stderr)
        if check:
            result.check_returncode()
        return result
