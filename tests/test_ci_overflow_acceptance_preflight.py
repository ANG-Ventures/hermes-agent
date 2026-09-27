"""Behavior contracts for the CI overflow Phase-0 fail-closed inventory."""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/ci_overflow_acceptance.py"
spec = importlib.util.spec_from_file_location("ci_overflow_acceptance", MODULE)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def test_schema_and_app_absence_gate(monkeypatch):
    monkeypatch.setattr(acceptance, "api", lambda path: {"installations": [{"app_slug": "other"}]})
    app = acceptance.app_capability("ANG-Ventures/hermes-agent")
    assert set(app) == {"name", "status", "evidence", "reason"}
    assert app["status"] == "BLOCK" and "App absent" in app["reason"]
    assert all(acceptance.gated_app_check(name, app)["status"] == "UNVERIFIABLE"
               for name in ("contents_cas_and_ruleset", "app_runner_read_and_fork_isolation"))


def test_incomplete_pr_inventory_is_not_pass(monkeypatch):
    monkeypatch.setattr(acceptance, "api", lambda path: {"sha": "head"})
    def broken_pages(path):
        raise RuntimeError("page 2 failed")
    monkeypatch.setattr(acceptance, "pages", broken_pages)
    assert acceptance.inventory("ANG-Ventures/hermes-agent")["status"] == "UNVERIFIABLE"


def test_one_call_site_requires_observed_producer(monkeypatch):
    workflow = {"on": {"merge_group": None}, "jobs": {"a": {"uses": "./.github/workflows/tests.yml"}}}
    monkeypatch.setattr(acceptance, "workflow_data", lambda path: workflow)
    def fixture_api(path):
        if path.endswith("actions/workflows?per_page=100"):
            return {"workflows": [{"id": 12, "path": ".github/workflows/ci.yaml", "state": "active"}]}
        if "/runs?" in path:
            return {"workflow_runs": [{"id": 34}]}
        return {"total_count": 1, "jobs": [{"name": "Python tests / Generate slices", "conclusion": "success", "id": 56}]}
    monkeypatch.setattr(acceptance, "api", fixture_api)
    assert acceptance.workflow_identity("ANG-Ventures/hermes-agent")["status"] == "PASS"
    workflow["jobs"]["b"] = {"uses": "./.github/workflows/tests.yml"}
    assert acceptance.workflow_identity("ANG-Ventures/hermes-agent")["status"] == "BLOCK"
    workflow["jobs"].pop("b")
    monkeypatch.setattr(acceptance, "api", lambda path: {"total_count": 2, "jobs": [{}]} if "/jobs?" in path else fixture_api(path))
    assert acceptance.workflow_identity("ANG-Ventures/hermes-agent")["status"] == "UNVERIFIABLE"


def test_utf16_budget_counts_both_matrices_and_placement(monkeypatch):
    full = {"slice": [{"index": i, "name": f"slice {i}", "files": "x" * 32, "runs_on": '["ubuntu-latest"]'} for i in range(16)]}
    scoped = {"slice": [full["slice"][0], {**full["slice"][1], "name": "core smoke"}]}
    monkeypatch.setattr(acceptance, "command", lambda *args, **kwargs: json.dumps(scoped if "plugin:memory" in args else full))
    result = acceptance.matrix_budget()
    assert result["status"] == "PASS"
    assert result["evidence"]["full"]["generate_bytes_utf16"] > acceptance.output_bytes("matrix=" + json.dumps(full))
    full["slice"][0]["files"] = "\U0001f600" * (750 * 1024 // 4)
    assert acceptance.matrix_budget()["status"] == "BLOCK"
    assert acceptance.output_bytes("\U0001f600") == 4


def test_trigger_inventory_never_converts_static_to_live_pass(monkeypatch, tmp_path):
    (tmp_path / "ci.yaml").write_text("on:\n  push:\n    branches: [main]\n")
    monkeypatch.setattr(acceptance, "WORKFLOWS", tmp_path)
    assert acceptance.trigger_inventory()["status"] == "UNVERIFIABLE"
    (tmp_path / "other.yml").write_text("on:\n  push:\n    branches: [ci-overflow-ledger-probe]\n")
    assert acceptance.trigger_inventory()["status"] == "BLOCK"


def test_probe_requires_data_only_commit_and_zero_runs(monkeypatch, tmp_path):
    (tmp_path / "ci.yaml").write_text("on:\n  push:\n    branches: [main]\n")
    monkeypatch.setattr(acceptance, "WORKFLOWS", tmp_path)
    def fixture_api(path):
        if "/commits/" in path:
            return {"files": [{"filename": "state.json"}]}
        return {"total_count": 0, "workflow_runs": []}
    monkeypatch.setattr(acceptance, "api", fixture_api)
    assert acceptance.trigger_inventory(probe_sha="probe")["status"] == "PASS"
    monkeypatch.setattr(acceptance, "api", lambda path: {"files": [{"filename": "workflow.yml"}]} if "/commits/" in path else fixture_api(path))
    assert acceptance.trigger_inventory(probe_sha="probe")["status"] == "BLOCK"


def test_app_permission_contract(monkeypatch):
    app = {"app_slug": acceptance.APP, "id": 4, "permissions": {"administration": "read", "actions": "read", "variables": "read", "contents": "write"}}
    monkeypatch.setattr(acceptance, "api", lambda path: {"installations": [app]})
    assert acceptance.app_capability("ANG-Ventures/hermes-agent")["status"] == "PASS"
    del app["permissions"]["contents"]
    assert acceptance.app_capability("ANG-Ventures/hermes-agent")["status"] == "BLOCK"


def test_unproven_external_gates_do_not_pass():
    assert acceptance.e2e_portability()["status"] == "BLOCK"
    assert acceptance.rate_preflight(acceptance.check("app", "BLOCK", {}, "App absent"))["status"] == "UNVERIFIABLE"
    assert acceptance.gated_app_check("contents", acceptance.check("app", "PASS", {}))["status"] == "UNVERIFIABLE"


def test_host_errors_do_not_pass(monkeypatch):
    monkeypatch.setattr(acceptance, "host_cache", lambda host: (_ for _ in ()).throw(RuntimeError("ssh unavailable")))
    result = acceptance.caches()
    assert result["status"] == "UNVERIFIABLE"
    assert "ace-ai" in result["reason"] and "ace-media" in result["reason"]


def test_unimplemented_subcommands_exit_two():
    import subprocess
    for action in ("caches", "verify-run"):
        proc = subprocess.run(["python3", str(MODULE), action], capture_output=True, text=True)
        assert proc.returncode == 2 and "not implemented" in proc.stderr
