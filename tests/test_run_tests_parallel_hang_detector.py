"""run_tests_parallel: the per-file kill is a HANG detector, not a slowness budget.

Incident (t_949655a6, 2026-09-25): merge-group slices on a quota-capped
self-hosted runner reached 93 % of ``tests/test_hermes_state_core.py`` with
zero failures and were SIGKILL'd at the 300 s wall ceiling, ejecting the
whole merge group. The file was slow, not hung -- it was streaming a PASSED
line every few seconds. A hang is the ABSENCE of progress; that is the only
thing the kill may key on.
"""
from __future__ import annotations

import textwrap
import time
from pathlib import Path

from scripts import run_tests_parallel as runner


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body))
    return f


def test_slow_but_progressing_file_survives_a_short_wall_ceiling(tmp_path: Path) -> None:
    """Six tests at ~0.6 s each (~4 s total) with a 2 s wall value that the
    OLD runner treated as a hard kill: the new runner keys on progress, so the
    wall value is only reached if the file stops emitting test lines."""
    probe = _write(tmp_path, "test_slow_probe.py", """
        import time
        import pytest

        @pytest.mark.parametrize("i", range(6))
        def test_slow(i):
            time.sleep(0.6)
    """)
    t0 = time.monotonic()
    _file, rc, output, summary, _wall = runner._run_one_file(
        probe, ["-v", "-p", "no:cacheprovider"], tmp_path,
        file_timeout=60, idle_timeout=5,
    )
    elapsed = time.monotonic() - t0
    assert rc == 0, output
    assert summary.get("passed") == 6, summary
    assert not summary.get("timed_out"), summary
    assert elapsed >= 3.0  # it really ran the slow tests


def test_silent_hang_is_killed_at_the_idle_window_not_the_wall(tmp_path: Path) -> None:
    """One test that sleeps 60 s emits nothing: dies at idle_timeout (3 s),
    long before the 60 s wall, and the verdict names it a HANG."""
    probe = _write(tmp_path, "test_hang_probe.py", """
        import time

        def test_ok():
            pass

        def test_hangs():
            time.sleep(60)
    """)
    t0 = time.monotonic()
    _file, rc, output, summary, _wall = runner._run_one_file(
        probe, ["-v", "-p", "no:cacheprovider"], tmp_path,
        file_timeout=60, idle_timeout=3,
    )
    elapsed = time.monotonic() - t0
    assert rc == 124, output
    assert summary.get("timed_out") == 1 and summary.get("idle_killed") == 1, summary
    assert elapsed < 30, f"idle kill took {elapsed:.1f}s -- fell through to the wall?"
    verdict = runner._format_timeout_verdict(output, summary)
    assert verdict.startswith("HUNG: no test progress for 3s"), verdict
    assert "test_hang_probe.py::test_hangs" in verdict, verdict


def test_wall_backstop_still_bounds_a_file_that_streams_forever(tmp_path: Path) -> None:
    """A file that keeps printing progress but never ends is still bounded by
    the absolute ceiling -- and the verdict says it was progressing."""
    probe = _write(tmp_path, "test_forever_probe.py", """
        import time
        import pytest

        @pytest.mark.parametrize("i", range(200))
        def test_stream(i):
            time.sleep(0.2)
    """)
    _file, rc, output, summary, _wall = runner._run_one_file(
        probe, ["-v", "-p", "no:cacheprovider"], tmp_path,
        file_timeout=3, idle_timeout=30,
    )
    assert rc == 124, output
    assert summary.get("timed_out") == 1 and not summary.get("idle_killed"), summary
    assert "still progressing" in runner._format_timeout_verdict(output, summary)


def test_defaults_make_idle_the_detector_and_wall_the_backstop() -> None:
    """Contract, not a snapshot: the hang window must be the SHORTER of the two
    and small enough to matter; the wall must be large enough that a
    CPU-heavy 150-test file on a 3-CPU quota never trips it."""
    assert runner._DEFAULT_IDLE_TIMEOUT_SECONDS <= 300
    assert runner._DEFAULT_FILE_TIMEOUT_SECONDS >= 900
    assert runner._DEFAULT_IDLE_TIMEOUT_SECONDS < runner._DEFAULT_FILE_TIMEOUT_SECONDS
    assert runner._PROGRESS_LINE_RE.match("tests/x.py::test_a PASSED [ 50%]")
    assert not runner._PROGRESS_LINE_RE.match("collected 12 items")
