"""CI placement probe (t_eb230c34): the synthetic P4 bench must exercise the REAL Placement path.

ci-placement-probe.yml is only worth its numbers if its Placement job is tests.yml's Placement job and
its producer uploads the artifacts tests.yml's generate uploads. These contracts compare the two
workflow files directly, so editing one without the other fails here.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PLACEMENT_IF = "vars.CI_OVERFLOW_PLACEMENT_ENABLED == 'true'"


def _load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if True in data:  # YAML 1.1 parses `on` as True
        data["on"] = data.pop(True)
    return data


def _step(job: dict, name: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("name") == name]
    return step


def test_probe_is_dispatch_only():
    probe = _load("ci-placement-probe.yml")
    assert set(probe["on"]) == {"workflow_dispatch"}
    assert set(probe["on"]["workflow_dispatch"]["inputs"]) == {"wave", "exchange"}
    assert probe["permissions"] == {"contents": "read"}


def test_probe_job_names_match_the_controller_synthetic_profile():
    # fleet-ops-scripts ci-overflow-controller config.json `synthetic`: producer/placement job names.
    jobs = _load("ci-placement-probe.yml")["jobs"]
    assert set(jobs) == {"generate", "placement"}
    assert jobs["generate"]["name"] == "Generate slices"
    assert jobs["placement"]["name"] == "Placement"


def test_probe_placement_job_is_tests_yml_placement_job():
    """Everything but `if:` (tests.yml also gates on merge_group) is identical: steps, env, outputs."""
    real = dict(_load("tests.yml")["jobs"]["placement"])
    probe = dict(_load("ci-placement-probe.yml")["jobs"]["placement"])
    assert real.pop("if") == f"github.event_name == 'merge_group' && {PLACEMENT_IF}"
    assert probe.pop("if") == PLACEMENT_IF
    assert probe == real


def test_probe_producer_uploads_what_tests_yml_generate_uploads():
    real = _load("tests.yml")["jobs"]["generate"]
    probe = _load("ci-placement-probe.yml")["jobs"]["generate"]
    for name in ("Upload CI overflow request", "Upload local matrix for placement"):
        assert _step(probe, name)["uses"] == _step(real, name)["uses"]
        assert _step(probe, name)["with"] == _step(real, name)["with"]
    assert _step(probe, "Upload CI overflow request")["id"] == "request"
    assert probe["outputs"]["request_digest"] == real["outputs"]["request_digest"]
    assert _step(probe, "Upload local matrix for placement")["if"] == PLACEMENT_IF
    build = _step(probe, "Build CI overflow request and local matrix")
    assert "--event merge_group" in build["run"]
    assert build["env"]["MANAGED_PLACEMENT"] == _step(real, "Build CI overflow request and local matrix")["env"][
        "MANAGED_PLACEMENT"]


def test_probe_restores_the_duration_cache_before_slicing_like_tests_yml():
    """Without tests.yml's restore, LPT slices on 2.0 s defaults and the request differs from merge_group's."""
    real = _load("tests.yml")["jobs"]["generate"]
    probe = _load("ci-placement-probe.yml")["jobs"]["generate"]
    name = "Restore duration cache"
    assert _step(probe, name) == _step(real, name)
    names = [s.get("name") for s in probe["steps"]]
    assert names.index(name) < names.index("Generate test slices")


def test_probe_slice_count_is_ci_yaml_merge_group_branch():
    """The controller derives N from ci.yaml for merge_group; the probe must emit that same N."""
    ci = _load("ci.yaml")["jobs"]["tests"]["with"]["slice_count"]
    inner = _step(_load("ci-placement-probe.yml")["jobs"]["generate"], "Generate test slices")["env"]["SLICES"]
    assert inner.startswith("${{ ") and inner.endswith(" }}")
    assert ci == "${{ github.event_name == 'merge_group' && (" + inner[4:-3] + ") || 16 }}"


def test_probe_carries_no_secrets():
    text = (WORKFLOWS / "ci-placement-probe.yml").read_text(encoding="utf-8")
    assert "secrets." not in text
