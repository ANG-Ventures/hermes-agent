"""Per-slice ``runs_on`` routing: self-hosted first, GitHub-hosted overflow.

Why this exists
---------------
Every job in ``.github/workflows/tests.yml`` used to read ONE label set
(``vars.CI_RUNNER_LABELS``) for the whole matrix, and GitHub will not spill a
full self-hosted pool onto hosted runners by itself. Measured on merge_group
run 35517572423 (2026-09-20): 26 self-hosted jobs against a 17-slot pool,
mean job queue-wait 4.6 min, max 14.4 min, against a mean job RUN of 3.3 min
— jobs waited longer than they ran, so the limiter was SLOT COUNT.

``--generate-slices`` now stamps each slice with its own ``runs_on`` and the
``test`` job reads ``matrix.slice.runs_on``. The two knobs are repo variables
(``CI_SELF_HOSTED_SLOTS`` / ``CI_RUNNER_LABELS``) and must fail SAFE: a
missing, empty, or malformed value can never strand the matrix on an
unschedulable label set, and an unset slot count must be a no-op that leaves
every slice exactly where it ran before.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"
_TESTS_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"

_SELF_HOSTED = '["self-hosted","hermes-ci"]'
_HOSTED = '["ubuntu-latest"]'


def _load_runner_module():
    """Import the runner by absolute file path (worktree bytes, no finder)."""
    spec = importlib.util.spec_from_file_location(
        "_runner_routing_run_tests_parallel_under_test", _RUNNER
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


# ── the split itself ─────────────────────────────────────────────────────────


def test_slices_within_the_cap_get_the_self_hosted_labels(runner):
    for index in (1, 8, 12):
        assert runner._runs_on_for(index, 12, _SELF_HOSTED) == _SELF_HOSTED


def test_slices_past_the_cap_spill_to_hosted(runner):
    for index in (13, 16, 40):
        assert runner._runs_on_for(index, 12, _SELF_HOSTED) == _HOSTED


def test_the_boundary_slice_is_inclusive(runner):
    """K is a slot COUNT, so slice K itself must still be self-hosted.

    Off by one here silently wastes a pool slot (K-1 used) or oversubscribes
    it (K+1 queued) — neither goes red on its own.
    """
    assert runner._runs_on_for(12, 12, _SELF_HOSTED) == _SELF_HOSTED
    assert runner._runs_on_for(13, 12, _SELF_HOSTED) == _HOSTED


def test_no_cap_leaves_every_slice_self_hosted(runner):
    """An unset CI_SELF_HOSTED_SLOTS must be a NO-OP, not a hosted stampede."""
    for index in (1, 17, 200):
        assert runner._runs_on_for(index, None, _SELF_HOSTED) == _SELF_HOSTED


def test_zero_slots_spills_the_whole_matrix(runner):
    """0 is a meaningful value — drain the pool without touching workflows."""
    assert runner._runs_on_for(1, 0, _SELF_HOSTED) == _HOSTED


# ── fail-safe parsing of the repo variables ──────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_slots_means_no_cap(runner, raw):
    assert runner._resolve_self_hosted_slots(raw) is None


@pytest.mark.parametrize("raw", ["seventeen", "17.5", "-1", "1,7"])
def test_unparseable_slots_fall_back_to_no_cap(runner, raw):
    """A fat-fingered repo variable must never MOVE jobs off the pool.

    Failing toward "no cap" keeps today's behaviour; failing toward 0 would
    silently evacuate the whole matrix to hosted runners.
    """
    assert runner._resolve_self_hosted_slots(raw) is None


def test_numeric_slots_are_parsed_and_stripped(runner):
    assert runner._resolve_self_hosted_slots(" 17 ") == 17


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_labels_fall_back_to_hosted(runner, raw):
    """Mirrors `vars.CI_RUNNER_LABELS || '["ubuntu-latest"]'` in the workflows."""
    assert runner._resolve_self_hosted_labels(raw) == _HOSTED


@pytest.mark.parametrize(
    "raw",
    [
        "self-hosted",  # bare string, not an array
        "[]",  # empty array → `runs-on: []` matches no runner
        '["", "hermes-ci"]',  # empty label
        '["self-hosted",',  # truncated JSON
        '{"labels": ["self-hosted"]}',  # object, not array
        "[1, 2]",  # non-string members
    ],
)
def test_malformed_labels_fall_back_to_hosted(runner, raw):
    """A malformed value in `runs-on` makes every matrix job UNSCHEDULABLE.

    It does not fail — it hangs queued until the run times out, which is the
    worst possible failure mode. Hosted always runs, so that is the fallback.
    """
    assert runner._resolve_self_hosted_labels(raw) == _HOSTED


def test_valid_labels_round_trip_as_json_array(runner):
    resolved = runner._resolve_self_hosted_labels('["self-hosted", "hermes-ci"]')
    assert json.loads(resolved) == ["self-hosted", "hermes-ci"]


def test_rollback_via_runner_labels_is_unchanged(runner):
    """Pointing CI_RUNNER_LABELS at ubuntu-latest still routes EVERYTHING hosted.

    The documented rollback must survive the new slot knob: with hosted labels
    the split still happens, but both sides resolve to ubuntu-latest.
    """
    labels = runner._resolve_self_hosted_labels('["ubuntu-latest"]')
    assert runner._runs_on_for(1, 4, labels) == _HOSTED
    assert runner._runs_on_for(99, 4, labels) == _HOSTED


# ── end-to-end through the real CLI (what CI actually invokes) ───────────────


def _generate(tmp_path: Path, *extra: str) -> dict:
    probe = tmp_path / "tests"
    probe.mkdir(exist_ok=True)
    for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py"):
        (probe / name).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUNNER),
            "--generate-slices",
            "4",
            "--paths",
            str(probe),
            *extra,
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_cli_stamps_every_slice_and_splits_at_the_cap(tmp_path):
    matrix = _generate(
        tmp_path, "--self-hosted-slots", "2", "--self-hosted-labels", _SELF_HOSTED
    )
    got = [(s["index"], s["runs_on"]) for s in matrix["slice"]]
    assert got == [
        (1, _SELF_HOSTED),
        (2, _SELF_HOSTED),
        (3, _HOSTED),
        (4, _HOSTED),
    ], got


def test_cli_without_the_new_flags_keeps_every_slice_on_one_class(tmp_path):
    """No flags = the pre-change shape: nothing spills anywhere unexpected."""
    matrix = _generate(tmp_path)
    assert {s["runs_on"] for s in matrix["slice"]} == {_HOSTED}


def test_cli_empty_flag_values_are_a_no_op(tmp_path):
    """CI passes "$CI_SELF_HOSTED_SLOTS" — unset expands to an EMPTY STRING.

    That is the single most likely real-world input (the repo variable does
    not exist yet), so it must resolve to "every slice self-hosted", never to
    a crash and never to a hosted stampede.
    """
    matrix = _generate(
        tmp_path, "--self-hosted-slots", "", "--self-hosted-labels", _SELF_HOSTED
    )
    assert {s["runs_on"] for s in matrix["slice"]} == {_SELF_HOSTED}


def test_cli_stdout_stays_pure_json_with_a_bad_label_value(tmp_path):
    """The fallback WARNING must not leak onto stdout.

    CI captures stdout with `MATRIX=$(...)` and feeds it straight into
    `fromJSON`; one stray line makes the whole matrix unparseable and fails
    the generate job before a single test runs.
    """
    matrix = _generate(
        tmp_path, "--self-hosted-slots", "2", "--self-hosted-labels", "not-json"
    )
    assert {s["runs_on"] for s in matrix["slice"]} == {_HOSTED}


def test_every_emitted_runs_on_is_parseable_as_a_label_array(tmp_path):
    """`fromJSON(matrix.slice.runs_on)` must yield a non-empty string list.

    Anything else leaves the job queued forever rather than failing loudly.
    """
    matrix = _generate(
        tmp_path, "--self-hosted-slots", "2", "--self-hosted-labels", _SELF_HOSTED
    )
    for slice_ in matrix["slice"]:
        labels = json.loads(slice_["runs_on"])
        assert isinstance(labels, list) and labels
        assert all(isinstance(x, str) and x for x in labels)


# ── the plugin-scoped matrix takes the same stamp ────────────────────────────


def test_scoped_plugin_matrix_pins_the_smoke_slice_self_hosted(runner):
    """The pinned core-smoke slice gates every plugin-scoped run — never spill it.

    Even with slots=0 (whole matrix drained to hosted) the smoke slice keeps
    the configured class, because it is the one slice the run cannot proceed
    without.
    """
    plugin_dir = _REPO_ROOT / "tests" / "plugins"
    names = [
        p.name
        for p in sorted(plugin_dir.iterdir())
        if p.is_dir() and any(p.glob("test_*.py"))
    ]
    if not names:
        pytest.skip("no plugin test tree to scope against")
    matrix = runner._scoped_plugin_matrix(
        f"plugin:{names[0]}", _REPO_ROOT, 0, _SELF_HOSTED
    )
    assert matrix is not None
    by_name = {s["name"]: s["runs_on"] for s in matrix["slice"]}
    assert by_name["core smoke"] == _SELF_HOSTED
    assert by_name[f"plugin {names[0]}"] == _HOSTED


def test_scoped_plugin_matrix_stamps_runs_on_at_all(runner):
    """Missing `runs_on` here resolves to '' -> `fromJSON('')` -> the job dies.

    The plugin-scoped path is a separate emit site from the LPT path; it is
    exactly the kind of second surface that gets forgotten.
    """
    plugin_dir = _REPO_ROOT / "tests" / "plugins"
    names = [
        p.name
        for p in sorted(plugin_dir.iterdir())
        if p.is_dir() and any(p.glob("test_*.py"))
    ]
    if not names:
        pytest.skip("no plugin test tree to scope against")
    matrix = runner._scoped_plugin_matrix(f"plugin:{names[0]}", _REPO_ROOT)
    assert matrix is not None
    for slice_ in matrix["slice"]:
        assert slice_.get("runs_on"), f"slice {slice_['name']} has no runs_on"
        assert json.loads(slice_["runs_on"])


# ── the workflow half of the contract ────────────────────────────────────────


def _tests_workflow() -> dict:
    return yaml.safe_load(_TESTS_WORKFLOW.read_text(encoding="utf-8"))


def test_test_job_reads_the_per_slice_runs_on():
    """The emit side is useless if the `test` job still reads the repo variable.

    This is the contract seam: `run_tests_parallel.py` stamps `runs_on`, and
    ONLY `matrix.slice.runs_on` consumes it. Reverting the job to
    `vars.CI_RUNNER_LABELS` would leave every stamp inert with no test failing.
    """
    job = _tests_workflow()["jobs"]["test"]
    assert job["runs-on"] == "${{ fromJSON(matrix.slice.runs_on) }}", job["runs-on"]


def test_generate_step_forwards_both_repo_variables():
    """Both knobs must reach the script, or the split silently never happens."""
    jobs = _tests_workflow()["jobs"]
    step = next(
        s for s in jobs["generate"]["steps"] if s.get("id") == "matrix"
    )
    env = step.get("env") or {}
    assert env.get("CI_SELF_HOSTED_SLOTS") == "${{ vars.CI_SELF_HOSTED_SLOTS }}"
    assert env.get("CI_RUNNER_LABELS") == "${{ vars.CI_RUNNER_LABELS }}"
    assert "--self-hosted-slots" in step["run"]
    assert "--self-hosted-labels" in step["run"]
