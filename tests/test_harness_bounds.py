"""``tests/_fixtures/bounds.py``: the harness bounds every test's wall clock and RSS growth.

Each case runs a child ``pytest`` on a generated file so the bound is exercised through
the real ``tests/conftest.py`` wiring (the hook wrapper, the markers, the message), not
by calling the watchdog directly. A child that HANGS here would itself be caught by the
parent's watchdog; the child budgets are seconds, the parent marker gives headroom.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._fixtures import bounds

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.slow_test(180, reason="each case is a child pytest run of a generated file")


def _run_child(tmp_path: Path, body: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    target = ROOT / "tests" / "hermes_cli" / f"test_bounds_child_{tmp_path.name.lower()}.py"
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(target), "-q", "-p", "no:cacheprovider", "-x", *extra],
            cwd=ROOT, capture_output=True, text=True, timeout=150,
            env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL,
        )
    finally:
        target.unlink(missing_ok=True)


def test_a_hung_test_fails_by_name_within_the_wall_clock(tmp_path, monkeypatch):
    out = _run_child(tmp_path, '''
        import time
        def test_hangs():
            while True:
                try:
                    time.sleep(0.01)
                except Exception:
                    pass
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "test_hangs ran past 2s of wall clock" in out.stdout
    assert "@pytest.mark.slow_test" in out.stdout
    assert "1 failed" in out.stdout


def test_a_blocking_syscall_is_interrupted_not_waited_out(tmp_path):
    out = _run_child(tmp_path, '''
        import threading
        def test_blocks():
            threading.Lock().acquire(); threading.Event().wait()  # never set
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == 1 and "test_blocks ran past 2s" in out.stdout
    assert "1 failed" in out.stdout


def test_runaway_allocation_fails_by_name_before_the_host_notices(tmp_path):
    """The t_69572750 shape: a list that grows forever. Stops at ~the growth cap."""
    out = _run_child(tmp_path, '''
        def test_allocates():
            blobs = []
            while True:
                blobs.append(bytearray(32 * 1024 * 1024))
    ''', "-o", "bounds_rss_growth_gb=0.25")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "test_allocates grew" in out.stdout and "limit 0.25 GiB" in out.stdout
    assert "@pytest.mark.big_memory" in out.stdout


def test_markers_raise_the_limits_and_need_a_reason(tmp_path):
    out = _run_child(tmp_path, '''
        import time, pytest
        @pytest.mark.slow_test(6, reason="deliberately sleeps past the 2 s test cap")
        def test_slow_but_marked():
            time.sleep(3)
        @pytest.mark.big_memory(0.6, reason="builds a 0.4 GiB buffer on purpose")
        def test_big_but_marked():
            blob = bytearray(400 * 1024 * 1024)
            assert blob
    ''', "-o", "bounds_wall_seconds=2", "-o", "bounds_rss_growth_gb=0.25")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "2 passed" in out.stdout

    out = _run_child(tmp_path, '''
        import pytest
        @pytest.mark.slow_test(600)
        def test_no_reason():
            pass
    ''')
    assert out.returncode != 0 and "needs reason=" in out.stdout + out.stderr


def test_except_exception_in_the_code_under_test_cannot_eat_the_stop(tmp_path):
    out = _run_child(tmp_path, '''
        import time
        def test_swallows():
            while True:
                try:
                    time.sleep(0.05)
                except Exception:
                    pass
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == 1 and "test_swallows ran past 2s" in out.stdout


def test_a_loop_that_swallows_base_exception_ends_the_process_by_name(tmp_path):
    """The only way left to stop it: the watchdog exits the pytest process, verdict first."""
    out = _run_child(tmp_path, '''
        import time
        def test_eats_everything():
            while True:
                try:
                    time.sleep(0.05)
                except BaseException:
                    pass
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == bounds._ESCALATE_EXIT_CODE, out.stdout + out.stderr
    assert "test_eats_everything ran past 2s" in out.stderr
    assert "swallowed the harness stop" in out.stderr


def test_a_passing_test_under_the_default_bounds_is_untouched(tmp_path):
    out = _run_child(tmp_path, '''
        def test_fine():
            assert sum(range(1000)) == 499500
    ''')
    assert out.returncode == 0 and "1 passed" in out.stdout


@pytest.mark.platforms("linux")
def test_rlimit_as_turns_a_runaway_into_memory_error_on_linux(tmp_path):
    out = _run_child(tmp_path, '''
        import resource
        def test_capped():
            soft, _ = resource.getrlimit(resource.RLIMIT_AS)
            assert soft == 1 * 1024 ** 3, soft
            blobs = []
            try:
                while True:
                    blobs.append(bytearray(64 * 1024 * 1024))
            except MemoryError:
                return
    ''', "-o", "bounds_rss_growth_gb=8", env={bounds._RLIMIT_ENV: "1"})
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.platforms("linux")
def test_rlimit_env_zero_disables_the_process_ceiling(tmp_path):
    """A soft rlimit is inherited, so lift this (parent) process's cap for the spawn."""
    import resource

    saved = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (saved[1], saved[1]))
    try:
        out = _run_child(tmp_path, '''
            import resource
            def test_uncapped():
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == resource.RLIM_INFINITY
        ''', env={bounds._RLIMIT_ENV: "0"})
    finally:
        resource.setrlimit(resource.RLIMIT_AS, saved)
    assert out.returncode == 0, out.stdout + out.stderr


def test_growth_budget_is_clamped_to_the_cgroup_ceiling(monkeypatch):
    gib = 1024 ** 3
    monkeypatch.setattr(bounds, "_cgroup_memory_max", lambda: None)
    assert bounds._clamp_to_cgroup(gib, 300 * 1024 ** 2) == gib
    monkeypatch.setattr(bounds, "_cgroup_memory_max", lambda: gib)
    assert bounds._clamp_to_cgroup(gib, 300 * 1024 ** 2) == gib - 300 * 1024 ** 2 - int(gib * 0.35)
    assert bounds._clamp_to_cgroup(gib, 2 * gib) == 16 * 1024 ** 2  # already over: fail at the first poll
    monkeypatch.setattr(bounds, "_cgroup_memory_max", lambda: 64 * gib)
    assert bounds._clamp_to_cgroup(gib, 300 * 1024 ** 2) == gib  # roomy cgroup: the budget stands


def test_marker_limits_are_read_from_the_item():
    class _Item:
        nodeid = "x::y"

        def __init__(self, marker):
            self._m = marker

        def get_closest_marker(self, name):
            return self._m

    class _M:
        def __init__(self, args, kwargs):
            self.args, self.kwargs = args, kwargs

    assert bounds._limit_from_marker(_Item(None), "slow_test", 60.0) == 60.0
    assert bounds._limit_from_marker(_Item(_M((120,), {"reason": "x"})), "slow_test", 60.0) == 120.0
    with pytest.raises(pytest.UsageError):
        bounds._limit_from_marker(_Item(_M((120,), {})), "slow_test", 60.0)
    with pytest.raises(pytest.UsageError):
        bounds._limit_from_marker(_Item(_M(("soon",), {"reason": "x"})), "slow_test", 60.0)
