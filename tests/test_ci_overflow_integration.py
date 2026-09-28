"""Behavior contracts for the P2c integration gates (scripts/ci_overflow_integration.py).

Workflow fixtures carry the exact P2b expressions (PR #938 @ a3cee862); the re-run fixtures are
the job listings and log lines measured live on probe run 35966312383 (2026-09-24)."""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("ci_overflow_integration", SCRIPTS / "ci_overflow_integration.py")
integ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integ)

IF = "always() && !cancelled() && needs.generate.result == 'success'"
MATRIX = ("${{ fromJSON(github.event_name != 'merge_group' && needs.generate.outputs.matrix || "
          "needs.placement.result == 'success' && needs.placement.outputs.plan_valid == 'true' && "
          "needs.placement.outputs.matrix || needs.generate.outputs.matrix) }}")
# t_42bed567: no plan -> the static split (CI_RUNNER_LABELS), never the all-local pool.
LOCAL_POOL = "'[\"self-hosted\",\"Linux\",\"X64\",\"hermes-ci\"]'"
STATIC_E2E = ("(contains(fromJSON(vars.CI_RUNNER_LABELS || '[\"ubuntu-latest\"]'), 'self-hosted') && "
              "format('[\"{0}\",\"X64\"]', join(fromJSON(vars.CI_RUNNER_LABELS), '\",\"')) || '[\"ubuntu-latest\"]')")
E2E_RUNS_ON = ("${{ fromJSON(needs.placement.result == 'success' && needs.placement.outputs.plan_valid == 'true' && "
               "needs.placement.outputs.e2e_runs_on || " + STATIC_E2E + ") }}")


def workflow(test_if=IF, matrix=MATRIX, e2e_if=IF, e2e_runs_on=E2E_RUNS_ON):
    def q(s):
        return "'" + s.replace("'", "''") + "'"
    return f"""
jobs:
  test:
    needs: [generate, placement]
    if: {q(test_if)}
    strategy:
      matrix: {q(matrix)}
  e2e:
    needs: [generate, placement]
    if: {q(e2e_if)}
    runs-on: {q(e2e_runs_on)}
"""


def test_p2b_fallback_predicate_passes():
    assert integ.fallback_predicate(workflow())["status"] == "PASS"


