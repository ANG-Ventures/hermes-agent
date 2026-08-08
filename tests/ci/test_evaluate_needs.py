"""Tests for the CI all-checks-pass evaluator."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "evaluate_needs.py"
_spec = importlib.util.spec_from_file_location("evaluate_needs", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load evaluate_needs.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate_needs = _mod.evaluate_needs

_JOBS = (
    "detect",
    "tests",
    "lint",
    "js-tests",
    "e2e-desktop",
    "docs-site",
    "history-check",
    "contributor-check",
    "uv-lockfile",
    "lockfile-diff",
    "docker-lint",
    "supply-chain",
    "review-labels",
    "osv-scanner",
)
_FALSE_DETECT_OUTPUTS = {
    "python": "false",
    "frontend": "false",
    "site": "false",
    "scan": "false",
    "deps": "false",
    "npm_lock": "false",
    "docker_meta": "false",
    "mcp_catalog": "false",
    "ci_review": "false",
    "event_name": "pull_request",
}


def _needs(
    *,
    results: dict[str, str] | None = None,
    detect_outputs: dict[str, str] | None = None,
    job_outputs: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict]:
    needs = {name: {"result": "success", "outputs": {}} for name in _JOBS}
    needs["detect"]["outputs"] = {**_FALSE_DETECT_OUTPUTS, **(detect_outputs or {})}
    for name, result in (results or {}).items():
        needs[name]["result"] = result
    for name, outputs in (job_outputs or {}).items():
        needs[name]["outputs"] = outputs
    return needs


def _old_failed_jobs(needs: dict[str, dict]) -> list[str]:
    """The pre-fix evaluator: only literal ``failure`` was rejected."""
    return [name for name, info in needs.items() if info["result"] == "failure"]


def test_all_success_passes():
    assert evaluate_needs(_needs()) == {}


def test_failure_result_fails():
    violations = evaluate_needs(_needs(results={"lint": "failure"}))

    assert set(violations) == {"lint"}
    assert "failure" in violations["lint"]


def test_cancelled_result_fails_despite_old_logic_accepting_it():
    needs = _needs(results={"tests": "cancelled"})

    assert _old_failed_jobs(needs) == []
    violations = evaluate_needs(needs)
    assert set(violations) == {"tests"}
    assert "cancelled" in violations["tests"]


def test_unrecognized_result_fails():
    violations = evaluate_needs(_needs(results={"lint": "timed_out"}))

    assert set(violations) == {"lint"}
    assert "timed_out" in violations["lint"]


def test_tests_skipped_while_python_changed_fails():
    violations = evaluate_needs(
        _needs(results={"tests": "skipped"}, detect_outputs={"python": "true"})
    )

    assert set(violations) == {"tests"}
    assert "detect.outputs.python == 'true'" in violations["tests"]


def test_tests_skipped_while_python_unchanged_passes():
    assert evaluate_needs(_needs(results={"tests": "skipped"})) == {}


@pytest.mark.parametrize(
    ("job", "detect_outputs", "job_outputs", "reason"),
    [
        pytest.param(
            "lint",
            {"python": "true"},
            {},
            "detect.outputs.python == 'true'",
            id="lint-python",
        ),
        pytest.param(
            "js-tests",
            {"frontend": "true"},
            {},
            "detect.outputs.frontend == 'true'",
            id="js-tests-frontend",
        ),
        pytest.param(
            "docs-site",
            {"site": "true"},
            {},
            "detect.outputs.site == 'true'",
            id="docs-site",
        ),
        pytest.param(
            "history-check",
            {},
            {},
            "detect.outputs.event_name == 'pull_request'",
            id="history-check-pull-request",
        ),
        pytest.param(
            "contributor-check",
            {"python": "true"},
            {},
            "detect.outputs.python == 'true'",
            id="contributor-check-python",
        ),
        pytest.param(
            "lockfile-diff",
            {"npm_lock": "true"},
            {},
            "pull_request and detect.outputs.npm_lock == 'true'",
            id="lockfile-diff",
        ),
        pytest.param(
            "docker-lint",
            {"docker_meta": "true"},
            {},
            "detect.outputs.docker_meta == 'true'",
            id="docker-lint",
        ),
        pytest.param(
            "supply-chain",
            {"scan": "true"},
            {},
            "pull_request and (detect.outputs.scan == 'true' or detect.outputs.deps == 'true')",
            id="supply-chain-scan",
        ),
        pytest.param(
            "supply-chain",
            {"deps": "true"},
            {},
            "pull_request and (detect.outputs.scan == 'true' or detect.outputs.deps == 'true')",
            id="supply-chain-deps",
        ),
        pytest.param(
            "review-labels",
            {"ci_review": "true"},
            {},
            "pull_request and a review-label trigger is true",
            id="review-labels-ci-review",
        ),
        pytest.param(
            "review-labels",
            {"mcp_catalog": "true"},
            {},
            "pull_request and a review-label trigger is true",
            id="review-labels-mcp-catalog",
        ),
        pytest.param(
            "review-labels",
            {},
            {"supply-chain": {"critical_findings": "true"}},
            "pull_request and a review-label trigger is true",
            id="review-labels-supply-chain",
        ),
    ],
)
def test_classifier_required_job_cannot_be_skipped(
    job: str,
    detect_outputs: dict[str, str],
    job_outputs: dict[str, dict[str, str]],
    reason: str,
):
    violations = evaluate_needs(
        _needs(
            results={job: "skipped"},
            detect_outputs=detect_outputs,
            job_outputs=job_outputs,
        )
    )

    assert set(violations) == {job}
    assert reason in violations[job]


def test_js_tests_skipped_while_frontend_unchanged_passes():
    assert evaluate_needs(_needs(results={"js-tests": "skipped"})) == {}


def test_classifier_jobs_may_skip_when_no_condition_requires_them():
    classifier_jobs = {
        "tests",
        "lint",
        "js-tests",
        "e2e-desktop",
        "docs-site",
        "history-check",
        "contributor-check",
        "lockfile-diff",
        "docker-lint",
        "supply-chain",
        "review-labels",
    }

    assert (
        evaluate_needs(
            _needs(
                results={name: "skipped" for name in classifier_jobs},
                detect_outputs={"event_name": "push"},
            )
        )
        == {}
    )


def test_main_preserves_compact_needs_json_shape(tmp_path, monkeypatch, capsys):
    needs = {
        "detect": {"result": "success", "outputs": dict(_FALSE_DETECT_OUTPUTS)},
        "tests": {"result": "skipped", "outputs": {}},
    }
    output = tmp_path / "github-output"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(needs)))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert _mod.main() == 0

    expected = 'needs-json={"detect": "success", "tests": "skipped"}'
    assert capsys.readouterr().out.splitlines()[0] == expected
    assert output.read_text(encoding="utf-8") == expected + "\n"


# ── e2e-desktop: intentionally disabled (upstream #76627) ────────────────────


def test_e2e_desktop_may_skip_while_intentionally_disabled():
    """The classifier must NOT require a job ci.yml deliberately disables.

    ci.yml guards e2e-desktop with `false &&` because this branch takes
    upstream's apps/desktop verbatim and inherits their broken Playwright
    suite (#76627: the mock-backend Electron window never gets a title).
    Requiring it here would fail the umbrella gate on a skip we chose.
    """
    assert evaluate_needs(_needs(results={"e2e-desktop": "skipped"})) == {}
    assert (
        evaluate_needs(
            _needs(results={"e2e-desktop": "skipped"}, detect_outputs={"python": "true"})
        )
        == {}
    )


def test_e2e_desktop_failure_still_fails_the_gate():
    """Exempt from 'must run' is NOT exempt from 'must not fail'.

    If someone re-enables the job and it goes red, the umbrella must still
    catch it -- the exemption only forgives `skipped`, never `failure`.
    """
    violations = evaluate_needs(_needs(results={"e2e-desktop": "failure"}))
    assert set(violations) == {"e2e-desktop"}
    assert "failure" in violations["e2e-desktop"]


def test_e2e_desktop_exemption_matches_the_ci_yml_guard():
    """Pin the exemption to the ACTUAL ci.yml guard so the two cannot drift.

    The failure mode this prevents: upstream fixes #76627, someone deletes the
    `false &&` in ci.yml, and the classifier exemption silently survives -- so
    a genuinely-skipped required job stops being noticed again, which is the
    exact hole #476 closed. If the guard is gone, the exemption must go too.
    """
    import re
    from pathlib import Path

    ci_yml = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    text = ci_yml.read_text(encoding="utf-8")

    block = re.search(r"^  e2e-desktop:\n(?:.*\n)*?^    uses:", text, re.MULTILINE)
    assert block, "could not locate the e2e-desktop job block in ci.yml"
    disabled_in_ci = "false &&" in block.group(0)

    exempt_in_classifier = (
        evaluate_needs(
            _needs(results={"e2e-desktop": "skipped"}, detect_outputs={"python": "true"})
        )
        == {}
    )

    assert disabled_in_ci == exempt_in_classifier, (
        "ci.yml and evaluate_needs.py disagree about e2e-desktop: "
        f"disabled_in_ci={disabled_in_ci} exempt_in_classifier={exempt_in_classifier}. "
        "Re-enabling the job means deleting BOTH the `false &&` guard and the "
        "classifier exemption."
    )
