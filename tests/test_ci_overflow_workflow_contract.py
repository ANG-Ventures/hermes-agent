"""CI overflow P2b: tests.yml wiring contracts (spec §5.1 CB1/RC1, §5.3, §5.3b).

The fallback checks evaluate the REAL ``if:``/``strategy.matrix``/``runs-on``
expressions from tests.yml with a small GitHub-expression evaluator, across
every placement outcome, so a regression in the YAML (not a copy of it) fails.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_overflow_placement import (PlanInvalid, gate, poll, summary_markdown, validate_record,
                                           write_outputs)
from scripts.ci_overflow_plan import ARM, POOL, X64, parse_request
from scripts.ci_overflow_request import build_request, local_matrix

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
LEDGER = "ci-overflow-ledger"
IF_PREDICATE = "always() && !cancelled() && needs.generate.result == 'success'"


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data:  # YAML 1.1 parses `on` as True
        data["on"] = data.pop(True)
    return data


def _tests_yml() -> dict:
    return _load(WORKFLOWS / "tests.yml")


# ── minimal GitHub Actions expression evaluator ───────────────────────────
_TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+)|(?P<op>==|!=|&&|\|\||[!(),])"
                    r"|(?P<name>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)*))")


class _Parser:
    """Parse to an AST so && / || short-circuit exactly like GitHub's evaluator."""

    def __init__(self, text: str):
        self.toks, pos = [], 0
        text = text.strip()
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise SyntaxError(f"cannot tokenize at {text[pos:pos + 20]!r}")
            self.toks.append((m.lastgroup, m.group(m.lastgroup)))
            pos = m.end()
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise SyntaxError(f"expected {value!r}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.binary(0)
        if self.i != len(self.toks):
            raise SyntaxError(f"trailing tokens {self.toks[self.i:]}")
        return node

    _LEVELS = (("||",), ("&&",), ("==", "!="))

    def binary(self, level):
        if level == len(self._LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while self.peek()[1] in self._LEVELS[level]:
            op = self.take()[1]
            node = ("op", op, node, self.binary(level + 1))
        return node

    def unary(self):
        if self.peek()[1] == "!":
            self.take()
            return ("not", self.unary())
        kind, value = self.take()
        if value == "(":
            inner = self.binary(0)
            self.take(")")
            return inner
        if kind == "str":
            return ("lit", value[1:-1].replace("''", "'"))
        if kind == "num":
            return ("lit", int(value))
        if kind == "name":
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                while self.peek()[1] != ")":
                    args.append(self.binary(0))
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")")
                return ("call", value, args)
            if value in ("true", "false", "null"):
                return ("lit", {"true": True, "false": False, "null": None}[value])
            return ("ref", value)
        raise SyntaxError(f"unexpected {value!r}")


def _truthy(v) -> bool:
    return v not in (False, None, "", 0)


def _call(name, args, status):
    if name in ("always", "cancelled", "success", "failure"):
        return status[name]
    if name == "fromJSON":
        if not isinstance(args[0], str) or not args[0]:
            raise ValueError(f"fromJSON received a missing/empty value: {args[0]!r}")
        return json.loads(args[0])
    if name == "contains":
        return args[1] in args[0]
    if name == "join":
        return args[1].join(args[0])
    if name == "format":
        return re.sub(r"\{(\d+)\}", lambda m: str(args[1 + int(m.group(1))]), args[0])
    raise NotImplementedError(name)


def evaluate(text: str, ctx: dict, status: dict):
    inner = text.strip()
    if inner.startswith("${{"):
        inner = inner[3:-2]
    return _eval(_Parser(inner).parse(), ctx, status)


def _eval(node, ctx, status):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ref":
        cur = ctx
        for part in node[1].split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur
    if kind == "not":
        return not _truthy(_eval(node[1], ctx, status))
    if kind == "call":
        return _call(node[1], [_eval(a, ctx, status) for a in node[2]], status)
    _, op, left, right = node
    lv = _eval(left, ctx, status)
    if op == "&&":
        return _eval(right, ctx, status) if _truthy(lv) else lv
    if op == "||":
        return lv if _truthy(lv) else _eval(right, ctx, status)
    rv = _eval(right, ctx, status)
    same = lv.lower() == rv.lower() if isinstance(lv, str) and isinstance(rv, str) else lv == rv
    return same if op == "==" else not same


# ── scenario harness over the real YAML ───────────────────────────────────
GEN_MATRIX = {"slice": [{"index": 1, "name": "slice 1/2", "files": "tests/a.py", "runs_on": '["ubuntu-latest"]'},
                        {"index": 2, "name": "slice 2/2", "files": "tests/b.py", "runs_on": '["self-hosted","hermes-ci"]'}]}
PLACED = copy.deepcopy(GEN_MATRIX)
PLACED["slice"][0]["runs_on"] = json.dumps(X64, separators=(",", ":"))
PLACED["slice"][1]["runs_on"] = json.dumps(POOL, separators=(",", ":"))

PLACEMENT_OUTCOMES = {
    "skipped": {"result": "skipped", "outputs": {}},
    "valid": {"result": "success", "outputs": {"plan_valid": "true", "matrix": json.dumps(PLACED),
                                               "e2e_runs_on": json.dumps(X64)}},
    "invalid": {"result": "success", "outputs": {"plan_valid": "false"}},
    # the explicit flag, not the presence of a matrix, is what authorises it
    "invalid-with-matrix": {"result": "success", "outputs": {"plan_valid": "false", "matrix": json.dumps(PLACED),
                                                             "e2e_runs_on": json.dumps(X64)}},
    "failure": {"result": "failure", "outputs": {}},
    "timeout": {"result": "failure", "outputs": {}},
    "cancelled": {"result": "cancelled", "outputs": {}},
    # job-level continue-on-error may surface a failed placement as success
    "failure-continue-on-error": {"result": "success", "outputs": {}},
    # killed after writing plan_valid but job failed: must still fall back
    "failed-after-output": {"result": "failure", "outputs": {"plan_valid": "true", "matrix": json.dumps(PLACED),
                                                             "e2e_runs_on": json.dumps(X64)}},
}


def _ctx(event, placement, runner_labels, enabled=False):
    return {"github": {"event_name": event}, "vars": {"CI_RUNNER_LABELS": runner_labels,
                                                       "CI_OVERFLOW_PLACEMENT_ENABLED": "true" if enabled else ""},
            "needs": {"generate": {"result": "success",
                                   "outputs": {"matrix": json.dumps(GEN_MATRIX),
                                               "local_matrix": json.dumps(local_matrix(GEN_MATRIX)) if event == "merge_group" and enabled else ""}},
                      "placement": placement}}


def _job_runs(job: dict, ctx: dict) -> bool:
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    status = {"always": True, "cancelled": False, "failure": False,
              "success": all(ctx["needs"][n]["result"] == "success" for n in needs)}
    cond = job.get("if", "success()")
    if not re.search(r"\b(always|success|failure|cancelled)\(", cond):
        cond = f"success() && ({cond})"  # GitHub's implicit status check
    return bool(_truthy(evaluate(cond, ctx, status)))


def check_fallback(doc: dict) -> list[str]:
    """Every placement outcome × event: test/e2e run, on the right matrix/labels."""
    jobs, errors = doc["jobs"], []
    local = json.dumps(local_matrix(GEN_MATRIX))
    for event in ("pull_request", "push", "merge_group"):
        for enabled in (False, True):
            for outcome, placement in PLACEMENT_OUTCOMES.items():
                for labels in (None, '["self-hosted","hermes-ci"]'):
                    ctx = _ctx(event, copy.deepcopy(placement), labels, enabled)
                    where = f"{event}/enabled={enabled}/{outcome}/labels={labels}"
                    status = {"always": True, "cancelled": False, "failure": False, "success": True}
                    try:
                        runs_placement = _job_runs(jobs["placement"], ctx)
                        if runs_placement != (event == "merge_group" and enabled):
                            errors.append(f"{where}: placement {'ran' if runs_placement else 'skipped'} unexpectedly")
                        # GitHub skips placement when the switch is OFF, regardless of a
                        # stale output in the synthetic input.
                        if not runs_placement:
                            ctx["needs"]["placement"] = copy.deepcopy(PLACEMENT_OUTCOMES["skipped"])
                        for name in ("test", "e2e"):
                            if not _job_runs(jobs[name], ctx):
                                errors.append(f"{where}: {name} skipped")
                        matrix = evaluate(jobs["test"]["strategy"]["matrix"], ctx, status)
                        e2e = evaluate(jobs["e2e"]["runs-on"], ctx, status)
                    except (ValueError, SyntaxError, KeyError, TypeError) as exc:
                        errors.append(f"{where}: {exc}")
                        continue
                    legacy_e2e = ["self-hosted", "hermes-ci", "X64"] if labels else ["ubuntu-latest"]
                    if event != "merge_group" or not enabled:
                        want, want_e2e = GEN_MATRIX, legacy_e2e
                    elif outcome == "valid":
                        want, want_e2e = PLACED, X64
                    else:
                        want, want_e2e = json.loads(local), POOL
                    if matrix != want:
                        errors.append(f"{where}: wrong matrix selected")
                    if e2e != want_e2e:
                        errors.append(f"{where}: e2e runs-on {e2e} != {want_e2e}")
    return errors


# ── workflow contract ─────────────────────────────────────────────────────
def test_fallback_integration_all_outcomes():
    assert check_fallback(_tests_yml()) == []


@pytest.mark.parametrize("job", ["test", "e2e"])
def test_mutating_always_fallback_out_fails_integration(job):
    doc = _tests_yml()
    # `!cancelled()` is itself a status function, so only removing the whole
    # status clause re-arms GitHub's implicit success() and skips the job.
    doc["jobs"][job]["if"] = doc["jobs"][job]["if"].replace("always() && !cancelled() && ", "")
    assert any(f"{job} skipped" in e for e in check_fallback(doc))
    del doc["jobs"][job]["if"]
    assert any(f"{job} skipped" in e for e in check_fallback(doc))


def test_mutating_plan_valid_guard_out_fails_integration():
    doc = _tests_yml()
    doc["jobs"]["test"]["strategy"]["matrix"] = doc["jobs"]["test"]["strategy"]["matrix"].replace(
        " && needs.placement.outputs.plan_valid == 'true'", "")
    assert check_fallback(doc)


def test_mutating_managed_fallback_to_legacy_matrix_fails_integration():
    doc = _tests_yml()
    doc["jobs"]["test"]["strategy"]["matrix"] = doc["jobs"]["test"]["strategy"]["matrix"].replace(
        "needs.generate.outputs.local_matrix", "needs.generate.outputs.matrix")
    assert any("wrong matrix" in e for e in check_fallback(doc))


def test_static_routing_switch_mutation_fails_integration():
    doc = _tests_yml()
    doc["jobs"]["placement"]["if"] = "github.event_name == 'merge_group'"
    assert any("placement ran unexpectedly" in e for e in check_fallback(doc))
    doc = _tests_yml()
    doc["jobs"]["test"]["strategy"]["matrix"] = doc["jobs"]["test"]["strategy"]["matrix"].replace(
        " || vars.CI_OVERFLOW_PLACEMENT_ENABLED != 'true'", "")
    assert any("missing/empty" in e for e in check_fallback(doc))
    doc = _tests_yml()
    doc["jobs"]["e2e"]["runs-on"] = doc["jobs"]["e2e"]["runs-on"].replace(
        " || vars.CI_OVERFLOW_PLACEMENT_ENABLED != 'true'", "")
    assert any("e2e runs-on" in e for e in check_fallback(doc))


def test_if_predicates_exact():
    jobs = _tests_yml()["jobs"]
    for name in ("test", "e2e"):
        assert jobs[name]["if"] == IF_PREDICATE
        assert jobs[name]["needs"] == ["generate", "placement"]
    assert jobs["placement"]["if"] == "github.event_name == 'merge_group' && vars.CI_OVERFLOW_PLACEMENT_ENABLED == 'true'"


def test_placement_job_shape_and_permissions_exact():
    job = _tests_yml()["jobs"]["placement"]
    assert job["needs"] == "generate"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 5
    assert job["permissions"] == {"contents": "read"}
    assert job["continue-on-error"] is True  # a dead placement must not fail required checks
    assert set(job["outputs"]) == {"matrix", "e2e_runs_on", "plan_valid"}


def test_generate_emits_local_matrix_and_request_artifact():
    gen = _tests_yml()["jobs"]["generate"]
    assert {"matrix", "local_matrix", "request_digest"} <= set(gen["outputs"])
    upload = next(s for s in gen["steps"] if s.get("id") == "request")
    assert upload["with"]["name"] == "ci-overflow-request-${{ github.run_id }}-${{ github.run_attempt }}"
    assert upload["with"]["path"] == "ci-overflow/request.json"
    step = next(s for s in gen["steps"] if s.get("id") == "overflow")
    assert step["env"]["MANAGED_PLACEMENT"] == "${{ vars.CI_OVERFLOW_PLACEMENT_ENABLED == 'true' }}"
    assert 'if [[ "$EVENT_NAME" == "merge_group" && "$MANAGED_PLACEMENT" == "true" ]]' in step["run"]
    local_upload = next(s for s in gen["steps"] if s.get("name") == "Upload local matrix for placement")
    assert local_upload["if"] == _tests_yml()["jobs"]["placement"]["if"]


def test_generate_step_emits_local_output_only_on_managed_merge_group(tmp_path):
    step = next(s for s in _tests_yml()["jobs"]["generate"]["steps"] if s.get("id") == "overflow")
    (tmp_path / "ci-overflow").mkdir()
    (tmp_path / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    (tmp_path / "ci-overflow" / "matrix.json").write_text(json.dumps(GEN_MATRIX), encoding="utf-8")
    for event, enabled in (("merge_group", "false"), ("pull_request", "true"), ("merge_group", "true")):
        output = tmp_path / "output"
        output.write_text("", encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "EVENT_NAME": event,
               "MANAGED_PLACEMENT": enabled, "GITHUB_OUTPUT": str(output)}
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert output.read_text().startswith("local_matrix=") == (event == "merge_group" and enabled == "true")
        assert (tmp_path / "ci-overflow" / "request.json").exists()


def test_static_merge_group_summary_reports_legacy_policy(tmp_path):
    src = tmp_path / "matrix.json"
    src.write_text(json.dumps(GEN_MATRIX), encoding="utf-8")
    for enabled in ("", "true"):
        summary = tmp_path / "summary.md"
        summary.write_text("", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/ci_overflow_request.py"),
                               "--matrix-file", str(src), "--out-dir", str(tmp_path / "out"),
                               "--event", "merge_group", "--managed", enabled],
                              env={"GITHUB_STEP_SUMMARY": str(summary), "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr
        assert ("policy=legacy, excluded_from_overflow_budget" in summary.read_text()) == (enabled != "true")


def test_matrices_never_travel_through_env():
    """A full matrix is ~350 KB; one env string over 128 KB fails the step with E2BIG
    (measured: PR run 35892008853, `Argument list too long`)."""
    for job in _tests_yml()["jobs"].values():
        for step in job.get("steps", []):
            for key, value in (step.get("env") or {}).items():
                assert "outputs.matrix" not in value and "outputs.local_matrix" not in value, (key, value)
                assert "toJSON(needs" not in value, (key, value)  # measured E2BIG, run 35892732551


def test_fromjson_never_fed_a_possibly_missing_output():
    """Every fromJSON over placement/local outputs is guarded or ends in a literal/total output."""
    text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    for expr in re.findall(r"fromJSON\(([^()]*(?:\([^()]*\))*[^()]*)\)", text):
        if "needs.placement.outputs" in expr:
            assert "needs.placement.result == 'success'" in expr and "plan_valid == 'true'" in expr
            assert expr.rstrip().endswith(("local_matrix", "'[\"self-hosted\",\"Linux\",\"X64\",\"hermes-ci\"]'"))


def test_gate_cli_reads_results_from_env(tmp_path):
    base = {"PATH": "/usr/bin:/bin", "GENERATE_RESULT": "success", "TEST_RESULT": "success", "E2E_RESULT": "success"}
    run = lambda env: subprocess.run([sys.executable, str(ROOT / "scripts/ci_overflow_placement.py"), "gate"],
                                     env=env, capture_output=True, text=True, timeout=60, cwd=ROOT).returncode
    assert run(base) == 0
    assert run({**base, "TEST_RESULT": "skipped"}) == 1
    assert run({**base, "E2E_RESULT": ""}) == 1
    step = _tests_yml()["jobs"]["tests-complete"]["steps"][-1]
    assert step["env"] == {"GENERATE_RESULT": "${{ needs.generate.result }}", "TEST_RESULT": "${{ needs.test.result }}",
                           "E2E_RESULT": "${{ needs.e2e.result }}"}


def test_aggregate_fails_skipped_required_work():
    job = _tests_yml()["jobs"]["tests-complete"]
    assert job["if"] == "always()"
    assert {"generate", "test", "e2e"} <= set(job["needs"])
    ok = {"generate": {"result": "success"}, "test": {"result": "success"}, "e2e": {"result": "success"},
          "placement": {"result": "failure"}}
    assert gate(ok) == []
    for job_name in ("test", "e2e", "generate"):
        for result in ("skipped", "failure", "cancelled"):
            bad = copy.deepcopy(ok)
            bad[job_name]["result"] = result
            assert gate(bad), (job_name, result)


def test_one_tests_call_site_and_no_secrets_on_candidate_surfaces():
    ci = _load(WORKFLOWS / "ci.yaml")
    calls = [k for k, j in ci["jobs"].items() if str(j.get("uses", "")).endswith("/tests.yml")]
    assert calls == ["tests"]
    assert "merge_group" in ci["on"]
    def walk(node):
        if isinstance(node, dict):
            assert "secrets" not in node, node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            assert "secrets." not in node and "secrets[" not in node, node
    for name in ("tests.yml", "ci.yaml"):
        walk(_load(WORKFLOWS / name))


def _branch_can_match(filters: dict | None, branch: str) -> bool:
    if filters is None:
        return True
    if "branches" in filters:
        return any(fnmatch.fnmatchcase(branch, p) for p in filters["branches"])
    if "branches-ignore" in filters:
        return not any(fnmatch.fnmatchcase(branch, p) for p in filters["branches-ignore"])
    return "tags" not in filters  # tags-only push filters never fire for a branch push


def test_ledger_branch_commit_starts_no_workflow():
    seen = 0
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        on = _load(path).get("on") or {}
        if isinstance(on, (str, list)):
            assert "push" not in on and "workflow_run" not in on, path.name
            continue
        for event in ("push", "workflow_run"):
            if event in on:
                seen += 1
                assert not _branch_can_match(on[event], LEDGER), f"{path.name} {event} fires on {LEDGER}"
    assert seen


# ── generator golden (real generator, real CLI) ───────────────────────────
def _generate(scope: str) -> dict:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/run_tests_parallel.py"), "--generate-slices", "16",
                           "--test-scope", scope, "--self-hosted-slots", "4", "--self-hosted-labels",
                           '["self-hosted","hermes-ci"]', "--arm-hosted-slices=3"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _file_set(matrix):
    return sorted((row["index"], row["name"], row["files"]) for row in matrix["slice"])


def test_static_merge_group_uses_real_generator_matrix():
    original = _generate("full")
    ctx = _ctx("merge_group", copy.deepcopy(PLACEMENT_OUTCOMES["skipped"]),
               '["self-hosted","hermes-ci"]', enabled=False)
    ctx["needs"]["generate"]["outputs"]["matrix"] = json.dumps(original)
    jobs = _tests_yml()["jobs"]
    assert not _job_runs(jobs["placement"], ctx)
    assert _job_runs(jobs["test"], ctx) and _job_runs(jobs["e2e"], ctx)
    status = {"always": True, "cancelled": False, "failure": False, "success": True}
    assert evaluate(jobs["test"]["strategy"]["matrix"], ctx, status) == original
    assert evaluate(jobs["e2e"]["runs-on"], ctx, status) == ["self-hosted", "hermes-ci", "X64"]


@pytest.mark.parametrize("scope", ["full", "plugin"])
def test_local_matrix_membership_identical_real_generator(tmp_path, scope):
    if scope == "plugin":
        plugin = next(p.name for p in sorted((ROOT / "tests/plugins").iterdir())
                      if p.is_dir() and any(p.glob("test_*.py")))
        scope = f"plugin:{plugin}"
    original = _generate(scope)
    out = tmp_path / "o"
    src = tmp_path / "matrix.json"
    src.write_text(json.dumps(original), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/ci_overflow_request.py"), "--matrix-file",
                           str(src), "--out-dir", str(out)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    local = json.loads((out / "local_matrix.json").read_text(encoding="utf-8"))
    assert _file_set(local) == _file_set(original)
    assert {row["runs_on"] for row in local["slice"]} == {json.dumps(POOL, separators=(",", ":"))}
    raw = (out / "request.json").read_bytes()
    req = parse_request(raw)
    assert [s["job_id"] for s in req.slices] == [row["name"] for row in original["slice"]]
    assert req.e2e["job_id"] == "e2e"
    assert set(json.loads(raw)) == {"slices", "e2e"}
    assert b"tests/" not in raw  # no test contents or file lists leak into the request


def test_request_weights_use_generator_fallback():
    matrix = {"slice": [{"index": 1, "name": "slice 1/1", "files": "tests/x.py:tests/y.py", "runs_on": "[]"}]}
    req = build_request(matrix, {"tests/x.py": 7.5}, ["tests/e2e/test_a.py"])
    assert req["slices"][0]["estimated_duration_s"] == 9.5
    assert req["e2e"]["estimated_duration_s"] == 2.0


# ── placement validator ───────────────────────────────────────────────────
IDENT = dict(repository_id=1234822999, run_id=42, run_attempt=2, head_sha="a" * 40, request_digest="sha256:" + "b" * 64)
MATRIX = {"slice": [{"index": 1, "name": "core smoke", "files": "tests/c.py", "runs_on": "[]"},
                    {"index": 2, "name": "slice 2/3", "files": "tests/d.py", "runs_on": "[]"},
                    {"index": 3, "name": "slice 3/3", "files": "tests/e.py", "runs_on": "[]"}]}


def _state(jobs=None, **summary):
    jobs = jobs if jobs is not None else [
        {"job_id": "core smoke", "labels": X64, "reason": "cloud-overflow", "reserved_minutes": 35},
        {"job_id": "e2e", "labels": POOL, "reason": "local-idle", "reserved_minutes": 0},
        {"job_id": "slice 2/3", "labels": ARM, "reason": "cloud-overflow", "reserved_minutes": 35},
        {"job_id": "slice 3/3", "labels": POOL, "reason": "local-idle", "reserved_minutes": 0}]
    s = {**IDENT, "policy_version": "0123456789abcdef", "validated": True, "mode": "overflow", "k_cap": 4,
         "remaining_allowance": 100, "snapshot": {"timestamp": "2026-09-23T00:00:00+00:00", "online": 3, "idle": 1,
                                                  "queued_matching_jobs": 0}}
    s.update(summary)
    key = f"{IDENT['repository_id']}:{IDENT['run_id']}:{IDENT['run_attempt']}"
    return {"version": 1, "daily_totals": {}, "attempts": {key: {"admitted_on": "2026-09-23", "terminal_on": None,
                                                                 "jobs": jobs, "plan": {"jobs": jobs, "incidents": [],
                                                                                        "summary": s}}}}


def _validate(state, **over):
    return validate_record(state, matrix=MATRIX, **{**IDENT, **over})


def test_valid_plan_relabels_without_touching_membership():
    got = _validate(_state())
    assert [(r["name"], r["files"]) for r in got["matrix"]["slice"]] == [(r["name"], r["files"]) for r in MATRIX["slice"]]
    assert [json.loads(r["runs_on"]) for r in got["matrix"]["slice"]] == [X64, ARM, POOL]
    assert got["e2e_runs_on"] == POOL


def test_plan_jobs_must_match_committed_admission():
    state = _state()
    state["attempts"]["1234822999:42:2"]["jobs"] = copy.deepcopy(state["attempts"]["1234822999:42:2"]["jobs"])
    state["attempts"]["1234822999:42:2"]["jobs"][0]["labels"] = POOL
    with pytest.raises(PlanInvalid, match="disagree"):
        _validate(state)


def test_no_record_yet_is_none():
    assert _validate(_state(), run_attempt=3) is None


@pytest.mark.parametrize("mutate", [
    lambda j: j[0].update(labels=["ubuntu-latest-4-cores"]),       # paid/larger runner
    lambda j: j[0].update(labels=["self-hosted", "macOS", "ARM64"]),  # Studio
    lambda j: j[0].update(labels=ARM),                               # core smoke on ARM
    lambda j: j[1].update(labels=ARM),                               # e2e on ARM
    lambda j: j.pop(),                                               # incomplete job-id set
    lambda j: j.append({"job_id": "slice 9/9", "labels": POOL, "reason": "x", "reserved_minutes": 0}),
    lambda j: j.append(dict(j[0])),                                  # duplicate id
])
def test_label_allowlist_and_job_set_completeness(mutate):
    state = _state()
    mutate(state["attempts"]["1234822999:42:2"]["plan"]["jobs"])
    with pytest.raises(PlanInvalid):
        _validate(state)


@pytest.mark.parametrize("over", [{"request_digest": "sha256:" + "c" * 64}, {"head_sha": "f" * 40},
                                  {"request_digest": ""}, {"head_sha": ""}])
def test_identity_mismatch_is_invalid(over):
    with pytest.raises(PlanInvalid):
        _validate(_state(), **over)


@pytest.mark.parametrize("summary", [{"validated": False}, {"validated": "true"}, {"run_attempt": 1},
                                     {"repository_id": "1234822999"}, {"policy_version": None}])
def test_unvalidated_or_mismatched_record_is_invalid(summary):
    with pytest.raises(PlanInvalid):
        _validate(_state(**summary))


def _clock():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def test_poll_times_out_without_record():
    clock, sleep = _clock()
    state = _state()
    placement, reason = poll(lambda: state, deadline_s=180, clock=clock, sleep=sleep, matrix=MATRIX,
                             **{**IDENT, "run_attempt": 9})
    assert placement is None and reason.startswith("timeout") and clock() <= 180


def test_poll_invalid_record_stops_immediately_and_read_errors_retry():
    clock, sleep = _clock()
    placement, reason = poll(lambda: _state(validated=False), clock=clock, sleep=sleep, matrix=MATRIX, **IDENT)
    assert placement is None and reason.startswith("invalid") and clock() == 0
    calls = iter([OSError("503"), ValueError("json"), _state()])

    def fetch():
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item
    placement, reason = poll(fetch, clock=clock, sleep=sleep, matrix=MATRIX, **IDENT)
    assert placement is not None and reason == "validated plan"


def test_outputs_invalid_emits_no_matrix(tmp_path):
    out = tmp_path / "out"
    write_outputs(str(out), None)
    assert out.read_text(encoding="utf-8") == "plan_valid=false\n"
    write_outputs(str(out), _validate(_state()))
    lines = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert lines["plan_valid"] == "true" and json.loads(lines["e2e_runs_on"]) == POOL


def test_place_without_digest_falls_back_without_network(tmp_path):
    out, summ, mfile = tmp_path / "out", tmp_path / "summary", tmp_path / "local_matrix.json"
    mfile.write_text(json.dumps(MATRIX), encoding="utf-8")
    env = {"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ), "CI_MATRIX_FILE": str(mfile),
           "CI_REPOSITORY": "ANG-Ventures/hermes-agent", "CI_REPOSITORY_ID": "1", "CI_RUN_ID": "2",
           "CI_RUN_ATTEMPT": "1", "CI_HEAD_SHA": "a" * 40, "CI_REQUEST_DIGEST": "", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/ci_overflow_placement.py"), "place"], env=env,
                          capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "plan_valid=false\n"
    assert "local fallback" in summ.read_text(encoding="utf-8")


def test_summary_table_columns():
    from datetime import datetime, timezone
    text = summary_markdown(_validate(_state()), "validated plan", datetime(2026, 9, 23, 0, 1, tzinfo=timezone.utc))
    for col in ("job/slice", "reason", "labels", "online/idle/queued", "K", "snapshot age", "mode", "reservation",
                "remaining", "policy"):
        assert col in text
    assert "| core smoke | cloud-overflow | ubuntu-latest | 3/1/0 | 4 | 60s | overflow | 35 | 100 | 0123456789abcdef |" in text
