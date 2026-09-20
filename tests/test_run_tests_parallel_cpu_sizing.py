"""Worker sizing must follow the cgroup CPU quota, not the host core count.

Why this exists
---------------
The self-hosted ACE-AI runner containers are CFS-capped (``--cpus 2``), so
``/sys/fs/cgroup/cpu.max`` reads ``200000 100000`` while ``nproc`` inside the
container still reports the host's 24 cores (``--cpus`` is a *quota*, not a
cpuset). ``.github/workflows/tests.yml`` pinned ``HERMES_TEST_WORKERS: "12"``,
so every slice ran 12 pytest subprocesses on 2 CPUs — 6x oversubscription,
CFS throttling, and wall-clock/sqlite-busy-timeout flakes that ejected
merge-queue entries (run 35434721712, slice 8/16).

``effective_cpu_count()`` reads the quota; ``HERMES_TEST_WORKERS`` becomes a
ceiling rather than an absolute.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _load_runner_module():
    """Import the runner by absolute file path (worktree bytes, no finder)."""
    spec = importlib.util.spec_from_file_location(
        "_cpu_sizing_run_tests_parallel_under_test", _RUNNER
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner():
    mod = _load_runner_module()
    assert mod.__file__ and str(_REPO_ROOT) in mod.__file__, (
        f"imported runner is not worktree bytes: {mod.__file__}"
    )
    return mod


def _cgroup_v2(tmp_path: Path, contents: str) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.max").write_text(contents, encoding="utf-8")
    return root


def _cgroup_v1(tmp_path: Path, quota: str, period: str) -> Path:
    root = tmp_path / "cgroup"
    (root / "cpu").mkdir(parents=True)
    (root / "cpu" / "cpu.cfs_quota_us").write_text(quota, encoding="utf-8")
    (root / "cpu" / "cpu.cfs_period_us").write_text(period, encoding="utf-8")
    return root


# ── effective_cpu_count: cgroup v2 ───────────────────────────────────────────


def test_cgroup_v2_quota_is_ceil_of_quota_over_period(runner, tmp_path):
    """The ACE-AI container shape: `--cpus 2` -> `200000 100000` -> 2 CPUs."""
    root = _cgroup_v2(tmp_path, "200000 100000\n")
    assert runner.effective_cpu_count(root) == (2, "cgroup-v2")


def test_cgroup_v2_fractional_quota_rounds_up(runner, tmp_path):
    """`--cpus 1.5` must not floor to 1 — ceil, so we never size to zero work."""
    root = _cgroup_v2(tmp_path, "150000 100000\n")
    assert runner.effective_cpu_count(root) == (2, "cgroup-v2")


def test_cgroup_v2_max_is_unlimited_and_falls_through(runner, tmp_path, monkeypatch):
    """`max <period>` means no quota: fall through to affinity/cpu_count."""
    root = _cgroup_v2(tmp_path, "max 100000\n")
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3, 4, 5, 6, 7}, raising=False)
    assert runner.effective_cpu_count(root) == (8, "affinity")


# ── effective_cpu_count: cgroup v1 ───────────────────────────────────────────


def test_cgroup_v1_quota_is_ceil_of_quota_over_period(runner, tmp_path):
    root = _cgroup_v1(tmp_path, "300000\n", "100000\n")
    assert runner.effective_cpu_count(root) == (3, "cgroup-v1")


def test_cgroup_v1_quota_minus_one_is_unlimited_and_falls_through(
    runner, tmp_path, monkeypatch
):
    """v1 signals 'no quota' with -1; that is not a CPU count."""
    root = _cgroup_v1(tmp_path, "-1\n", "100000\n")
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    assert runner.effective_cpu_count(root) == (4, "affinity")


# ── effective_cpu_count: fallbacks ───────────────────────────────────────────


def test_no_cgroup_files_falls_back_to_affinity(runner, tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    assert runner.effective_cpu_count(root) == (2, "affinity")


def test_falls_back_to_cpu_count_when_affinity_unavailable(
    runner, tmp_path, monkeypatch
):
    """macOS/Windows have no sched_getaffinity; os.cpu_count() is the floor."""
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.delattr(runner.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 16)
    assert runner.effective_cpu_count(root) == (16, "cpu_count")


def test_unreadable_cgroup_files_do_not_raise(runner, tmp_path, monkeypatch):
    """A garbage cpu.max must degrade, not crash the whole test run."""
    root = _cgroup_v2(tmp_path, "not-a-quota\n")
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    assert runner.effective_cpu_count(root) == (2, "affinity")


# ── ceiling arithmetic ───────────────────────────────────────────────────────


def test_requested_above_ceiling_is_clamped(runner):
    """The bug: HERMES_TEST_WORKERS=12 on a 2-CPU container -> 4, not 12."""
    assert runner.resolve_worker_count(requested=12, effective_cpus=2) == 4


def test_requested_below_ceiling_is_honoured(runner):
    """A ceiling never raises the count — an explicit small -j still caps."""
    assert runner.resolve_worker_count(requested=3, effective_cpus=4) == 3


def test_no_request_uses_the_ceiling_as_the_default(runner):
    assert runner.resolve_worker_count(requested=None, effective_cpus=4) == 8


def test_single_cpu_still_gets_two_workers(runner):
    """max(2, ...) floor: a 1-CPU box is IO-bound enough for 2."""
    assert runner.resolve_worker_count(requested=None, effective_cpus=1) == 2
    assert runner.resolve_worker_count(requested=12, effective_cpus=1) == 2


def test_force_bypasses_the_ceiling(runner):
    """HERMES_TEST_WORKERS_FORCE=1 is the documented escape hatch."""
    assert runner.resolve_worker_count(requested=12, effective_cpus=2, force=True) == 12


def test_force_without_a_request_still_uses_the_default(runner):
    assert runner.resolve_worker_count(requested=None, effective_cpus=2, force=True) == 4


# ── startup log line ─────────────────────────────────────────────────────────


def test_startup_log_line_names_every_input(runner):
    line = runner.format_worker_sizing_log(
        workers=4, effective_cpus=2, requested=12, source="cgroup-v2"
    )
    assert line == "workers=4 (effective_cpus=2, requested=12, source=cgroup-v2)"


def test_startup_log_line_reports_no_request_as_none(runner):
    line = runner.format_worker_sizing_log(
        workers=8, effective_cpus=4, requested=None, source="affinity"
    )
    assert line == "workers=8 (effective_cpus=4, requested=none, source=affinity)"


def test_generate_slices_stdout_stays_pure_json(tmp_path):
    """The sizing log must NOT land on stdout.

    CI captures `--generate-slices` stdout with `MATRIX=$(...)` and feeds it
    straight into `fromJSON`. Any extra stdout line makes the whole matrix
    unparseable and fails the generate job before a single test runs.
    """
    probe = tmp_path / "tests"
    probe.mkdir()
    (probe / "test_probe.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUNNER),
            "--generate-slices",
            "2",
            "--paths",
            str(probe),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    matrix = json.loads(proc.stdout)  # raises if the log leaked onto stdout
    assert len(matrix["slice"]) == 2
    # And the log line still exists — on stderr, where CI job logs show it.
    assert "effective_cpus=" in proc.stderr, proc.stderr

