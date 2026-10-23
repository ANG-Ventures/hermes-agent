"""Every test is bounded by the harness, not by its author's care.

2026-10-12 (t_69572750): ``tests/hermes_cli/test_kanban_pr_owner.py`` at a WIP tree
monkeypatched ``os.kill`` with a recorder that never raises. ``_dead_pid()`` is a
poll-until-``ProcessLookupError`` loop, so it never returned and the test process grew
to 34.6 GiB; ACE-AI (MemAvailable 35 GB -> 3 GB) hard-reset twice. Nothing in the run
bounded time or memory per test: the only ceiling was the per-FILE wall clock of
``scripts/run_tests_parallel.py``, which a bare ``pytest`` (kanban worker, test-gate,
a developer shell) never goes through.

This module is the one mechanism every pytest invocation in the repo inherits, because
``tests/conftest.py`` imports it. Three bounds, each a NAMED failure that points at the
test, not a dead host:

1. **Wall clock per test** (``_TEST_WALL_SECONDS``, 60 s; ``@pytest.mark.slow_test(N)``
   raises it for a named reason). A watchdog thread delivers ``HarnessWallClockExceeded``
   into the test thread with ``PyThreadState_SetAsyncExc`` (ends a busy loop between
   bytecodes) and sends the main thread ``SIGURG``, whose handler raises the same
   exception when a watchdog has fired (aborts a blocking ``sleep``/``recv``/``wait``/
   untimed ``Lock.acquire``: measured, the async exception alone never reaches an untimed
   acquire). It is NOT ``SIGALRM``: the tree uses ``SIGALRM``/``setitimer`` itself
   (``kanban_db._cwd_probe_budget``), and the two would clobber each other (verified:
   pytest-timeout's signal method turned 25 passing tests red in
   ``test_kanban_workspace_retention.py``). The exception derives from ``BaseException``
   so an ``except Exception`` in the code under test cannot eat it; a loop that swallows
   ``BaseException`` gets re-delivered every 0.5 s and, ``_ESCALATE_AFTER_SECONDS`` later,
   the watchdog writes the named verdict to fd 2 and ``os._exit(70)``s the process: the
   per-file runner reports that file red with the test named, and the host is untouched.

2. **Address-space ceiling per process** (``_PROCESS_RLIMIT_GB``, 6 GiB): on Linux
   ``RLIMIT_AS`` makes a runaway allocation raise ``MemoryError`` INSIDE the test instead
   of eating the host. macOS refuses to lower ``RLIMIT_AS``/``RLIMIT_DATA``/``RLIMIT_RSS``
   below the hard limit (``setrlimit`` -> EINVAL, measured on 26.7), so there the
   ceiling is rule 3 only. ``RLIMIT_DATA`` is NOT used on Linux: it caps ``brk``/private
   anonymous maps only and a 6 GiB cap let a 25 GiB ``bytearray`` loop run (measured);
   ``RLIMIT_AS`` caught it at the cap. A torch/CUDA import on ACE-AI under a 6 GiB AS
   cap reports ``cuda.is_available() == False`` (CUDA reserves address space), so a
   test that must initialise CUDA sets ``HERMES_TEST_RLIMIT_GB=0`` for its run; no
   test in this tree imports torch.

3. **Peak-RSS growth per test** (``_TEST_RSS_GROWTH_GB``, 1 GiB; ``@pytest.mark.
   big_memory(N)`` raises it): the watchdog samples ``psutil.Process().memory_info().rss``
   every ``_RSS_POLL_SECONDS`` while the test runs and fails it, by name, the moment
   growth over the test's starting RSS crosses the threshold. Polling (not a post-hoc
   ``getrusage`` delta) is what makes this a *stop*, not an autopsy: the incident test
   would have been failed at ~1 GiB, three seconds in, on every host. The sample costs
   ~2 us. Inside a cgroup with a numeric ``memory.max`` (test-gate's ``MemoryMax``, a CI
   container) the budget is clamped to what the cgroup leaves above the test's starting
   RSS minus a headroom (35 % of the cgroup, min 96 MiB, max 512 MiB: the poll interval plus the
   stop latency of a ~2 GB/s allocator), so the harness names the test BEFORE the kernel's OOM killer SIGKILLs the
   process (measured: a 1 GiB cgroup + the 1 GiB default budget = exit 137 with no
   verdict; clamped = the named failure in under a second).

This file is self-contained (stdlib + pytest; psutil only off-Linux) so other repos carry
a byte-identical copy as a plugin: ``pytest_plugins = ["pytest_bounds"]`` or ``-p``, via the
hook functions at the bottom. hermes-agent imports it into ``tests/conftest.py`` instead
(a non-root conftest cannot list ``pytest_plugins``), so those hooks are inert here.

Opt-outs are markers with a reason, never env vars (root AGENTS.md: behaviour is
config/markers, ``.env`` is secrets). The defaults are pytest ini values
(``bounds_wall_seconds``, ``bounds_rss_growth_gb``; override with ``-o`` for a probe, never
in pyproject to make one test pass). ``HERMES_TEST_RLIMIT_GB`` is the single env knob
and exists only because the rlimit is per PROCESS, decided before any test runs.
"""
from __future__ import annotations

