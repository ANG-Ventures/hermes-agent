"""Execute the detect-changes composite action's bash for real, per event.

The lane classifier itself is covered by ``test_classify_changes.py``. What is
NOT covered there — and what merges untested code if it regresses — is the
*event gating* in ``.github/actions/detect-changes/action.yml``: which events
resolve a real ``base...head`` diff and which fail OPEN to the full matrix.

These tests run the action's actual ``run:`` block in bash with a stub ``gh``
on PATH, so the assertions are over the script that CI executes, not a
paraphrase of it.

The property under test:

* ``pull_request``  → diff from ``pull_request.base.sha``/``head.sha``.
* ``merge_group``   → diff from the TARGET BRANCH
  (``merge_group.base_ref``) to ``merge_group.head_sha``. Deliberately NOT
  ``merge_group.base_sha``: queue candidate refs are chained, so base_sha is
  the previous candidate and its diff is only the topmost entry's delta, which
  under-tests every other member of the batch (measured 2026-09-19; argument in
  full at the action).
* every other event (``push``, ``release``, ``workflow_dispatch``, ...) → no
  base resolved, empty diff, every lane ``true``, ``test_scope=full``.
* a malformed/absent payload on a gated event → fails OPEN, same as push.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION = REPO_ROOT / ".github" / "actions" / "detect-changes" / "action.yml"

PR_BASE = "1" * 40
PR_HEAD = "2" * 40
# The PREVIOUS queue candidate. A chained batch's payload carries this as
# base_sha; the classifier must never diff from it.
MG_PREV_CANDIDATE = "3" * 40
MG_HEAD = "4" * 40
MG_BASE_REF = "refs/heads/main"


def _action_script() -> str:
    doc = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = [s for s in doc["runs"]["steps"] if s.get("id") == "classify"]
    return step["run"]


def _run(tmp_path: Path, env_overrides: dict[str, str], changed: list[str]):
    """Run the action script with a stub ``gh`` that prints ``changed``.

    The stub echoes the requested compare ref to a sidecar file so a test can
    assert WHICH SHAs the script asked about.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmp_path / "gh_args.txt"}\n'
        + "".join(f'printf "%s\\n" {p!r}\n' for p in changed),
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)

    github_output = tmp_path / "github_output"
    github_output.touch()

    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_TOKEN": "stub",
        "REPO": "ANG-Ventures/hermes-agent",
        "EVENT_NAME": "",
        "PR_BASE_SHA": "",
        "PR_HEAD_SHA": "",
        "MG_BASE_REF": "",
        "MG_HEAD_SHA": "",
        "GITHUB_OUTPUT": str(github_output),
        **env_overrides,
    }
    proc = subprocess.run(
        ["bash", "-c", _action_script()],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"action script failed:\n{proc.stdout}\n{proc.stderr}"

    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    gh_args_path = tmp_path / "gh_args.txt"
    gh_args = gh_args_path.read_text(encoding="utf-8") if gh_args_path.exists() else ""
    return outputs, gh_args, proc.stdout + proc.stderr


@pytest.fixture(autouse=True)
def _need_bash():
    if shutil.which("bash") is None:  # pragma: no cover - CI always has bash
        pytest.skip("bash unavailable")


# --------------------------------------------------------------------------
# (a) merge_group with a tests-only batch: narrowed lanes.
# --------------------------------------------------------------------------
def test_merge_group_tests_only_batch_narrows_lanes(tmp_path):
    outputs, gh_args, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py", "tests/tools/test_bar.py"],
    )
    # It asked about the TARGET BRANCH, not the chained previous candidate.
    assert f"main...{MG_HEAD}" in gh_args
    # and never about the chained previous candidate.
    assert MG_PREV_CANDIDATE not in gh_args
    # pytest must still run (tests changed), but nothing that ships the product.
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "false"
    assert outputs["frontend"] == "false"
    assert outputs["site"] == "false"
    assert outputs["docker"] == "false"
    assert outputs["nix"] == "false"
    assert outputs["uv_lock"] == "false"
    assert outputs["rust"] == "false"
    assert outputs["installer"] == "false"
    assert outputs["test_scope"] == "full"


