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
   RSS minus a headroom (35 % of the cgroup, min 96 MiB: the poll interval plus the
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

import contextlib
import ctypes
import os
import signal
import sys
import threading
import time

import pytest

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


def _current_rss() -> int:
    """Resident set size now. ``/proc`` on Linux (no dependency, ~1 us), psutil elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", "rb") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    try:
        import psutil
    except ImportError:
        return 0  # no RSS source: the wall clock still bounds the test
    return psutil.Process().memory_info().rss


def _cgroup_memory_max() -> int | None:
    """This process's cgroup v2 ``memory.max`` in bytes, or None (no cgroup / unlimited)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            rel = next((ln.split(":", 2)[2].strip() for ln in fh if ln.startswith("0::")), None)
        if rel is None:
            return None
        with open(f"/sys/fs/cgroup{rel}/memory.max", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except (OSError, IndexError):
        return None
    return int(raw) if raw.isdigit() else None


def _clamp_to_cgroup(growth_bytes: int, start_rss: int) -> int:
    limit = _cgroup_memory_max()
    if limit is None:
        return growth_bytes
    headroom = max(_CGROUP_HEADROOM_MIN, int(limit * _CGROUP_HEADROOM_FRACTION))
    room = limit - start_rss - headroom
    return max(min(growth_bytes, room), 16 * 1024 ** 2)


def _on_sigurg(_signum, _frame) -> None:
    """Raise the fired watchdog's exception inside whatever syscall the main thread is in."""
    if _FIRED:
        raise _FIRED[-1].fired[0]  # type: ignore[index]


def _interrupt(thread_ident: int, exc_type: type) -> None:
    """Raise ``exc_type`` in the test thread and unblock any syscall it sits in."""
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_ident), ctypes.py_object(exc_type)
    )
    sigurg = getattr(signal, "SIGURG", None)
    if sigurg is not None and hasattr(signal, "pthread_kill"):
        try:
            signal.pthread_kill(thread_ident, sigurg)
        except (ProcessLookupError, OSError):
            pass


class _Watchdog:
    """One thread per test: wall clock + RSS growth, both ending in a named failure."""

    def __init__(self, item, wall_seconds: float, rss_growth_bytes: int) -> None:
        self.item = item
        self.wall_seconds = wall_seconds
        self.target = threading.get_ident()
        self.start_rss = _current_rss()
        self.rss_growth_bytes = _clamp_to_cgroup(rss_growth_bytes, self.start_rss)
        self.started = time.monotonic()
        self.fired: tuple[type, str] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"test-bounds:{item.name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        deadline = self.started + self.wall_seconds
        while not self._stop.wait(_RSS_POLL_SECONDS):
            now = time.monotonic()
            try:
                rss = _current_rss()
            except Exception:  # psutil lost the process: nothing to bound
                rss = self.start_rss
            grown = rss - self.start_rss
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
                first = time.monotonic()
                _interrupt(self.target, self.fired[0])
                # A ``try: ... except BaseException: pass`` in the test swallows a
                # delivery; keep re-raising, then end the process by name.
                while not self._stop.wait(0.5):
                    if time.monotonic() - first > _ESCALATE_AFTER_SECONDS:
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
            os.write(_real_stderr_fd(self.item.config), message.encode("utf-8", "replace"))
        except OSError:
            pass
        os._exit(_ESCALATE_EXIT_CODE)


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
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft != resource.RLIM_INFINITY and soft <= cap:
        return None
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ValueError, OSError) as exc:
        return f"setrlimit(RLIMIT_AS) refused: {exc}"
    return None


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
    if getattr(signal, "SIGURG", None) is not None and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGURG, _on_sigurg)


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
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(dog.target), None)


# ── Plugin surface for repos that load this file directly (hermes-home, ace-ai-ops) ──

def pytest_addoption(parser):  # noqa: D401 — pytest hook
    addoption(parser)


def pytest_configure(config):  # noqa: D401 — pytest hook
    configure(config)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):  # noqa: D401 — pytest hook
    with bounded_call(item):
        return (yield)
