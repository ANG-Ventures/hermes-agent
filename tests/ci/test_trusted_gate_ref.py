"""The required gate runs its deciding scripts from a TRUSTED ref, not the PR tree.

Prism P1 ac6c31b59f61: ``all-checks-pass`` checked out ``github.sha`` (the PR's
merge ref) and ran ``scripts/ci/evaluate_needs.py`` from it, and ``detect`` ran
the PR's own ``detect-changes`` action + ``classify_changes.py``. A PR could
rewrite the script that decides whether it passes.

These tests resolve each gate job's REAL ``actions/checkout`` ``ref:``
expression against a real-shaped event, check that ref out of a throwaway
origin whose base holds the honest script and whose PR head holds a script
rewritten to ``exit 0``, then execute the step's real ``run:`` bytes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ORCHESTRATORS = ("ci.yaml", "ci-local.yaml")
CHECKOUT = "actions/checkout@"


def _jobs(workflow: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"]


def _checkout_ref(job: dict) -> str:
    (step,) = [s for s in job["steps"] if str(s.get("uses", "")).startswith(CHECKOUT)]
    return (step.get("with") or {}).get("ref", "")


def _resolve(expr: str, ctx: dict) -> str:
    """Evaluate a ``${{ a || b || c }}`` checkout ref over a github context."""
    m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr.strip())
    assert m, f"checkout ref is not a single expression: {expr!r}"
    for term in m.group(1).split("||"):
        node: object = {"github": ctx}
        for part in term.strip().split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node:
            return str(node)
    return ""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _world(tmp: Path, rel: str, pr_bytes: str) -> tuple[Path, dict]:
    """origin: main = this tree's ``rel``; PR head rewrites ``rel`` to ``pr_bytes``.

    Returns the bare origin and a github context for each event shape, with
    ``github.sha`` = the PR head (pull_request's merge ref carries the PR bytes
    the same way) so the OLD ``ref: github.sha`` is the vulnerable control.
    """
    seed = tmp / "seed"
    (seed / rel).parent.mkdir(parents=True)
    shutil.copy(ROOT / rel, seed / rel)
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    base = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "-q", "-b", "pr")
    (seed / rel).write_text(pr_bytes, encoding="utf-8")
    _git(seed, "commit", "-q", "-am", "pr: the gate always passes")
    head = _git(seed, "rev-parse", "HEAD")
    origin = tmp / "origin.git"
    _git(tmp, "clone", "-q", "--bare", str(seed), str(origin))
    events = {
        "pull_request": {"sha": head, "event": {"pull_request": {"base": {"sha": base}}}},
        # base_sha is the PREVIOUS queue candidate: here, deliberately the PR head.
        "merge_group": {"sha": head, "event": {"merge_group": {
            "base_ref": "refs/heads/main", "base_sha": head, "head_sha": head}}},
    }
    return origin, events


def _checkout(tmp: Path, origin: Path, ref: str) -> Path:
    work = tmp / f"work-{len(list(tmp.glob('work-*')))}"
    _git(tmp, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", ref if not ref.startswith("refs/heads/")
         else ref.removeprefix("refs/heads/"))
    return work


EXIT_ZERO = "import sys\nsys.exit(0)\n"
FAILED_NEEDS = {
    "detect": {"result": "success", "outputs": {"event_name": "pull_request"}},
    "tests": {"result": "failure", "outputs": {}},
}


def _run_evaluate(work: Path, tmp: Path) -> subprocess.CompletedProcess:
    (step,) = [s for s in _jobs("ci.yaml")["all-checks-pass"]["steps"]
               if s.get("id") == "evaluate"]
    out = tmp / "github_output"
    out.touch()
    env = {**os.environ, "NEEDS": json.dumps(FAILED_NEEDS), "RELEASE": "false",
           "GITHUB_OUTPUT": str(out), "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("workflow", ORCHESTRATORS)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_pr_that_rewrites_the_gate_to_exit_0_still_fails(tmp_path, workflow, event):
    origin, events = _world(tmp_path, "scripts/ci/evaluate_needs.py", EXIT_ZERO)
    ref = _resolve(_checkout_ref(_jobs(workflow)["all-checks-pass"]), events[event])
    r = _run_evaluate(_checkout(tmp_path, origin, ref), tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tests concluded 'failure'" in r.stdout


def test_control_old_ref_github_sha_lets_the_rewrite_pass(tmp_path):
    # The same world under the pre-fix ``ref: github.sha``: the PR's exit-0
    # script decides, and a failed required job reads green. Without this the
    # test above could pass for a reason other than the ref.
    origin, events = _world(tmp_path, "scripts/ci/evaluate_needs.py", EXIT_ZERO)
    ref = _resolve("${{ github.sha }}", events["pull_request"])
    r = _run_evaluate(_checkout(tmp_path, origin, ref), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


LIE_CLASSIFIER = (
    "import os\n"
    "with open(os.environ['GITHUB_OUTPUT'], 'a') as f:\n"
    "    f.write('python=false\\nci_review=false\\n')\n"
)


@pytest.mark.parametrize("workflow", ORCHESTRATORS)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_pr_that_rewrites_the_classifier_does_not_choose_its_lanes(tmp_path, workflow, event):
    # detect's classifier decides which required lanes run and whether the
    # ci-reviewed gate fires; the PR's own copy must not be the one executed.
    origin, events = _world(tmp_path, "scripts/ci/classify_changes.py", LIE_CLASSIFIER)
    ref = _resolve(_checkout_ref(_jobs(workflow)["detect"]), events[event])
    work = _checkout(tmp_path, origin, ref)
    out = tmp_path / "github_output"
    out.touch()
    r = subprocess.run([sys.executable, "scripts/ci/classify_changes.py"],
                       input=".github/workflows/ci.yaml\n", cwd=work, capture_output=True,
                       text=True, env={**os.environ, "GITHUB_OUTPUT": str(out), "EVENT_NAME": event},
                       timeout=60)
    assert r.returncode == 0, r.stderr
    lanes = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert lanes["python"] == "true" and lanes["ci_review"] == "true", lanes


def test_attribution_relay_runs_from_main_not_the_previous_candidate(tmp_path):
    origin, events = _world(tmp_path, "scripts/ci/attribution_merge_group.py", EXIT_ZERO)
    job = yaml.safe_load((WORKFLOWS / "attribution-merge-group.yml").read_text())["jobs"]["attribution"]
    ref = _resolve(_checkout_ref(job), events["merge_group"])
    work = _checkout(tmp_path, origin, ref)
    assert (work / "scripts/ci/attribution_merge_group.py").read_bytes() == \
        (ROOT / "scripts/ci/attribution_merge_group.py").read_bytes()