def test_merge_group_single_plugin_batch_narrows_test_scope(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["plugins/kanban/plugin_api.py", "tests/plugins/kanban/test_api.py"],
    )
    assert outputs["test_scope"] == "plugin:kanban"


# --------------------------------------------------------------------------
# (b) merge_group touching a core path: full matrix, unchanged.
# --------------------------------------------------------------------------
def test_merge_group_core_batch_runs_full_matrix(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["agent/run_agent.py", "tests/agent/test_run_agent.py"],
    )
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["docker"] == "true"
    assert outputs["nix"] == "true"
    assert outputs["scan"] == "true"
    assert outputs["test_scope"] == "full"


def test_merge_group_workflow_change_forces_every_lane(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        [".github/workflows/ci.yaml"],
    )
    for lane in ("python", "python_prod", "frontend", "site", "scan", "deps",
                 "uv_lock", "npm_lock", "installer", "rust", "nix", "ci_review"):
        assert outputs[lane] == "true", lane
    assert outputs["test_scope"] == "full"


# --------------------------------------------------------------------------
# (c) malformed / absent payload: fails OPEN.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"EVENT_NAME": "merge_group"}, id="merge_group-absent-payload"),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": ""},
            id="merge_group-half-empty",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "main", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-not-refs-heads",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "refs/tags/v1", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-is-a-tag",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "refs/heads/m ain;rm", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-hostile-charset",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": "not-a-sha"},
            id="merge_group-garbage-head",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD[:39]},
            id="merge_group-short-head-sha",
        ),
        pytest.param({"EVENT_NAME": "pull_request"}, id="pull_request-absent-shas"),
        pytest.param(
            {"EVENT_NAME": "pull_request", "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": "not-a-sha"},
            id="pull_request-garbage-head",
        ),
    ],
)
def test_malformed_payload_fails_open(tmp_path, overrides):
    outputs, gh_args, _ = _run(tmp_path, overrides, ["tests/gateway/test_foo.py"])
    assert gh_args == "", "must not call the compare API without a valid base/head pair"
    for lane in ("python", "python_prod", "frontend", "site", "scan", "deps",
                 "uv_lock", "npm_lock", "installer", "rust", "nix"):
        assert outputs[lane] == "true", lane
    assert outputs["test_scope"] == "full"


# --------------------------------------------------------------------------
# Non-PR, non-merge_group events keep failing open — the narrowing is not
# widened beyond merge_group.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("event", ["push", "release", "workflow_dispatch", "schedule"])
def test_other_events_still_fail_open(tmp_path, event):
    outputs, gh_args, _ = _run(
        tmp_path,
        # Even if a payload happened to carry SHAs, a non-gated event ignores them.
        {"EVENT_NAME": event, "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD,
         "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": PR_HEAD},
        ["tests/gateway/test_foo.py"],
    )
    assert gh_args == "", f"{event} must not resolve a diff"
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["test_scope"] == "full"


def test_pull_request_still_uses_pull_request_shas(tmp_path):
    outputs, gh_args, _ = _run(
        tmp_path,
        {"EVENT_NAME": "pull_request", "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": PR_HEAD,
         "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py"],
    )
    assert f"{PR_BASE}...{PR_HEAD}" in gh_args
    assert MG_HEAD not in gh_args
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "false"


