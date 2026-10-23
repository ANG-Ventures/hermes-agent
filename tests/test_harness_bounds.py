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


@pytest.mark.platforms("linux")
def test_many_threads_do_not_spend_the_address_space_cap_on_malloc_arenas(tmp_path):
    """t_485afb24: glibc's per-thread arenas (64 MiB of AS each, up to 8 x ncpu) filled the 6 GiB
    cap on a 128-core host with ~100 threads and a <200 MiB RSS; the process then aborted."""
    out = _run_child(tmp_path, '''
        import threading
        def test_threads_malloc():
            gate = threading.Barrier(121, timeout=20)
            def work():
                blobs = [bytearray(4096) for _ in range(64)]  # small allocs land in this thread's arena
                gate.wait()
                del blobs
            ts = [threading.Thread(target=work) for _ in range(120)]
            for t in ts:
                t.start()
            gate.wait()
            for t in ts:
                t.join()
            vm = [l for l in open("/proc/self/status") if l.startswith("VmPeak")][0].split()[1]
            assert int(vm) < 3 * 1024 ** 2, vm  # kB; uncapped arenas on a many-core host reserve >= 7 GiB here
    ''', env={bounds._RLIMIT_ENV: "6"})
    assert out.returncode == 0, out.stdout + out.stderr


def _fake_cgroups(monkeypatch, levels):
    """levels: [(path, memory.max or None, working set or None)], innermost first."""
    monkeypatch.setattr(bounds, "_cgroup_levels", lambda: [p for p, _, _ in levels])
    monkeypatch.setattr(bounds, "_level_max", lambda b: {p: m for p, m, _ in levels}[b])
    monkeypatch.setattr(bounds, "_level_used", lambda b: {p: u for p, _, u in levels}[b])


def test_growth_budget_is_clamped_to_the_cgroup_ceiling(monkeypatch):
    gib = 1024 ** 3
    _fake_cgroups(monkeypatch, [])
    assert bounds._clamp_to_cgroup(gib, 300 * 1024 ** 2) == gib
    _fake_cgroups(monkeypatch, [("/cg/a", gib, None)])
    assert bounds._clamp_to_cgroup(gib, 300 * 1024 ** 2) == gib - 300 * 1024 ** 2 - int(gib * 0.35)
    assert bounds._clamp_to_cgroup(gib, 2 * gib) == 16 * 1024 ** 2  # already over: fail at the first poll
    _fake_cgroups(monkeypatch, [("/cg/a", 64 * gib, None)])
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


# ── Prism round on #1946 / #3957 (t_4a3eba50). Each case was red at a53d5d1afe / ce7856b09. ──

_PATCHED_STDLIB_CHILD = '''
    import builtins, os, threading, time, pytest

    class _InlineThread:
        """tests/tui_gateway/test_auto_continue.py's stand-in: start() runs the target inline."""
        def __init__(self, target=None, args=(), kwargs=None, **_):
            self._t, self._a, self._k = target, args, kwargs or {{}}
        def start(self):
            self._t(*self._a, **self._k)
        def join(self, timeout=None):
            pass

    @pytest.fixture()
    def patched(monkeypatch):
        {patch}

    def test_body_runs(patched):
        assert True
'''


@pytest.mark.parametrize("patch", [
    'monkeypatch.setattr(threading, "Thread", _InlineThread)',
    'monkeypatch.setattr(threading.Thread, "start", lambda self: None)',
    'monkeypatch.setattr(threading.Thread, "join", lambda self, timeout=None: None)',
    'monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)',
    'monkeypatch.setattr(time, "monotonic", lambda: 1e12)',
    'monkeypatch.setattr(builtins, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("open")))',
])
def test_a_fixture_patching_the_stdlib_does_not_reach_the_watchdog(tmp_path, patch):
    """Prism 6e9e5d3d21cc: ``turn_env`` in tests/tui_gateway swaps ``threading.Thread`` for an
    inline class, so ``dog.start()`` ran the poll loop on the test thread and the test never ran."""
    out = _run_child(tmp_path, _PATCHED_STDLIB_CHILD.format(patch=patch), "-o", "bounds_wall_seconds=4")
    assert out.returncode == 0 and "1 passed" in out.stdout, out.stdout + out.stderr