import _thread
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

# The watchdog runs while the test body runs, so the test's monkeypatches are live around it.
# Tests in these trees replace ``threading.Thread`` with an inline class, patch
# ``Thread.start``/``Thread.join``/``Event.wait``, pin ``time.monotonic`` and mock
# ``builtins.open`` and ``os._exit``. Every primitive the watchdog uses is bound HERE, at import,
# before any fixture runs. It never looks one up through a module attribute at test time
# (Prism 6e9e5d3d21cc / bbb775203d64, t_4a3eba50).
_start_new_thread = _thread.start_new_thread
_allocate_lock = _thread.allocate_lock
_get_ident = _thread.get_ident
_monotonic = time.monotonic
_os_open, _os_read, _os_close, _os_write, _os_exit = os.open, os.read, os.close, os.write, os._exit
_pthread_kill = getattr(signal, "pthread_kill", None)
_SIGURG = getattr(signal, "SIGURG", None)
_set_async_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
_RLIMIT_AS_SAVED: tuple[int, int] | None = None  # (soft, hard) before configure() lowered it

# Defaults. Raise per test with the markers; never edit these to make one test pass.
_TEST_WALL_SECONDS = 60.0
_TEST_RSS_GROWTH_GB = 1.0
_PROCESS_RLIMIT_GB = 6.0
_RSS_POLL_SECONDS = 0.02  # the incident allocated at ~2 GB/s: 20 ms is ~40 MB of overshoot
_ESCALATE_AFTER_SECONDS = 3.0
_CGROUP_HEADROOM_MIN = 96 * 1024 ** 2
# Measured under a 1 GiB cgroup on ace-media (memory.current traced at 10 ms): detection
# lands one poll late and the test thread allocates another ~150 MB before the async
# exception stops it, so a 1 GiB cgroup needs ~0.35 GiB above the budget.
_CGROUP_HEADROOM_FRACTION = 0.35
# The overshoot is allocator speed x stop latency, not a share of the cgroup, so the fraction
# stops growing here. Uncapped, a 4 GiB ci-runner container (8 workers, ~630 MiB of /_work tmpfs)
# reserved 1.4 GiB and every test on a busy slice got the 16 MiB floor ("grew 0.02 GiB of RSS
# (limit 0.02 GiB)" on hundreds of unrelated tests, measured on ci-box, t_b56f9e1e).
_CGROUP_HEADROOM_MAX = 512 * 1024 ** 2
_CGROUP_RECHECK_SECONDS = 0.5  # re-read the cgroup tree's room this often while a test runs
_MIN_GROWTH = 16 * 1024 ** 2
_ESCALATE_EXIT_CODE = 70
# The watchdog that has fired and not yet been acknowledged by the hook wrapper; read by
# the SIGURG handler on the main thread.
_FIRED: list["_Watchdog"] = []
_MARK_SLOW = "slow_test"
_MARK_BIG_MEMORY = "big_memory"
_RLIMIT_ENV = "HERMES_TEST_RLIMIT_GB"

