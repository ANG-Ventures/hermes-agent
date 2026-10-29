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

import json
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
    funcs = {"fromJSON": json.loads, "contains": lambda haystack, needle: needle in haystack}
    return eval("(" + text + ")", {"__builtins__": {}}, funcs)


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
    npm_restore = "node-cache-self-hosted" if save == "true" else "node-cache-restore"
    for restore in ("tools-cache-restore", "python-cache-restore", npm_restore):
        assert _cond(steps[restore]["if"], ctx), restore


def test_setup_pm_self_hosted_npm_restore_reads_the_namespace_hosted_saves():
    """Prism 956a8deebe07: a self-hosted npm restore must look up keys a save writes."""
    steps = _setup_steps()
    saved, restored = steps["node-cache"]["with"], steps["node-cache-self-hosted"]["with"]
    save_prefixes = [k.strip() for k in saved["restore-keys"].splitlines() if k.strip()]
    restore_prefixes = [k.strip() for k in restored["restore-keys"].splitlines() if k.strip()]
    assert restore_prefixes and set(restore_prefixes) <= set(save_prefixes)
    assert restored["key"] == saved["key"]
    assert all(saved["key"].startswith(prefix) for prefix in restore_prefixes)


def test_setup_pm_hosted_still_saves_by_default():
    steps = _setup_steps()
    ctx = {"inputs": _CACHE_ON, "runner": {"environment": "github-hosted"}, "steps": {}}
    for save in ("tools-cache", "python-cache", "node-cache"):
        assert _cond(steps[save]["if"], ctx), save
    for restore in ("tools-cache-restore", "python-cache-restore", "node-cache-restore", "node-cache-self-hosted"):
        assert not _cond(steps[restore]["if"], ctx), restore


# ── t_58979a5e: the ONE self-hosted job that warms the caches ───────────────

_TESTS = WORKFLOWS / "tests.yml"
_WARM = WORKFLOWS / "warm-selfhosted-cache.yml"
_SELF_HOSTED_LABELS = '["self-hosted","linux","x64","ci-box","hermes-ci"]'
_USES_LOCAL = re.compile(r"^\./\.github/workflows/([A-Za-z0-9._-]+\.ya?ml)$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tests_jobs() -> dict:
    return _load(_TESTS)["jobs"]


def _warm_job() -> dict:
    return _load(_WARM)["jobs"]["warm-selfhosted-cache"]


def _setup_pm_with(job: dict) -> dict:
    (step,) = [s for s in job["steps"] if s.get("uses") == "./.github/actions/setup-pm"]
    return dict(step.get("with") or {})


def _reachable_workflows(root: str) -> set[str]:
    """Every workflow file reachable from ``root`` through local ``uses:`` calls."""
    seen, todo = set(), [root]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for job in (_load(WORKFLOWS / name).get("jobs") or {}).values():
            m = _USES_LOCAL.match(str(job.get("uses", "")))
            if m:
                todo.append(m.group(1))
    return seen


def test_save_on_self_hosted_turns_every_save_back_on():
    """The warm job saves all three caches and runs none of the restore-only steps."""
    steps = _setup_steps()
    inputs = {**_CACHE_ON, "save-on-self-hosted": "true"}
    ctx = {"inputs": inputs, "runner": {"environment": "self-hosted"}, "steps": {}}
    for save in ("tools-cache", "python-cache", "node-cache"):
        assert _cond(steps[save]["if"], ctx), save
    for restore in ("tools-cache-restore", "python-cache-restore", "node-cache-restore", "node-cache-self-hosted"):
        assert not _cond(steps[restore]["if"], ctx), restore


def test_save_on_self_hosted_defaults_off():
    raw = yaml.safe_load(SETUP_PM.read_text(encoding="utf-8"))["inputs"]["save-on-self-hosted"]
    assert raw["default"] == "false"


def test_exactly_one_job_saves_on_self_hosted_and_it_is_the_warm_job():
    """A second saving job would put the 0.1 MB/s upload back on a gating job."""
    savers = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for name, job in (_load(path).get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if str((step.get("with") or {}).get("save-on-self-hosted", "")).lower() == "true":
                    savers.append(f"{path.name}:{name}")
    assert savers == ["warm-selfhosted-cache.yml:warm-selfhosted-cache"]


def test_warm_job_writes_the_keys_the_slices_restore():
    """Same setup-pm inputs as the slice job, so the saved keys are the restored keys."""
    warm = _setup_pm_with(_warm_job())
    assert warm.pop("save-on-self-hosted") == "true"
    assert warm == _setup_pm_with(_tests_jobs()["test"])


def test_warm_job_has_its_own_budget_and_never_gates_main():
    """Prism cea075482a07: a job inside reusable tests.yml rolls up into the caller's
    `tests` result, which all-checks-pass reads. The warm workflow must be top-level
    and unreachable from either CI orchestrator."""
    # ~225 MB at the measured 0.1 MB/s is ~38 min.
    assert _warm_job()["timeout-minutes"] >= 60
    on = _load(_WARM).get("on", _load(_WARM).get(True))
    assert "workflow_call" not in on
    assert on["push"]["branches"] == ["main"]
    assert set(on) <= {"push", "workflow_dispatch"}
    for orchestrator in ("ci.yaml", "ci-local.yaml"):
        assert _WARM.name not in _reachable_workflows(orchestrator), orchestrator
    assert "warm-selfhosted-cache" not in _tests_jobs()


@pytest.mark.parametrize(
    "ref,labels,switch,runs",
    [
        ("refs/heads/main", _SELF_HOSTED_LABELS, None, True),
        ("refs/heads/main", _SELF_HOSTED_LABELS, "off", False),
        ("refs/heads/main", '["ubuntu-latest"]', None, False),
        ("refs/heads/main", None, None, False),
        ("refs/heads/feature", _SELF_HOSTED_LABELS, None, False),
    ],
)
def test_warm_job_runs_only_on_main_on_a_self_hosted_pool(ref, labels, switch, runs):
    warm = _warm_job()
    vars_ = {k: v for k, v in {"CI_RUNNER_LABELS": labels, "CI_SELFHOSTED_CACHE_WARM": switch}.items() if v}
    ctx = {"github": {"event_name": "push", "ref": ref}, "vars": vars_}
    assert bool(_cond(warm["if"], ctx)) is runs
    if runs:
        assert _eval(warm["runs-on"], ctx) == json.loads(labels)


def test_tools_key_ignores_pm_bytecode():
    """prepare imports pm and writes fresh .pyc files before the cache step hashes pm/**.

    Mirror run 40139260526: four slices of one sha missed on four different
    tools keys. Both the save and the restore key must exclude __pycache__.
    """
    steps = _setup_steps()
    keys = {steps[i]["with"]["key"] for i in ("tools-cache", "tools-cache-restore")}
    assert len(keys) == 1
    (key,) = keys
    hashed = re.search(r"hashFiles\((.*?)\)", key)[1]
    patterns = [p.strip().strip("'") for p in hashed.split(",")]
    assert "pm/**" in patterns
    assert "!pm/**/__pycache__/**" in patterns
    assert patterns.index("!pm/**/__pycache__/**") > patterns.index("pm/**")
