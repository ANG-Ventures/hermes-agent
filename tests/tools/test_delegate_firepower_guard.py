"""Delegate and cron explicit-model flagship admission tests."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
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
          patch("tools.delegate_tool._build_child_preserving_parent_tools", return_value=SimpleNamespace()),
          patch("tools.delegate_tool._run_single_child", return_value={"task_index": 0, "status": "completed", "summary": "done", "api_calls": 1, "duration_seconds": 0}),
          patch("tools.delegate_tool.logger.info") as audit):
        result = delegate_task(goal="Diagnose concurrency failure", model="gpt-6-astra-900k", provider="openai-codex", allow_flagship_reason="hard concurrency diagnosis", parent_agent=_parent())
    assert "done" in result
    assert captured["model"] == "gpt-6-astra-900k"
    assert captured["provider"] == "openai-codex"
    assert captured["base_url"] == captured["api_key"] == captured["api_mode"] == ""
    assert "flagship override:" in str(audit.call_args)
    assert "hard concurrency diagnosis" in str(audit.call_args)


def test_delegate_invalid_request_has_no_success_override_audit():
    with (patch("tools.delegate_tool._resolve_delegation_credentials", return_value={"model": "gpt-6-astra-900k", "provider": "openai-codex"}),
          patch("tools.delegate_tool.logger.info") as audit):
        result = delegate_task(model="gpt-6-astra-900k", provider="openai-codex", allow_flagship_reason="incident", parent_agent=_parent())
    assert "No tasks provided" in result
    assert not any("flagship override:" in str(call) for call in audit.call_args_list)


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
    from tools.cronjob_tools import set_current_agent_model
    set_current_agent_model("claude-apr", "claude-fable-5")
    try:
        result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status", model="auto"))
    finally:
        set_current_agent_model(None, None)
    assert result["success"] is True
    assert get_job(result["job_id"])["allow_flagship_reason"].startswith("auto-pin:")


def test_cron_literal_flagship_default_is_not_disguised_as_agent_auto_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.cronjob_tools import set_current_agent_model
    set_current_agent_model(None, None)
    with patch("hermes_cli.config.load_config", return_value={"cron": {"default_model": "claude-fable-5", "default_provider": "claude-apr"}}):
        refused = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status"))
        assert refused["success"] is False
        assert "--allow-flagship" in refused["error"]
        accepted = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status", allow_flagship_reason="incident"))
    assert accepted["success"] is True
    job = get_job(accepted["job_id"])
    assert job["model"] == "claude-fable-5"
    assert job["allow_flagship_reason"] == "incident"


def test_cron_config_auto_inherits_creating_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.cronjob_tools import set_current_agent_model
    set_current_agent_model("openai-codex", "gpt-6-astra-900k")
    try:
        with patch("hermes_cli.config.load_config", return_value={"cron": {"default_model": "auto"}}):
            result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Check status"))
    finally:
        set_current_agent_model(None, None)
    assert result["success"] is True
    job = get_job(result["job_id"])
    assert job["model"] == "gpt-6-astra-900k"
    assert job["allow_flagship_reason"].startswith("auto-pin:")


def test_cron_store_cannot_erase_flagship_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = create_job(prompt="Check status", schedule="every 1h", model="gpt-6-astra-900k", allow_flagship_reason="incident")
    with pytest.raises(ValueError, match="--allow-flagship"):
        update_job(job["id"], {"allow_flagship_reason": None})
    assert get_job(job["id"])["allow_flagship_reason"] == "incident"


@pytest.mark.parametrize("creator_model", ["claude-opus-5", "claude-fable-5", None])
def test_interleaved_turn_cannot_auto_pin_other_agents_flagship(tmp_path, monkeypatch, caplog, creator_model):
    """Tool-executor admission must bind the creator, not the last published turn."""
    from agent import tool_executor as te
    from tools import cronjob_tools as ct
    from tools.thread_context import propagate_context_to_thread

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"default_model": "auto"}})
    monkeypatch.setattr("agent.relay_tools.execute", lambda name, args, dispatch, **kw: (dispatch(args), args))
    monkeypatch.setattr("hermes_cli.middleware.apply_tool_request_middleware", lambda name, args, **kw: SimpleNamespace(payload=args, trace=[]))
    monkeypatch.setattr("hermes_cli.middleware.run_tool_execution_middleware", lambda name, args, dispatch, **kw: dispatch(args))
    monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks", lambda *a, **kw: (None, None))
    monkeypatch.setattr(te, "_begin_tool_execution", lambda *a, **kw: None)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **kw: None)
    agent = SimpleNamespace(
        model=creator_model, provider="claude-apr", session_id="creator-A",
        _current_turn_id="turn-A", _current_api_request_id="",
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)),
        _touch_activity=lambda *a: None,
    )
    try:
        ct.set_current_agent_model(agent.provider, agent.model)  # turn A publishes
        ct.set_current_agent_model("claude-apr", "gpt-6-astra-900k")  # turn B interleaves
        with caplog.at_level(logging.INFO), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(propagate_context_to_thread(
                lambda: te._run_agent_tool_execution_middleware(
                    agent, function_name="cronjob",
                    function_args={"action": "create", "schedule": "every 1h", "prompt": "Check status"},
                    effective_task_id="A", tool_call_id="cron-A", execute=lambda args: ct.cronjob(**args),
                )
            )).result(timeout=15)
        created = json.loads(result.result)
        assert created["success"] is True
        job = get_job(created["job_id"])
        assert job is not None
        assert job["model"] == creator_model
        if creator_model == "claude-fable-5":
            assert job["allow_flagship_reason"].startswith("auto-pin:")
            assert any("flagship override: cron" in record.message for record in caplog.records)
        else:
            assert not job.get("allow_flagship_reason")
            assert not any("flagship override: cron" in record.message for record in caplog.records)
    finally:
        ct.set_current_agent_model(None, None)