_GIB = 1024 ** 3


class HarnessWallClockExceeded(BaseException):
    """The harness stopped a test that ran past its wall-clock budget."""


class HarnessMemoryExceeded(BaseException):
    """The harness stopped a test whose RSS grew past its budget."""


def _limit_from_marker(item, name: str, default: float) -> float:
    marker = item.get_closest_marker(name)
    if marker is None:
        return default
    if not marker.args or not isinstance(marker.args[0], (int, float)):
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.{name}(<number>, reason=...) needs a numeric limit"
        )
    if not marker.kwargs.get("reason"):
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.{name} needs reason='why this test may exceed the default'"
        )
    return float(marker.args[0])


def _read(path: str) -> bytes:
    """Whole small file via the import-time ``os`` functions (a test may mock ``builtins.open``)."""
    fd = _os_open(path, os.O_RDONLY)
    try:
        return _os_read(fd, 65536)
    finally:
        _os_close(fd)


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _current_rss() -> int:
    """Resident set size now. ``/proc`` on Linux (no dependency, ~1 us), psutil elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            return int(_read("/proc/self/statm").split()[1]) * _PAGE_SIZE
        except (OSError, ValueError, IndexError):
            pass
    try:
        import psutil
    except ImportError:
        return 0  # no RSS source: the wall clock still bounds the test
    return psutil.Process().memory_info().rss


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return False
    return True


_CGROUP_ROOT = "/sys/fs/cgroup"


def _cgroup_dir() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        text = _read("/proc/self/cgroup").decode("utf-8", "replace")
    except OSError:
        return None
    rel = next((ln.split(":", 2)[2].strip() for ln in text.splitlines() if ln.startswith("0::")), None)
    return None if rel is None else (_CGROUP_ROOT + rel).rstrip("/") or _CGROUP_ROOT


def _cgroup_levels() -> list[str]:
    """This process's cgroup and every ancestor up to and including the mount root: a parent
    slice's ``memory.max`` binds as surely as our own (Prism e023de0647d5), and inside a
    cgroup-namespaced container ``/proc/self/cgroup`` is ``0::/`` while the container's limit
    sits at ``/sys/fs/cgroup/memory.max`` itself (Prism 6e9e5d3d21cc; measured in the fleet's
    ci-runner containers). On a bare host the root has no ``memory.max`` and is skipped by
    ``_level_max``."""
    base = _cgroup_dir()
    out: list[str] = []
    while base:
        out.append(base)
        if base.rstrip("/") == _CGROUP_ROOT:
            break
        base = os.path.dirname(base.rstrip("/"))
    return out


def _level_max(base: str) -> int | None:
    try:
        raw = _read(f"{base}/memory.max").decode().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _level_used(base: str) -> int | None:
    """``memory.current`` minus CLEAN page cache: the working set the OOM killer weighs, for the
    WHOLE cgroup (other pytest workers, children), not this process alone.

    Clean file pages are reclaimed before ``memory.max`` OOM-kills, active or not. Subtracting only
    ``inactive_file`` (t_123bafee) counted a CI runner's git/pip cache as used: under MGLRU the
    kernel reports it as ``active_file`` (measured on ci-box, 4 GiB containers: active_file ~3 GiB,
    inactive_file 0, anon ~150 MiB), so the clamp left no room above the 35 % headroom and every
    test got the 16 MiB floor ("grew 0.02 GiB of RSS (limit 0.02 GiB)" on unrelated tests).
    ``shmem`` (counted inside ``file``), dirty and writeback pages are not freely reclaimable and
    stay counted. So do ``unevictable`` pages (Prism 2d3bf87e12ba): clean file pages a sibling
    ``mlock()``ed sit in ``file`` but cannot be reclaimed before ``memory.max`` OOM-kills. The kernel
    does not split ``unevictable`` by type (mlocked anon and SHM_LOCKed shmem land there too), so the
    whole of it is subtracted from ``clean``: over-counting used is the safe direction here."""
    try:
        current = int(_read(f"{base}/memory.current").decode().strip())
    except (OSError, ValueError):
        return None
    stat: dict[str, int] = {}
    try:
        for ln in _read(f"{base}/memory.stat").decode().splitlines():
            key, _, val = ln.partition(" ")
            if key in ("file", "shmem", "file_dirty", "file_writeback", "unevictable", "inactive_file"):
                stat[key] = int(val)
    except (OSError, ValueError):
        pass
    if "file" in stat:
        clean = (stat["file"] - stat.get("shmem", 0) - stat.get("file_dirty", 0) - stat.get("file_writeback", 0)
                 - stat.get("unevictable", 0))
    else:  # no ``file`` row: fall back to what is surely reclaimable
        clean = stat.get("inactive_file", 0)
    return max(current - max(clean, 0), 0)


def _cgroup_room(own_rss: int) -> tuple[int, int] | None:
    """(bytes left before the tightest limited level's ``memory.max``, that level's limit), or None
    when no level has a numeric limit. A level whose usage is unreadable counts only this process."""
    best: tuple[int, int] | None = None
    for base in _cgroup_levels():
        limit = _level_max(base)
        if limit is None:
            continue
        used = max(own_rss, _level_used(base) or 0)
        room = limit - used
        if best is None or room - _headroom(limit) < best[0] - _headroom(best[1]):
            best = (room, limit)
    return best


def _headroom(limit: int) -> int:
    return min(max(_CGROUP_HEADROOM_MIN, int(limit * _CGROUP_HEADROOM_FRACTION)), _CGROUP_HEADROOM_MAX)


def _cgroup_memory_max() -> int | None:
    """The tightest numeric ``memory.max`` over this process's cgroup and its ancestors."""
    limits = [m for m in (_level_max(b) for b in _cgroup_levels()) if m is not None]
    return min(limits) if limits else None