# --------------------------------------------------------------------------
# REGRESSION GUARD — the multi-entry chained batch.
#
# This is the case that makes base_sha wrong and it is invisible on a
# single-entry batch (where base_sha == the target tip, so both bases agree).
# Measured on the live queue 2026-09-19: with 5 entries queued, each candidate
# ref's base_sha was the PREVIOUS candidate's head, so the payload pair on the
# candidate that fast-forwards main resolved 3 files where the true incoming
# set was 12 — two product files (gateway/platforms/base.py,
# plugins/platforms/discord/adapter.py) would have merged with their lanes
# never run on the tree that lands.
#
# The stub gh serves the batch UNION for the target-branch compare and only
# the topmost entry's delta for the chained-base compare, so this test goes RED
# if the action is ever pointed back at merge_group.base_sha.
# --------------------------------------------------------------------------
def test_merge_group_chained_batch_uses_union_not_topmost_delta(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    union = ["cli.py", "gateway/platforms/base.py", "plugins/platforms/discord/adapter.py"]
    topmost = ["cli.py"]
    # Route on which base the script asked about.
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmp_path / "gh_args.txt"}\n'
        f'if [[ "$*" == *"{MG_PREV_CANDIDATE}"* ]]; then\n'
        + "".join(f'  printf "%s\\n" {p!r}\n' for p in topmost)
        + "else\n"
        + "".join(f'  printf "%s\\n" {p!r}\n' for p in union)
        + "fi\n",
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)

    github_output = tmp_path / "github_output"
    github_output.touch()
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_TOKEN": "stub",
        "REPO": "ANG-Ventures/hermes-agent",
        "EVENT_NAME": "merge_group",
        "PR_BASE_SHA": "",
        "PR_HEAD_SHA": "",
        # A real chained payload carries BOTH: base_ref (target branch) and
        # base_sha (the previous candidate). Export the wrong one too, so the
        # test proves the action ignores it rather than merely lacking it.
        "MG_BASE_REF": MG_BASE_REF,
        "MG_BASE_SHA": MG_PREV_CANDIDATE,
        "MG_HEAD_SHA": MG_HEAD,
        "GITHUB_OUTPUT": str(github_output),
    }
    proc = subprocess.run(
        ["bash", "-c", _action_script()],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    gh_args = (tmp_path / "gh_args.txt").read_text(encoding="utf-8")
    assert f"main...{MG_HEAD}" in gh_args, "must diff from the target branch"
    assert MG_PREV_CANDIDATE not in gh_args, (
        "must NOT diff from merge_group.base_sha — that is the previous chained "
        "candidate and yields only the topmost entry's delta"
    )

    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    # The union contains a plugin path and a gateway path. Under the topmost
    # delta (cli.py alone) frontend/site stay false either way, but the lanes
    # the OTHER member PRs need must be ON.
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["scan"] == "true"
    # A multi-tree union must never resolve to a single plugin's scope.
    assert outputs["test_scope"] == "full"


# --------------------------------------------------------------------------
# CONTRACT: the step's env: BLOCK, not just its run: script.
#
# `_action_script()` above extracts only `step["run"]`, so every test in this
# file injects MG_BASE_REF/PR_BASE_SHA itself. That makes the *binding* — WHICH
# payload field each env var reads — invisible to the suite: mutating
# `MG_BASE_REF: ${{ github.event.merge_group.base_ref }}` to `.base_sha` on
# action.yml left all 19 tests green (measured 2026-09-19). That one line IS
# the defect this action exists to fix, so it gets pinned lexically here.
#
# A lexical equality assert is deliberate: the point is pinning the field name,
# not the behaviour downstream of it.
# --------------------------------------------------------------------------

# Every env var of the classify step, mapped to the EXACT expression that must
# produce it. Payload-derived entries are the load-bearing ones; the rest are
# included so the dict is the whole block and a drift anywhere trips.
EXPECTED_CLASSIFY_ENV = {
    "GH_TOKEN": "${{ inputs.github-token || github.token }}",
    "REPO": "${{ github.repository }}",
    "EVENT_NAME": "${{ github.event_name }}",
    "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    # base_REF (the target branch), NOT base_sha (the previous chained queue
    # candidate) — see the SAFETY ARGUMENT in the action.
    "MG_BASE_REF": "${{ github.event.merge_group.base_ref }}",
    "MG_HEAD_SHA": "${{ github.event.merge_group.head_sha }}",
}


def _classify_step_env() -> dict[str, str]:
    doc = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = [s for s in doc["runs"]["steps"] if s.get("id") == "classify"]
    return dict(step["env"])


@pytest.mark.parametrize("var", sorted(EXPECTED_CLASSIFY_ENV))
def test_classify_env_binds_the_exact_payload_field(var):
    """Each env var must read the exact expression it is pinned to."""
    actual = _classify_step_env().get(var)
    expected = EXPECTED_CLASSIFY_ENV[var]
    assert actual == expected, (
        f"detect-changes classify step: env {var} reads {actual!r}, "
        f"must read {expected!r}. The classifier's whole correctness argument "
        f"rests on WHICH payload field each var binds; the run: script cannot "
        f"see this, so changing it here silently changes what CI diffs."
    )


def test_classify_env_block_has_no_unpinned_vars():
    """A new env var must be pinned above, not merely added to the action."""
    actual = set(_classify_step_env())
    expected = set(EXPECTED_CLASSIFY_ENV)
    assert actual == expected, (
        "detect-changes classify step env: block drifted — "
        f"added {sorted(actual - expected)}, removed {sorted(expected - actual)}. "
        "Pin any new payload-derived var in EXPECTED_CLASSIFY_ENV."
    )


def test_classify_env_never_reads_merge_group_base_sha():
    """The chained-candidate field must appear in NO binding, under any name."""
    offenders = {
        name: expr
        for name, expr in _classify_step_env().items()
        if "merge_group.base_sha" in expr
    }
    assert not offenders, (
        f"detect-changes classify step binds github.event.merge_group.base_sha "
        f"via {offenders} — base_sha is the PREVIOUS queue candidate, so "
        "diffing from it yields only the topmost entry's delta and under-tests "
        "every other member of the batch."
    )


# --------------------------------------------------------------------------
# CONTRACT: the action's top-level `outputs:` BLOCK, and the whole hop chain
# from the script's $GITHUB_OUTPUT keys to every consuming `if:`.
#
# The env: seam above is only half of the plumbing the suite could not see.
# The tests read $GITHUB_OUTPUT directly, so they prove the SCRIPT emits
# `python=true` — nothing proves the action re-exports it as `outputs.python`,
# nor that `needs.detect.outputs.python` resolves to anything at all. There
# are three renameable hops:
#
#   script key  ->  action outputs:  ->  job outputs:  ->  needs.<job>.outputs
#
# A typo or rename at any hop makes the consuming `if:` evaluate the empty
# string, and empty is falsey, so the lane silently goes OFF. That fails
# CLOSED — the dangerous direction: the env: seam under-diffs loudly-wrong,
# this one skips a required check while CI stays green.
#
# These assert DERIVED invariants (emitted set == declared set, and every
# reference resolves), not a frozen 17-name list: adding a genuine new lane
# must stay a one-line change, while a rename that breaks a hop must fail.
# --------------------------------------------------------------------------


def _action_doc() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _emitted_output_keys(tmp_path: Path) -> set[str]:
    """Keys the action's script actually writes to $GITHUB_OUTPUT."""
    outputs, _, _ = _run(tmp_path, {"EVENT_NAME": "push"}, ["hermes/agent.py"])
    return set(outputs)


def test_action_outputs_are_exactly_what_the_script_emits(tmp_path):
    """Declared outputs must match the script's emitted keys, both ways.

    Declared-but-never-emitted -> the consuming `if:` reads "" -> lane OFF.
    Emitted-but-undeclared     -> the value dies at the action boundary.
    """
    emitted = _emitted_output_keys(tmp_path)
    declared = set(_action_doc()["outputs"])
    assert declared == emitted, (
        "detect-changes action.yml outputs: block is out of sync with the "
        f"script: declared-but-not-emitted {sorted(declared - emitted)} "
        f"(consumers read '' and the lane silently goes OFF), "
        f"emitted-but-not-declared {sorted(emitted - declared)} "
        "(the value never escapes the composite action)."
    )


def test_action_outputs_forward_the_same_named_classify_key():
    """Each `outputs.X` must forward `steps.classify.outputs.X` — same name.

    Cross-wiring (outputs.nix reading steps.classify.outputs.site) type-checks
    fine in YAML and runs the wrong lanes.
    """
    mismatched = {
        name: spec.get("value")
        for name, spec in _action_doc()["outputs"].items()
        if str(spec.get("value", "")).strip()
        != "${{ steps.classify.outputs.%s }}" % name
    }
    assert not mismatched, (
        f"detect-changes action.yml outputs: {mismatched} — each output must "
        "forward the identically-named key of the classify step. A rename or "
        "cross-wire here resolves to '' (lane OFF) or to another lane's value."
    )


def _workflow_job_outputs(doc: dict, job_name: str) -> set[str] | None:
    """Outputs a job exposes to `needs.<job>.outputs.*`, or None if no job.

    A `uses:` (reusable-workflow) job exposes the CALLED workflow's
    `on.workflow_call.outputs`, not anything in the calling file.
    """
    job = (doc.get("jobs") or {}).get(job_name)
    if job is None:
        return None
    called = job.get("uses")
    if isinstance(called, str) and called.startswith("./"):
        called_doc = yaml.safe_load(
            (REPO_ROOT / called[2:]).read_text(encoding="utf-8")
        )
        # PyYAML parses a bare `on:` key as the boolean True.
        triggers = called_doc.get("on", called_doc.get(True)) or {}
        return set((triggers.get("workflow_call") or {}).get("outputs") or {})
    return set(job.get("outputs") or {})


WORKFLOW_FILES = sorted(
    p
    for p in (REPO_ROOT / ".github" / "workflows").iterdir()
    if p.suffix in (".yml", ".yaml")
)


@pytest.mark.parametrize("workflow", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_needs_outputs_reference_resolves(workflow):
    """No `needs.<job>.outputs.<key>` may reference an undeclared key.

    This is the last hop and the one with no schema behind it: GitHub silently
    resolves an unknown output to "", every `if:` comparing it to 'true' goes
    false, and the job is SKIPPED while the run stays green.
    """
    text = workflow.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or not doc.get("jobs"):
        pytest.skip(f"{workflow.name} declares no jobs")
    dangling = []
    for job_name, key in sorted(
        set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", text))
    ):
        declared = _workflow_job_outputs(doc, job_name)
        if declared is None:
            dangling.append(f"needs.{job_name}.outputs.{key} (no job '{job_name}')")
        elif key not in declared:
            dangling.append(
                f"needs.{job_name}.outputs.{key} "
                f"(job '{job_name}' declares {sorted(declared)})"
            )
    assert not dangling, (
        f"{workflow.name} reads workflow outputs that are never declared: "
        f"{dangling}. GitHub resolves these to '' — every gated job goes "
        "SKIPPED and CI stays green, so the lane is silently off."
    )


# --------------------------------------------------------------------------
# CONTRACT: the FOURTH hop — `steps.<id>.outputs.<key>` inside CONSUMING
# workflow files.
#
# The chain is four hops, not three:
#
#   script $GITHUB_OUTPUT key
#     -> action.yml `outputs:`                        (pinned above)
#     -> `steps.<id>.outputs.<key>` in the CONSUMER   (THIS test)
#     -> job `outputs:` / step `env:`
#     -> `needs.<job>.outputs.<key>`                  (pinned above)
#
# `test_action_outputs_forward_the_same_named_classify_key` only reads
# action.yml. The consumers carry their OWN `steps.classify.outputs.X`
# references — ci.yaml's `detect` job forwards 15 of them, docker.yml's gate
# step reads one in `env:`, nix.yml forwards one — and nothing read those.
# Measured 2026-09-19 on this branch: renaming ci.yaml's
# `steps.classify.outputs.python_prod` to `.pythonn_prod` (kills the
# Playwright/e2e-desktop gate) and docker.yml's `.docker` to `.dockerr`
# (kills the whole docker build lane) BOTH left the suite at 225 passed.
#
# Same fail-CLOSED shape as every other hop: GitHub resolves an unknown step
# output to "", `"" == 'true'` is false, the lane is SKIPPED, the run is green.
#
# Scope is the CLASS, not the three files that happen to consume
# detect-changes: every job in every workflow and every step list in every
# local composite action. Refs to remote actions (`actions/upload-artifact@…`)
# and to `run:` steps are skipped — their output keys are not declared in this
# repo, so there is nothing to resolve against. Measured: 21 resolvable
# local-composite refs, 47 run-step refs, 7 remote-action refs, 0 dangling.
# --------------------------------------------------------------------------

ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ACTION_FILES = sorted(ACTIONS_DIR.rglob("action.y*ml"))

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_STEP_OUTPUT_REF = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")


def _local_composite_outputs(uses: object) -> set[str] | None:
    """Declared `outputs:` of a local (`./…`) action, else None.

    None means "not resolvable from this repo" — a `run:` step (uses is None)
    or a pinned third-party action, neither of which declares its outputs here.
    """
    if not isinstance(uses, str) or not uses.startswith("./"):
        return None
    base = REPO_ROOT / uses[2:]
    for candidate in (base / "action.yml", base / "action.yaml", base):
        if candidate.is_file():
            doc = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            return set((doc or {}).get("outputs") or {})
    return set()  # `uses: ./…` pointing at nothing — every ref dangles.


def _step_output_refs(node: object) -> set[tuple[str, str]]:
    """Every `steps.<id>.outputs.<key>` inside a `${{ }}` expression."""
    text = yaml.safe_dump(node, default_flow_style=False, sort_keys=True)
    refs: set[tuple[str, str]] = set()
    for expression in _EXPRESSION.findall(text):
        refs |= set(_STEP_OUTPUT_REF.findall(expression))
    return refs


def _step_scopes(doc: dict):
    """(label, steps, node) for each independent `steps.*` namespace.

    A composite action has ONE namespace (`runs.steps`) but references it from
    the whole file — its top-level `outputs:` block is where the step values
    escape, so the scanned node is the entire doc, not just `runs:`.
    """
    if isinstance(doc.get("runs"), dict):  # a composite action
        yield "action", doc["runs"].get("steps") or [], doc
    for job_name, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            yield f"job {job_name}", job.get("steps") or [], job


def _dangling_step_output_refs(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return []
    dangling = []
    for label, steps, node in _step_scopes(doc):
        uses_by_id = {
            step["id"]: step.get("uses")
            for step in steps
            if isinstance(step, dict) and step.get("id")
        }
        for step_id, key in sorted(_step_output_refs(node)):
            if step_id not in uses_by_id:
                dangling.append(
                    f"[{label}] steps.{step_id}.outputs.{key} "
                    f"(no step with id '{step_id}'; ids: {sorted(uses_by_id)})"
                )
                continue
            declared = _local_composite_outputs(uses_by_id[step_id])
            if declared is None:
                continue  # run: step or remote action — not resolvable here.
            if key not in declared:
                dangling.append(
                    f"[{label}] steps.{step_id}.outputs.{key} "
                    f"({uses_by_id[step_id]} declares {sorted(declared)})"
                )
    return dangling


@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES + ACTION_FILES,
    ids=lambda p: str(p.relative_to(REPO_ROOT / ".github")),
)
def test_every_step_output_reference_resolves(path):
    """No `steps.<id>.outputs.<key>` may name a key its action never declares.

    Checked for refs the repo can resolve: a step whose `uses:` is a local
    composite action. GitHub resolves an unknown step output to "" with no
    warning, so the consuming `if:`/`env:` sees the empty string, the lane
    goes OFF, and the run stays green — the fail-CLOSED direction.
    """
    dangling = _dangling_step_output_refs(path)
    assert not dangling, (
        f"{path.relative_to(REPO_ROOT)} reads step outputs that are never "
        f"declared: {dangling}. These resolve to '' at runtime — the gated "
        "lane is silently skipped and CI still reports green."
    )