def test_real_tests_yml_passes_fallback_and_attempt_gates():
    text = (SCRIPTS.parent / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert integ.fallback_predicate(text)["status"] == "PASS", integ.fallback_predicate(text)
    assert integ.plan_bound_to_attempt(text)["status"] == "PASS"


@pytest.mark.parametrize("mutant", [
    {"test_if": "!cancelled() && needs.generate.result == 'success'"},        # always() dropped
    {"e2e_if": "needs.generate.result == 'success'"},                          # e2e fallback dropped
    {"matrix": MATRIX.replace(" || needs.generate.outputs.matrix) }}", ") }}")},  # no static fallback
    {"matrix": MATRIX.replace("|| needs.generate.outputs.matrix) }}", "|| needs.generate.outputs.local_matrix) }}")},  # all-local
    {"matrix": MATRIX.replace("needs.placement.outputs.plan_valid == 'true' && ", "")},
    {"e2e_runs_on": E2E_RUNS_ON.replace(" || " + STATIC_E2E, "")},  # no static fallback
    {"e2e_runs_on": E2E_RUNS_ON.replace(STATIC_E2E, LOCAL_POOL)},  # all-local
])
def test_removing_any_fallback_guard_blocks(mutant):
    assert integ.fallback_predicate(workflow(**mutant))["status"] == "BLOCK"


def test_placement_plan_not_bound_to_current_attempt_blocks():
    # The shipped P2b shape: a selective re-run reuses attempt N's placement outputs.
    result = integ.plan_bound_to_attempt(workflow())
    assert result["status"] == "BLOCK"
    assert set(result["evidence"]["unbound_consumers"]) == {"test.strategy.matrix", "e2e.runs-on"}


def test_attempt_bound_consumers_pass():
    bind = "needs.placement.outputs.plan_attempt == format('{0}', github.run_attempt) && "
    bound = workflow(matrix=MATRIX.replace("needs.placement.result == 'success' && ", bind + "needs.placement.result == 'success' && "),
                     e2e_runs_on=E2E_RUNS_ON.replace("fromJSON(needs.placement.result", "fromJSON(" + bind + "needs.placement.result"))
    assert integ.plan_bound_to_attempt(bound)["status"] == "PASS"


# Measured: attempt 1 and attempt 2 (rerun-failed-jobs) of run 35966312383.
A1 = [{"name": "Python tests / Generate slices", "runner_name": "GitHub Actions 1000067780", "started_at": "2026-09-24T06:48:43Z"},
      {"name": "Python tests / Placement", "runner_name": "GitHub Actions 1000067781", "started_at": "2026-09-24T06:48:49Z"},
      {"name": "Python tests / Run tests a", "runner_name": "GitHub Actions 1000067782", "started_at": "2026-09-24T06:48:54Z"},
      {"name": "Python tests / Run tests b", "runner_name": "GitHub Actions 1000067783", "started_at": "2026-09-24T06:48:54Z"}]
A2 = [dict(j) for j in A1[:3]] + [{"name": "Python tests / Run tests b", "runner_name": "GitHub Actions 1000067784",
                                  "started_at": "2026-09-24T06:49:22Z"}]
A2_LOG = {"Python tests / Run tests b": "PROBE slice=b executing_attempt=2 planned_attempt=1 generated_attempt=1 "
                                        "placement_result=success runner=GitHub Actions 1000067784 image=ubuntu24"}


def test_measured_selective_rerun_is_a_stale_plan_block():
    result = integ.selective_rerun_verdict(A1, A2, A2_LOG)
    assert result["status"] == "BLOCK"
    assert result["evidence"]["executed"] == ["Python tests / Run tests b"]
    assert {"Python tests / Generate slices", "Python tests / Placement"} <= set(result["evidence"]["planners_reused"])


def test_rerun_that_replans_passes():
    # Full re-run shape: every job re-executed and the plan matches the executing attempt.
    fresh = [{**j, "runner_name": j["runner_name"] + "-x", "started_at": "2026-09-24T06:57:10Z"} for j in A1]
    logs = {j["name"]: "PROBE slice=b executing_attempt=3 planned_attempt=3" for j in fresh}
    assert integ.selective_rerun_verdict(A1, fresh, logs)["status"] == "PASS"


def test_reexecution_on_a_same_named_runner_still_counts_as_executed():
    # Self-hosted runner names repeat across jobs; only a NEW start time proves re-execution.
    same_name = [dict(j) for j in A1[:3]] + [{**A1[3], "started_at": "2026-09-24T06:49:22Z"}]
    result = integ.selective_rerun_verdict(A1, same_name, A2_LOG)
    assert result["evidence"]["executed"] == ["Python tests / Run tests b"]
    assert result["status"] == "BLOCK"


def test_rerun_that_executed_nothing_is_unverifiable_not_pass():
    assert integ.selective_rerun_verdict(A1, [dict(j) for j in A1], {})["status"] == "UNVERIFIABLE"


# -- C3 #954: identity scan must not PASS without scanning, and must scan every artifact ---------
def _identity_fakes(monkeypatch, artifacts, blobs, total=None):
    import base64
    import io
    import zipfile

    def zipped(text):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("f.txt", text)
        return buf.getvalue()

    def api(path):
        if path.startswith("repos/o/r/contents/.github/workflows?"):
            return [{"path": ".github/workflows/x.yml", "name": "x.yml"}]
        if path.startswith("repos/o/r/contents/"):
            return {"content": base64.b64encode(b"on: push").decode()}
        if path == "repos/o/r/actions/secrets":
            return {"secrets": [{"name": n} for n in sorted(integ.PRE_EXISTING_SECRETS)]}
        if path == "orgs/ANG-Ventures/actions/secrets":
            return {"total_count": 0}
        if path == "repos/o/r/environments":
            return {"environments": []}
        if "/artifacts?" in path:
            return {"total_count": len(artifacts) if total is None else total, "artifacts": artifacts}
        raise AssertionError(path)

    def api_bytes(path):
        if path.endswith("/logs"):
            return zipped("clean log")
        return zipped(blobs[path.split("/")[-2]])

    monkeypatch.setattr(integ, "api", api)
    monkeypatch.setattr(integ, "_api_bytes", api_bytes)


def test_identity_scan_without_a_run_is_unverifiable_not_pass(monkeypatch):
    _identity_fakes(monkeypatch, [], {})
    assert integ.identity_absent("o/r", "main", None)["status"] == "UNVERIFIABLE"


def test_identity_in_a_non_overflow_artifact_blocks(monkeypatch):
    _identity_fakes(monkeypatch, [{"id": 7, "name": "coverage"}], {"7": "BEGIN PRIVATE KEY"})
    res = integ.identity_absent("o/r", "main", 1)
    assert res["status"] == "BLOCK" and res["evidence"]["run_hits"] == ["artifact:coverage/f.txt"]


def test_incomplete_artifact_listing_fails_closed(monkeypatch):
    _identity_fakes(monkeypatch, [{"id": 7, "name": "coverage"}], {"7": "clean"}, total=101)
    assert integ.identity_absent("o/r", "main", 1)["status"] == "UNVERIFIABLE"


def test_clean_scanned_run_passes(monkeypatch):
    _identity_fakes(monkeypatch, [{"id": 7, "name": "coverage"}], {"7": "clean"})
    assert integ.identity_absent("o/r", "main", 1)["status"] == "PASS"
