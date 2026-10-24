"""Mirror-main runs complete instead of being cancelled (t_38da7416).

Two causes, measured on ANG-Ventures/hermes-agent-ci main:

* run 40124835454 @83bf634c was cancelled with 0 jobs: push:main shared ONE
  ``ci-refs/heads/main`` group, which holds one pending run, so the next push
  replaced it. Under ``vars.CI_MAIN_GROUP_PER_SHA == 'true'`` each main sha gets
  its own group, and every nested workflow keys push:main by run_id so two live
  main runs never cancel each other's children.
* run 40122483687 @63b25ec3 had 8 jobs "cancelled" whose steps all passed: the
  post-job ``actions/cache`` save uploaded at ~0.1 MB/s from the self-hosted pool
  and outlived ``timeout-minutes``. setup-pm now restores and never saves on a
  self-hosted runner.

The expressions are evaluated from the real YAML, not matched as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ORCHESTRATORS = ("ci.yaml", "ci-local.yaml")
SETUP_PM = ROOT / ".github" / "actions" / "setup-pm" / "action.yml"

_CTX = re.compile(r"\b(?:inputs|github|vars|runner|steps)(?:\.[\w-]+)+")


def _eval(expr, ctx: dict):
    """Evaluate a value: a bare ``${{ }}`` expression, or a template string with
    ``${{ }}`` interpolations (``ci-${{ ... }}``), as Actions does."""
    text = str(expr).strip()
    if not (text.startswith("${{") and text.endswith("}}") and text.count("${{") == 1):
        if "${{" not in text:
            return text
        return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(_expr(m[1], ctx)), text)
    return _expr(text.removeprefix("${{").removesuffix("}}"), ctx)


def _expr(text: str, ctx: dict):
    """The Actions expression subset these files use (&&, ||, !, ==, !=)."""

    def value(match):
        cur = ctx
        for bit in match[0].split("."):
            cur = cur.get(bit, "") if isinstance(cur, dict) else ""
        return repr(cur)

    text = _CTX.sub(value, text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    text = re.sub(r"\btrue\b", "True", text)
    text = re.sub(r"\bfalse\b", "False", text)
    return eval("(" + text + ")", {"__builtins__": {}}, {})


def _ctx(*, event="push", ref="refs/heads/main", sha="a" * 40, run_id=1, pr=None, per_sha="true",
         release=False, ref_type="branch"):
    github = {"event_name": event, "ref": ref, "sha": sha, "run_id": run_id, "ref_type": ref_type,
              "event": {"pull_request": {"number": pr}} if pr else {}}
    return {"github": github, "vars": {"CI_MAIN_GROUP_PER_SHA": per_sha} if per_sha else {},
            "inputs": {"release": release} if release else {}}


def _concurrency(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("concurrency") or {}


def _called_workflows() -> set[str]:
    called = set()
    for orch in ORCHESTRATORS:
        jobs = yaml.safe_load((WORKFLOWS / orch).read_text(encoding="utf-8"))["jobs"]
        for job in jobs.values():
            uses = str(job.get("uses", ""))
            if uses.startswith("./.github/workflows/"):
                called.add(uses.rsplit("/", 1)[1])
    return called


@pytest.mark.parametrize("orch", ORCHESTRATORS)
def test_orchestrator_main_push_is_one_group_per_sha_under_the_var(orch):
    conc = _concurrency(WORKFLOWS / orch)
    g = lambda **kw: _eval(conc["group"], _ctx(**kw))  # noqa: E731
    assert g(sha="a" * 40) != g(sha="b" * 40)
    assert _eval(conc["cancel-in-progress"], _ctx()) is False
    # Unset var: one shared main group, the pre-change behaviour (rollback = delete the var).
    assert g(sha="a" * 40, per_sha=None) == g(sha="b" * 40, per_sha=None) == "ci-refs/heads/main"
    # PRs still collapse per PR; non-main pushes per ref; a release per run.
    assert g(event="pull_request", ref="refs/pull/7/merge", pr=7, sha="c" * 40) == "ci-7"
    assert g(ref="refs/heads/other", sha="a" * 40) == g(ref="refs/heads/other", sha="b" * 40)
    assert g(release=True, run_id=5) != g(release=True, run_id=6)


def test_orchestrators_agree_on_concurrency():
    a, b = (_concurrency(WORKFLOWS / o) for o in ORCHESTRATORS)
    assert a == b


@pytest.mark.parametrize("workflow", sorted(_called_workflows()))
def test_nested_workflow_never_shares_a_group_across_main_runs(workflow):
    conc = _concurrency(WORKFLOWS / workflow)
    if not conc:
        return  # no group: GitHub never cancels it on concurrency
    one = _eval(conc["group"], _ctx(run_id=1, sha="a" * 40))
    two = _eval(conc["group"], _ctx(run_id=2, sha="b" * 40))
    assert one != two, f"{workflow}: push:main runs share group {one!r}; one main run cancels the other's child"
    cancel = conc.get("cancel-in-progress", False)
    assert not (_eval(cancel, _ctx()) if isinstance(cancel, str) else cancel)


def _cond(expr, ctx: dict):
    """A step ``if:`` is an expression with or without the ``${{ }}`` wrapper."""
    text = str(expr).strip()
    return _expr(text.removeprefix("${{").removesuffix("}}") if text.startswith("${{") else text, ctx)


def _setup_steps() -> dict:
    steps = yaml.safe_load(SETUP_PM.read_text(encoding="utf-8"))["runs"]["steps"]
    return {s["id"]: s for s in steps if "id" in s}


_CACHE_ON = {"cache": "true", "cache-python": "true", "cache-node": "true", "toolchain": "all",
             "test-environment": "true", "extras": "", "save-tools-cache": "true",
             "save-python-cache": "true", "save-node-cache": "true", "prune-python-cache": "true"}


@pytest.mark.parametrize("save", ["true", "false"])
def test_setup_pm_self_hosted_restores_and_never_saves(save):
    inputs = {**_CACHE_ON, "save-tools-cache": save, "save-python-cache": save, "save-node-cache": save}
    steps = _setup_steps()
    ctx = {"inputs": inputs, "runner": {"environment": "self-hosted"}, "steps": {}}
    saving = [i for i, s in steps.items() if str(s.get("uses", "")).startswith("actions/cache@") and _cond(s["if"], ctx)]
    assert saving == []
    for restore in ("tools-cache-restore", "python-cache-restore", "node-cache-restore"):
        assert _cond(steps[restore]["if"], ctx), restore


def test_setup_pm_hosted_still_saves_by_default():
    steps = _setup_steps()
    ctx = {"inputs": _CACHE_ON, "runner": {"environment": "github-hosted"}, "steps": {}}
    for save in ("tools-cache", "python-cache", "node-cache"):
        assert _cond(steps[save]["if"], ctx), save
    for restore in ("tools-cache-restore", "python-cache-restore", "node-cache-restore"):
        assert not _cond(steps[restore]["if"], ctx), restore
