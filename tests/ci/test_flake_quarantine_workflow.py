"""tests.yml wiring for the flake quarantine (spec I4) and the L2(a) re-run guard.

The slice-verdict test EXECUTES the real ``run:`` bytes of the workflow step in
a throwaway git repo whose ``origin`` holds a base commit, so the base-ref rule
(a PR cannot exempt its own broken test) is proven on the YAML itself, not on a
copy of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows" / "tests.yml"
BASE_EXPR = ("${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha "
             "|| github.event.before || github.sha }}")


def _jobs() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name_prefix: str) -> dict:
    for st in _jobs()[job]["steps"]:
        if str(st.get("name", "")).startswith(name_prefix):
            return st
    raise AssertionError(f"no step {name_prefix!r} in job {job}")


def _index(job: str, name_prefix: str) -> int:
    for i, st in enumerate(_jobs()[job]["steps"]):
        if str(st.get("name", "")).startswith(name_prefix):
            return i
    raise AssertionError(name_prefix)


def test_generate_records_executed_sha_annotation():
    st = _step("generate", "Record executed tree")
    assert 'title=ci-executed-sha::${GITHUB_SHA}' in st["run"]


def test_slice_writes_junit_and_manifest_and_defers_verdict():
    run = _step("test", "Run tests (")
    assert run["id"] == "tests" and run["continue-on-error"] is True
    assert "--junit-dir ci-junit --result-file ci-junit/slice-result.json" in run["run"]
    up = _step("test", "Upload per-file junit")
    assert up["with"]["name"] == "ci-slice-result-${{ matrix.slice.index }}-a${{ github.run_attempt }}"
    assert up["if"] == "${{ !cancelled() }}"
    verdict = _step("test", "Slice verdict")
    assert verdict["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in verdict  # the verdict IS the gate
    assert verdict["env"]["TESTS_OUTCOME"] == "${{ steps.tests.outcome }}"
    assert verdict["env"]["BASE_SHA"] == BASE_EXPR
    # Evidence is uploaded before the verdict can fail the job.
    assert _index("test", "Upload per-file junit") < _index("test", "Slice verdict")


def test_tests_complete_runs_evidence_gate_with_actions_read():
    job = _jobs()["tests-complete"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    st = _step("tests-complete", "Quarantine list lint + evidence gate")
    assert st["env"]["BASE_SHA"] == BASE_EXPR
    assert 'git show "$BASE_SHA:scripts/ci/flake_quarantine.py"' in st["run"]
    assert "check-changes" in st["run"]


# ── executing the verdict step's real bytes ─────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _world(tmp: Path, base_entries: list, head_entries: list, with_base_checker=True):
    """origin (bare) holds the base commit; ``work`` is a clone at the PR head."""
    seed = tmp / "seed"
    (seed / "scripts/ci").mkdir(parents=True)
    if with_base_checker:
        shutil.copy(ROOT / "scripts/ci/flake_quarantine.py", seed / "scripts/ci/flake_quarantine.py")
        (seed / "scripts/ci/flake_quarantine.json").write_text(json.dumps({"schema": 1, "entries": base_entries}))
    else:
        (seed / "README").write_text("pre-quarantine base\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    base_sha = _git(seed, "rev-parse", "HEAD")
    origin = tmp / "origin.git"
    _git(tmp, "clone", "-q", "--bare", str(seed), str(origin))
    work = tmp / "work"
    _git(tmp, "clone", "-q", str(origin), str(work))
    (work / "scripts/ci").mkdir(parents=True, exist_ok=True)
    (work / "scripts/ci/flake_quarantine.json").write_text(json.dumps({"schema": 1, "entries": head_entries}))
    (work / ".venv/bin").mkdir(parents=True)
    os.symlink(sys.executable, work / ".venv/bin/python")
    return work, base_sha


def _failing_slice(work: Path) -> str:
    tf = work / "test_broken.py"
    tf.write_text("def test_broken():\n    assert False\n")
    out = work / "ci-junit"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_tests_parallel.py"), "-j", "1", "--file-retries", "0",
         "--junit-dir", str(out), "--result-file", str(out / "slice-result.json"), "--files", str(tf)],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout
    return f"{tf}::test_broken"


def _entry(node):
    return {"node_id": node, "card": "t_162ffd04", "owner": "daedalus", "until": "2099-01-01", "evidence": []}


def _run_verdict(work: Path, base_sha: str, outcome="failure"):
    script = _step("test", "Slice verdict")["run"]
    env = {**os.environ, "TESTS_OUTCOME": outcome, "BASE_SHA": base_sha, "RUNNER_TEMP": str(work.parent)}
    return subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True, text=True)


def test_step_bytes_a_pr_cannot_quarantine_its_own_broken_test(tmp_path):
    # Head list exempts the test; base list does not => RED.
    work, base = _world(tmp_path, [], [])
    node = _failing_slice(work)
    (work / "scripts/ci/flake_quarantine.json").write_text(json.dumps({"schema": 1, "entries": [_entry(node)]}))
    r = _run_verdict(work, base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "not quarantined" in r.stdout


def test_step_bytes_base_quarantine_makes_failure_non_gating(tmp_path):
    node = f"{tmp_path / 'work' / 'test_broken.py'}::test_broken"
    # Entry on the BASE, removed on the head: the base list decides => GREEN.
    work, base = _world(tmp_path, [_entry(node)], [])
    assert _failing_slice(work) == node
    r = _run_verdict(work, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "QUARANTINED" in r.stdout


def test_step_bytes_base_without_checker_gates(tmp_path):
    work, base = _world(tmp_path, [], [], with_base_checker=False)
    _failing_slice(work)
    r = _run_verdict(work, base)
    assert r.returncode == 1
    assert "no quarantine checker/list on base" in r.stdout


def test_step_bytes_skipped_tests_step_gates(tmp_path):
    work, base = _world(tmp_path, [], [])
    assert _run_verdict(work, base, outcome="skipped").returncode == 1
    assert _run_verdict(work, base, outcome="success").returncode == 0