def test_a_frozen_clock_in_the_test_does_not_disable_the_wall_clock(tmp_path):
    """Prism bbb775203d64: a test pinning ``time.monotonic`` froze the watchdog's deadline."""
    out = _run_child(tmp_path, '''
        import time
        def test_frozen_and_hung(monkeypatch):
            real_sleep = time.sleep
            monkeypatch.setattr(time, "monotonic", lambda: 5.0)
            while True:
                real_sleep(0.01)
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == 1 and "test_frozen_and_hung ran past 2s" in out.stdout, out.stdout + out.stderr


def test_a_test_that_catches_the_harness_stop_and_returns_still_fails(tmp_path):
    """Prism 65e6a2d109d2: ``pytest.raises(BaseException)`` around a hang turned an
    exceeded budget into a pass."""
    out = _run_child(tmp_path, '''
        import time, pytest
        def test_swallows_once():
            with pytest.raises(BaseException):
                while True:
                    time.sleep(0.01)
    ''', "-o", "bounds_wall_seconds=2")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "test_swallows_once ran past 2s" in out.stdout and "1 failed" in out.stdout


def test_cgroup_room_counts_the_whole_cgroup_not_just_this_process(monkeypatch):
    """Prism 69b22e334665: 1 GiB cgroup, 0.8 GiB already used by siblings, this process 0.1 GiB.
    The room is what the cgroup has left, not ``limit - own RSS``."""
    gib = 1024 ** 3
    _fake_cgroups(monkeypatch, [("/cg/a", gib, int(0.8 * gib))])
    assert bounds._clamp_to_cgroup(gib, int(0.1 * gib)) == 16 * 1024 ** 2  # 1 - 0.8 - 0.35 < 0
    _fake_cgroups(monkeypatch, [("/cg/a", gib, int(0.3 * gib))])
    assert bounds._clamp_to_cgroup(gib, int(0.1 * gib)) == gib - int(0.3 * gib) - int(gib * 0.35)


def test_an_ancestor_cgroup_limit_binds(monkeypatch):
    """Prism e023de0647d5: the leaf is unlimited, the parent slice holds memory.max."""
    gib = 1024 ** 3
    _fake_cgroups(monkeypatch, [("/cg/slice/leaf", None, int(0.1 * gib)), ("/cg/slice", 2 * gib, int(1.2 * gib))])
    assert bounds._clamp_to_cgroup(gib, int(0.1 * gib)) == 2 * gib - int(1.2 * gib) - int(2 * gib * 0.35)


def test_a_namespaced_container_root_cgroup_is_a_level(monkeypatch):
    """Prism 6e9e5d3d21cc: inside a cgroup-namespaced container ``/proc/self/cgroup`` reads ``0::/``
    and the container's limit is ``/sys/fs/cgroup/memory.max`` itself (measured in the fleet's
    ci-runner containers). The root must be a level, and the host's limit-less root must not be."""
    monkeypatch.setattr(bounds.sys, "platform", "linux")
    monkeypatch.setattr(bounds, "_read", lambda path: b"0::/\n" if path == "/proc/self/cgroup" else b"")
    assert bounds._cgroup_levels() == ["/sys/fs/cgroup"]
    monkeypatch.setattr(bounds, "_read", lambda path: b"0::/user.slice/leaf\n" if path == "/proc/self/cgroup" else b"")
    assert bounds._cgroup_levels() == ["/sys/fs/cgroup/user.slice/leaf", "/sys/fs/cgroup/user.slice", "/sys/fs/cgroup"]
    monkeypatch.setattr(bounds, "_level_max", lambda base: 1024 ** 3 if base == "/sys/fs/cgroup" else None)
    assert bounds._cgroup_memory_max() == 1024 ** 3


def test_the_budget_shrinks_when_siblings_grow_mid_test(tmp_path):
    """Prism 00c34a8dbc11: the room is re-read while the test runs, not once at start."""
    out = _run_child(tmp_path, '''
        import time
        from tests._fixtures import bounds as B
        def test_squeezed(monkeypatch):
            gib = 1024 ** 3
            used = [0]
            monkeypatch.setattr(B, "_cgroup_room", lambda own: (8 * gib - used[0], 8 * gib))
            time.sleep(0.3)  # the start-time budget is the full 1 GiB default
            used[0] = 8 * gib  # then a sibling takes the whole cgroup
            blob = bytearray(256 * 1024 * 1024)  # well under 1 GiB, over what is left
            blob[::4096] = b"x" * len(blob[::4096])
            while True:
                time.sleep(0.05)
    ''', "-o", "bounds_wall_seconds=20")
    assert out.returncode == 1 and "test_squeezed grew" in out.stdout, out.stdout + out.stderr


@pytest.mark.platforms("linux")
def test_a_browser_child_does_not_inherit_the_address_space_cap(tmp_path):
    """Prism c8672c954293: Chromium dies (SIGTRAP) under a 6 GiB RLIMIT_AS. A browser child
    gets the pre-harness limit; every other child, and the test itself, keeps the cap."""
    import resource

    saved = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (saved[1], saved[1]))
    probe = tmp_path / "chrome-headless-shell"
    probe.write_text("#!/bin/sh\nulimit -v\n", encoding="utf-8")
    probe.chmod(0o755)
    other = tmp_path / "plain-tool"
    other.write_text("#!/bin/sh\nulimit -v\n", encoding="utf-8")
    other.chmod(0o755)
    try:
        out = _run_child(tmp_path, f'''
            import resource, subprocess
            def test_children():
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3
                browser = subprocess.run([{str(probe)!r}], capture_output=True, text=True).stdout.strip()
                plain = subprocess.run([{str(other)!r}], capture_output=True, text=True).stdout.strip()
                assert browser == "unlimited", browser
                assert plain == str(1024 ** 2), plain
                p = subprocess.Popen([{str(probe)!r}], stdout=subprocess.DEVNULL)
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3  # never uncapped, not even mid-spawn
                assert p.wait() == 0
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3
        ''', env={bounds._RLIMIT_ENV: "1"})
    finally:
        resource.setrlimit(resource.RLIMIT_AS, saved)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.platforms("linux")
def test_a_driven_program_and_its_descendants_run_without_the_address_space_cap(tmp_path):
    """t_b56f9e1e: the e2e upgrade sandbox runs install.sh -> node; a node build with one V8
    isolate per worker needs more than 6 GiB of address space. ``uncapped_argv`` lifts the cap
    for that program and everything it execs; the test process and its other children keep it."""
    import resource

    saved = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (saved[1], saved[1]))
    try:
        out = _run_child(tmp_path, '''
            import resource, subprocess
            from tests._fixtures.bounds import uncapped_argv
            def test_driven():
                grandchild = ["/bin/sh", "-c", "/bin/sh -c 'ulimit -v'"]
                assert subprocess.run(uncapped_argv(grandchild), capture_output=True, text=True).stdout.strip() == "unlimited"
                assert subprocess.run(grandchild, capture_output=True, text=True).stdout.strip() == str(1024 ** 2)
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3
        ''', env={bounds._RLIMIT_ENV: "1"})
    finally:
        resource.setrlimit(resource.RLIMIT_AS, saved)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.platforms("linux")
def test_the_browser_exemption_resolves_through_the_child_env_and_keeps_shell_semantics(tmp_path):
    """Prism f368ed80a38b: a browser pinned through ``env['PATH']`` (or a relative ``cwd``) must be
    THAT browser, not the parent PATH's. Prism acd9932c0af8: ``shell=True`` must still run the
    command through ``/bin/sh -c``. Both go through the trampoline, uncapped."""
    import resource

    saved = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (saved[1], saved[1]))
    parent_bin = tmp_path / "parent-bin"
    child_bin = tmp_path / "child-bin"
    for d, tag in ((parent_bin, "PARENT"), (child_bin, "CHILD")):
        d.mkdir()
        exe = d / "google-chrome"
        exe.write_text(f"#!/bin/sh\necho {tag} $(ulimit -v) $PWD\n", encoding="utf-8")
        exe.chmod(0o755)
    try:
        out = _run_child(tmp_path, f'''
            import os, resource, subprocess
            def test_child_env_wins():
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3
                env = {{**os.environ, "PATH": {str(child_bin)!r} + os.pathsep + os.environ["PATH"]}}
                got = subprocess.run(["google-chrome"], capture_output=True, text=True, env=env, cwd={str(child_bin)!r}).stdout.split()
                assert got == ["CHILD", "unlimited", {str(child_bin)!r}], got
                # relative executable resolved against the child's cwd
                got = subprocess.run(["./google-chrome"], capture_output=True, text=True, cwd={str(child_bin)!r}).stdout.split()
                assert got[:2] == ["CHILD", "unlimited"], got
                # shell=True: the string is a shell command, with shell syntax honoured
                got = subprocess.run("google-chrome --flag && echo SHELL-OK", shell=True, capture_output=True, text=True, env=env).stdout.split()
                assert got[0] == "CHILD" and got[1] == "unlimited" and got[-1] == "SHELL-OK", got
                assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1024 ** 3
        ''', env={bounds._RLIMIT_ENV: "1", "PATH": str(parent_bin) + os.pathsep + os.environ["PATH"]})
    finally:
        resource.setrlimit(resource.RLIMIT_AS, saved)
    assert out.returncode == 0, out.stdout + out.stderr


def test_a_suite_with_its_own_pytest_ini_still_gets_the_bounds(tmp_path):
    """Prism 7f79b8c956d0: ``cd optional-skills/creative/comfyui/tests && pytest`` selects that
    directory's pytest.ini, the root conftest is never loaded, and nothing would bound the run.
    That conftest registers the harness itself; a 3 s sleep under a 1 s budget must fail by name."""
    nested = ROOT / "optional-skills" / "creative" / "comfyui" / "tests"
    probe_dir = nested.parent / f"tests_bounds_probe_{tmp_path.name.lower()}"
    probe_dir.mkdir()
    try:
        for name in ("conftest.py", "pytest.ini"):
            (probe_dir / name).write_bytes((nested / name).read_bytes())
        (probe_dir / "test_probe.py").write_text("import time\ndef test_slow():\n    time.sleep(3)\n", encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "test_probe.py", "-q", "-p", "no:cacheprovider", "-o", "bounds_wall_seconds=1"],
            cwd=probe_dir, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
    finally:
        import shutil

        shutil.rmtree(probe_dir, ignore_errors=True)
    assert out.returncode == 1 and "test_slow ran past 1s of wall clock" in out.stdout, out.stdout + out.stderr


def test_browser_argv_detection():
    assert bounds._is_browser_argv(["/usr/bin/google-chrome", "--headless"], None)
    assert bounds._is_browser_argv(["/x/chromium_headless_shell-1243/chrome-linux/headless_shell"], None)
    assert bounds._is_browser_argv(["/sp/playwright/driver/node", "/sp/playwright/driver/package/cli.js", "run-driver"], None)
    assert not bounds._is_browser_argv(["node", "-e", "1"], None)
    assert not bounds._is_browser_argv([sys.executable, "-m", "pytest"], None)
    assert not bounds._is_browser_argv("git status", None)
