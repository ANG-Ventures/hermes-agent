"""Delegate and cron explicit-model flagship admission tests."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run_agent
from cron.jobs import create_job, get_job, update_job
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.cronjob_tools import cronjob


def _parent():
    return SimpleNamespace(_delegate_depth=0)


def test_delegate_schema_offers_explicit_route_and_reason():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert {"model", "provider", "allow_flagship_reason"} <= props.keys()


def test_delegate_rejects_explicit_flagship_before_spawning():
    result = delegate_task(goal="Diagnose concurrency failure", model="gpt-6-astra-900k", parent_agent=_parent())
    assert "--allow-flagship" in result


def test_delegate_resolves_alias_before_policy():
    with patch("hermes_cli.model_switch.resolve_model_pair_for_storage", return_value=("claude-fable-5", "claude-apr")):
        result = delegate_task(goal="Diagnose concurrency failure", model="premium", parent_agent=_parent())
    assert "--allow-flagship" in result


def test_delegate_justified_route_is_audited_and_credential_isolation():
    captured = {}

    def resolve(cfg, _parent):
        captured.update(cfg)
        return {"model": cfg.get("model"), "provider": cfg.get("provider"), "base_url": cfg.get("base_url"), "api_key": cfg.get("api_key"), "api_mode": cfg.get("api_mode"), "command": None, "args": None}

    with (patch("tools.delegate_tool._load_config", return_value={"model": "claude-opus-5", "provider": "claude-apr", "base_url": "old-endpoint", "api_key": "old-key", "api_mode": "anthropic"}),
          patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=resolve),
          patch("tools.delegate_tool.logger.info") as audit):
        result = delegate_task(model="gpt-6-astra-900k", provider="openai-codex", allow_flagship_reason="hard concurrency diagnosis", parent_agent=_parent())
    assert "No tasks provided" in result
    assert captured["model"] == "gpt-6-astra-900k"
    assert captured["provider"] == "openai-codex"
    assert captured["base_url"] == captured["api_key"] == captured["api_mode"] == ""
    assert "flagship override:" in str(audit.call_args)
    assert "hard concurrency diagnosis" in str(audit.call_args)


def test_delegate_dispatch_and_registry_forward_route():
    from tools.registry import registry
    captured = []
    with patch("tools.delegate_tool.delegate_task", side_effect=lambda **kw: captured.append(kw) or "{}"):
        args = {"goal": "Diagnose concurrency failure", "model": "claude-fable-5", "provider": "claude-apr", "allow_flagship_reason": "incident"}
        run_agent.AIAgent._dispatch_delegate_task(_parent(), args)
        registry.get_entry("delegate_task").handler(args, parent_agent=_parent())
    assert len(captured) == 2
    assert all(all(entry[key] == args[key] for key in ("model", "provider", "allow_flagship_reason")) for entry in captured)


def test_cron_create_rejects_explicit_flagship_and_accepts_audited_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    refused = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status", model="claude-fable-5", provider="claude-apr"))
    assert refused["success"] is False
    assert "--allow-flagship" in refused["error"]
    accepted = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status", model="claude-fable-5", provider="claude-apr", allow_flagship_reason="incident"))
    assert accepted["success"] is True
    job = get_job(accepted["job_id"])
    assert job["allow_flagship_reason"] == "incident"


def test_cron_store_blocks_direct_create_and_update_without_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="--allow-flagship"):
        create_job(prompt="Check status", schedule="every 1h", model="gpt-6-astra-900k")
    job = create_job(prompt="Check status", schedule="every 1h")
    with pytest.raises(ValueError, match="--allow-flagship"):
        update_job(job["id"], {"model": "gpt-6-astra-900k"})
    assert get_job(job["id"])["model"] is None
    updated = update_job(job["id"], {"model": "gpt-6-astra-900k", "allow_flagship_reason": "incident"})
    assert updated["allow_flagship_reason"] == "incident"


def test_cron_store_checks_resolved_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.model_switch.resolve_model_pair_for_storage", return_value=("gpt-6-astra-900k", "openai-codex")):
        with pytest.raises(ValueError, match="--allow-flagship"):
            create_job(prompt="Check status", schedule="every 1h", model="premium")


def test_cron_tool_update_refuses_flagship_without_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = create_job(prompt="Check status", schedule="every 1h")
    result = json.loads(cronjob(action="update", job_id=job["id"], model="claude-fable-5"))
    assert result["success"] is False
    assert "--allow-flagship" in result["error"]
    assert get_job(job["id"])["model"] is None


def test_cron_auto_pin_inherits_creating_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("tools.cronjob_tools._resolve_cron_llm_model", return_value=("claude-fable-5", "claude-apr")):
        result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status", model="auto"))
    assert result["success"] is True
    assert get_job(result["job_id"])["allow_flagship_reason"].startswith("auto-pin:")