def _clamp_to_cgroup(growth_bytes: int, start_rss: int) -> int:
    """Cap the growth budget at what the cgroup tree has left minus a headroom
    (Prism 69b22e334665, e023de0647d5). The watchdog re-reads it while the test runs
    (``_CGROUP_RECHECK_SECONDS``): siblings grow too (Prism 00c34a8dbc11)."""
    got = _cgroup_room(start_rss)
    if got is None:
        return growth_bytes
    room, limit = got
    return max(min(growth_bytes, room - _headroom(limit)), _MIN_GROWTH)


def _on_sigurg(_signum, _frame) -> None:
    """Raise the fired watchdog's exception inside whatever syscall the main thread is in."""
    if _FIRED:
        raise _FIRED[-1].fired[0]  # type: ignore[index]


def _interrupt(thread_ident: int, exc_type: type) -> None:
    """Raise ``exc_type`` in the test thread and unblock any syscall it sits in."""
    _set_async_exc(ctypes.c_ulong(thread_ident), ctypes.py_object(exc_type))
    if _SIGURG is not None and _pthread_kill is not None:
        try:
            _pthread_kill(thread_ident, _SIGURG)
        except (ProcessLookupError, OSError):
            pass


class _Watchdog:
    """One thread per test: wall clock + RSS growth, both ending in a named failure.

    Built on ``_thread`` locks and the import-time ``_monotonic``, never on ``threading.Thread``/
    ``Event`` or ``time.monotonic`` looked up at test time (see the module-top note)."""

    def __init__(self, item, wall_seconds: float, rss_growth_bytes: int) -> None:
        self.item = item
        self.wall_seconds = wall_seconds
        self.target = _get_ident()
        self.start_rss = _current_rss()
        self.rss_growth_bytes = _clamp_to_cgroup(rss_growth_bytes, self.start_rss)
        self.started = _monotonic()
        self._next_cgroup_check = self.started + _CGROUP_RECHECK_SECONDS
        self.fired: tuple[type, str] | None = None
        self._stop = _allocate_lock()  # held = keep running; released by stop()
        self._stop.acquire()
        self._done = _allocate_lock()  # held while _run runs; released on exit
        self._done.acquire()

    def start(self) -> None:
        _start_new_thread(self._main, ())

    def stop(self) -> None:
        if self._stop.locked():
            self._stop.release()
        if self._done.acquire(timeout=2.0):
            self._done.release()

    def _stopped(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True once stop() was called."""
        if self._stop.acquire(timeout=seconds):
            self._stop.release()
            return True
        return False

    def _main(self) -> None:
        try:
            self._run()
        finally:
            self._done.release()

    def _run(self) -> None:
        deadline = self.started + self.wall_seconds
        while not self._stopped(_RSS_POLL_SECONDS):
            now = _monotonic()
            try:
                rss = _current_rss()
            except Exception:  # psutil lost the process: nothing to bound
                rss = self.start_rss
            grown = rss - self.start_rss
            if now >= self._next_cgroup_check:
                self._next_cgroup_check = now + _CGROUP_RECHECK_SECONDS
                got = _cgroup_room(rss)
                if got is not None:  # what is left now, on top of what this test already grew
                    live = grown + got[0] - _headroom(got[1])
                    self.rss_growth_bytes = max(min(self.rss_growth_bytes, live), _MIN_GROWTH)
            if grown > self.rss_growth_bytes:
                self.fired = (
                    HarnessMemoryExceeded,
                    f"{self.item.nodeid} grew {grown / _GIB:.2f} GiB of RSS "
                    f"(limit {self.rss_growth_bytes / _GIB:.2f} GiB, {now - self.started:.1f}s in). "
                    f"Raise it only with @pytest.mark.{_MARK_BIG_MEMORY}(<GiB>, reason=...).",
                )
            elif now >= deadline:
                self.fired = (
                    HarnessWallClockExceeded,
                    f"{self.item.nodeid} ran past {self.wall_seconds:.0f}s of wall clock. "
                    f"Raise it only with @pytest.mark.{_MARK_SLOW}(<seconds>, reason=...).",
                )
            if self.fired is not None:
                _FIRED.append(self)
                first = _monotonic()
                _interrupt(self.target, self.fired[0])
                # A ``try: ... except BaseException: pass`` in the test swallows a
                # delivery; keep re-raising, then end the process by name.
                while not self._stopped(0.5):
                    if _monotonic() - first > _ESCALATE_AFTER_SECONDS:
                        self._escalate()
                    _interrupt(self.target, self.fired[0])
                return

    def _escalate(self) -> None:
        message = (
            f"\n{self.fired[1]}\nThe test swallowed the harness stop for "  # type: ignore[index]
            f"{_ESCALATE_AFTER_SECONDS:.0f}s (an `except BaseException: pass` loop?); "
            f"ending the pytest process with exit {_ESCALATE_EXIT_CODE} so the host survives.\n"
        )
        # pytest's fd capture has dup2'd a temp file onto fd 2 (and ``sys.__stderr__``
        # wraps fd 2); the capture manager keeps the real terminal fd in
        # ``targetfd_save``. Write there so the verdict reaches the runner's log.
        try:
            _os_write(_real_stderr_fd(self.item.config), message.encode("utf-8", "replace"))
        except OSError:
            pass
        _os_exit(_ESCALATE_EXIT_CODE)


def _real_stderr_fd(config) -> int:
    capman = config.pluginmanager.getplugin("capturemanager")
    try:
        saved = capman._global_capturing.err.targetfd_save  # type: ignore[union-attr]
    except AttributeError:
        return 2
    return saved if isinstance(saved, int) else 2


def _apply_process_rlimit() -> str | None:
    """Lower RLIMIT_AS to the cap once per process. Returns the reason it was not applied."""
    raw = os.environ.get(_RLIMIT_ENV)
    gib = _PROCESS_RLIMIT_GB if raw is None else float(raw)
    if gib <= 0:
        return f"{_RLIMIT_ENV}={raw} disables it"
    try:
        import resource
    except ImportError:
        return "no resource module on this platform"
    if not sys.platform.startswith("linux"):
        # macOS refuses to lower RLIMIT_AS/DATA/RSS below the hard limit (EINVAL,
        # measured on 26.7); only the per-test RSS watchdog bounds memory there.
        return f"{sys.platform} cannot lower RLIMIT_AS"
    cap = int(gib * _GIB)
    # Before the early return: a child pytest inherits the parent's soft cap and still needs it.
    _bound_malloc_arenas()
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft != resource.RLIM_INFINITY and soft <= cap:
        return None
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ValueError, OSError) as exc:
        return f"setrlimit(RLIMIT_AS) refused: {exc}"
    global _RLIMIT_AS_SAVED
    _RLIMIT_AS_SAVED = (soft, hard)
    _exempt_browser_children()
    return None


# glibc gives each new thread its own malloc arena, up to 8 x ncpu, and every arena reserves
# a 64 MiB heap of ADDRESS SPACE up front. On a 128-core host (ci-box: limit 1024 arenas) a
# test process with ~100 threads (plugin loaders, env-probe, executors) reserves ~6 GiB
# while its RSS is under 200 MiB: measured 2026-10-19, VmSize pinned at 6.00 GiB under the
# cap above, then "can't start new thread", "failed to map segment from shared object", and
# a SIGABRT core dump (t_485afb24). Uncapped, the same test peaked at 8.86 GiB VmSize. A
# Mac or a 32-core box never got near the cap, which is why it only failed off-box. Capping
# the arena count keeps the address-space ceiling about memory the test USES. 8 arenas,
# not 1: threads still spread across arenas, so lock contention barely changes.
_MALLOC_ARENA_MAX = 8
_M_ARENA_MAX = -8  # glibc <malloc.h>


def _bound_malloc_arenas() -> None:
    try:
        libc = ctypes.CDLL(None)
        mallopt = libc.mallopt
    except (OSError, AttributeError):  # not glibc (musl has no per-thread arenas)
        return
    mallopt(_M_ARENA_MAX, _MALLOC_ARENA_MAX)


# A soft RLIMIT_AS is inherited across fork/exec. Chromium reserves far more address space
# than it touches: chrome-headless-shell exits on SIGTRAP (rc -5) under a 6 or a 16 GiB AS
# cap and starts under 64, and Playwright's ``chromium.launch()`` fails at 6 (measured on
# ci-box, Linux 7.0, Chromium 1243, t_4a3eba50). The cap is for THIS interpreter, so a
# browser child (and the Playwright node driver, which spawns the browser) is started with
# the soft limit the process had before the harness lowered it (Prism c8672c954293).
# Not every browser death is the cap: ``rc -6`` (SIGABRT) with "Failed to move to new
# namespace" on stderr is the zygote sandbox refused by the container's seccomp profile
# (t_8351434e), identical with the harness off; measured in the ci-runner image 2026-10-13.
_BROWSER_EXES = frozenset({
    "chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "google-chrome-beta", "google-chrome-unstable", "chrome-headless-shell", "headless_shell",
    "msedge", "microsoft-edge", "firefox", "brave", "brave-browser",
})


def _is_browser_argv(args, executable) -> bool:
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        argv = [os.fsdecode(a) for a in args]
    exe = os.fsdecode(executable) if executable is not None else (argv[0].split()[0] if argv else "")
    name = os.path.basename(exe).lower()
    if name in _BROWSER_EXES or "chrome" in name or "chromium" in name:
        return True
    # Playwright: ``<site-packages>/playwright/driver/node <...>/package/cli.js run-driver``
    return name.startswith("node") and any("playwright" in a for a in argv[1:2])


# Runs in the CHILD between fork and the browser's exec: raise this process's own soft
# RLIMIT_AS (allowed up to the hard limit), then become the browser. The pytest process is
# never uncapped, so a concurrent spawn or allocation elsewhere keeps the cap (Prism 6ee6a42466dd).
# ``argv[2]`` is the executable as the caller gave it (``Popen(executable=)`` or ``argv[0]``),
# resolved HERE with ``execvp``: after ``_execute_child`` applied the child's ``cwd`` and ``env``,
# so a test that pins a browser through ``env['PATH']`` or a relative ``cwd`` gets that browser,
# not the parent's (Prism f368ed80a38b). ``-S`` and ``-I`` keep the stub free of site/user code.
_EXEC_UNCAPPED = (
    "import os, resource, sys\n"
    "soft = int(sys.argv[1]); hard = resource.getrlimit(resource.RLIMIT_AS)[1]\n"
    "resource.setrlimit(resource.RLIMIT_AS, (soft if hard == resource.RLIM_INFINITY else min(soft, hard), hard))\n"
    "os.execvp(sys.argv[2], sys.argv[3:])\n"
)
_SHELL_ARG = 10  # ``Popen._execute_child(self, args, executable, preexec_fn, ..., shell, ...)``: index of ``shell``


def uncapped_argv(argv) -> list[str]:
    """``argv`` started with the pre-harness soft RLIMIT_AS, for a child that is a whole other
    program the test drives, not code under the test's own budget. The cap is inherited through
    every exec, so it reaches grandchildren the browser match above never sees: the e2e upgrade
    sandbox runs install.sh / ``hermes update``, whose ``node`` builds start one V8 isolate per
    worker (~1 GiB of address space each, measured on ACE-AI: 16 workers peak at 16 GiB VmPeak)
    and died with \"JavaScript heap out of memory\" at a 75 MB heap under 6 GiB (t_b56f9e1e).
    Unchanged when the harness did not lower the cap."""
    argv = [os.fsdecode(a) for a in argv]
    if _RLIMIT_AS_SAVED is None or not argv:
        return argv
    return [sys.executable, "-I", "-S", "-c", _EXEC_UNCAPPED, str(_RLIMIT_AS_SAVED[0]), argv[0], *argv]


def _exempt_browser_children() -> None:
    """Spawn browser children (and the Playwright node driver) with the pre-harness soft
    RLIMIT_AS. Covers ``subprocess.Popen`` and asyncio subprocesses (both reach
    ``Popen._execute_child``): the argv is wrapped in ``_EXEC_UNCAPPED``, so the pid,
    process group and exit status are the browser's own. ``shell=True`` keeps its meaning:
    the trampoline execs ``/bin/sh -c <command>`` (or ``executable`` in the shell's place), as
    ``_execute_child`` itself would have (Prism acd9932c0af8)."""
    real = subprocess.Popen._execute_child
    if getattr(real, "_test_bounds_exempt", False):
        return

    def _execute_child(self, args, executable, *rest, **kw):
        if _RLIMIT_AS_SAVED is None or not _is_browser_argv(args, executable):
            return real(self, args, executable, *rest, **kw)
        shell = kw.get("shell") if "shell" in kw else (rest[_SHELL_ARG - 3] if len(rest) > _SHELL_ARG - 3 else False)
        if isinstance(args, (str, bytes, os.PathLike)):
            argv = [os.fsdecode(args)]
        else:
            argv = [os.fsdecode(a) for a in args]
        if shell:
            argv = ["/bin/sh", "-c", *argv]  # exactly what Popen does for shell=True on POSIX
        target = os.fsdecode(executable) if executable is not None else argv[0]
        soft = _RLIMIT_AS_SAVED[0]
        wrapped = [sys.executable, "-I", "-S", "-c", _EXEC_UNCAPPED, str(soft), target, *argv]
        if "shell" in kw:
            kw = {**kw, "shell": False}
        elif len(rest) > _SHELL_ARG - 3:
            rest = (*rest[:_SHELL_ARG - 3], False, *rest[_SHELL_ARG - 2:])
        return real(self, wrapped, sys.executable, *rest, **kw)

    _execute_child._test_bounds_exempt = True  # type: ignore[attr-defined]
    subprocess.Popen._execute_child = _execute_child  # type: ignore[method-assign]


def addoption(parser) -> None:
    """Called from ``tests/conftest.py::pytest_addoption``."""
    parser.addini("bounds_wall_seconds", "per-test wall-clock budget (s)", default=str(_TEST_WALL_SECONDS))
    parser.addini("bounds_rss_growth_gb", "per-test RSS growth budget (GiB)", default=str(_TEST_RSS_GROWTH_GB))


def _defaults(config) -> tuple[float, float]:
    return float(config.getini("bounds_wall_seconds")), float(config.getini("bounds_rss_growth_gb"))


def configure(config) -> None:
    """Called from ``tests/conftest.py::pytest_configure`` (hooks register only on the
    conftest module itself; this module is imported, not listed in ``pytest_plugins``)."""
    config.addinivalue_line(
        "markers",
        f"{_MARK_SLOW}(seconds, reason=...): this test may run longer than the "
        f"{_TEST_WALL_SECONDS:.0f}s per-test wall clock the harness enforces",
    )
    config.addinivalue_line(
        "markers",
        f"{_MARK_BIG_MEMORY}(gib, reason=...): this test may grow RSS by more than the "
        f"{_TEST_RSS_GROWTH_GB:.0f} GiB the harness enforces",
    )
    skipped = _apply_process_rlimit()
    config._hermes_test_bounds_rlimit = skipped  # read by the self-test; None = applied
    if not sys.platform.startswith("linux") and not _has_psutil():
        # No /proc and no psutil: the per-test RSS bound cannot sample, and off Linux the
        # RLIMIT_AS ceiling is not applied either. Say so instead of running unbounded
        # (Prism a7c89e6bfce2); the wall clock still applies.
        config.issue_config_time_warning(pytest.PytestConfigWarning(
            "test bounds: no RSS source on this host (install psutil); the per-test memory bound "
            "is OFF for this run, only the wall clock applies"), stacklevel=2)
    if _SIGURG is not None and threading.current_thread() is threading.main_thread():
        signal.signal(_SIGURG, _on_sigurg)


@contextlib.contextmanager
def bounded_call(item):
    """Wrap the ``pytest_runtest_call`` body in ``tests/conftest.py``.

    Wraps only the CALL phase: fixtures that build 20 s of state are not the hazard,
    and a watchdog across setup would fire inside another fixture's finaliser.
    """
    default_wall, default_growth = _defaults(item.config)
    wall = _limit_from_marker(item, _MARK_SLOW, default_wall)
    growth = _limit_from_marker(item, _MARK_BIG_MEMORY, default_growth)
    dog = _Watchdog(item, wall, int(growth * _GIB))
    dog.start()
    try:
        yield
        # The test returned normally. If the watchdog fired, the test caught the harness
        # stop (``pytest.raises(BaseException)``, ``except BaseException: return``) and
        # would otherwise pass over budget (Prism 65e6a2d109d2).
        dog.stop()
        if dog.fired is not None:
            raise pytest.fail.Exception(dog.fired[1], pytrace=False)
    except (HarnessWallClockExceeded, HarnessMemoryExceeded):
        if dog.fired is None:
            raise
        exc_type, message = dog.fired
        raise pytest.fail.Exception(message, pytrace=False) from None
    finally:
        dog.stop()
        if dog.fired is not None:
            if dog in _FIRED:
                _FIRED.remove(dog)
            # Cancel any still-pending async exception so it cannot land in teardown.
            _set_async_exc(ctypes.c_ulong(dog.target), None)


# ── Plugin surface for repos that load this file directly (hermes-home, ace-ai-ops) ──

def pytest_addoption(parser):  # noqa: D401 — pytest hook
    addoption(parser)


def pytest_configure(config):  # noqa: D401 — pytest hook
    configure(config)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):  # noqa: D401 — pytest hook
    with bounded_call(item):
        return (yield)
