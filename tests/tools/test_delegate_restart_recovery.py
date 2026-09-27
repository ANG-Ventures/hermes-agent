"""Shared live/resume adapter coverage for delegate_task."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from gateway.session_context import get_session_env, restore_session_vars, set_session_vars
from agent.runtime_cwd import resolve_agent_cwd
from tools import delegate_tool as dt


def _parent():
    return SimpleNamespace(
        model="parent-model",
        provider="parent-provider",
        base_url="https://parent.invalid/v1",
        api_mode="chat_completions",
        enabled_toolsets=["file", "terminal"],
        reasoning_config={"effort": "high"},
        fallback_model=None,
        service_tier=None,
        provider_preferences=None,
        _delegate_depth=0,
        terminal_cwd="/tmp",
    )


def test_gateway_background_spec_persists_effective_settings_without_credentials(monkeypatch):
    tokens = set_session_vars(
        platform="telegram",
        chat_id="123",
        thread_id="7",
        user_id="u1",
        user_name="Ace",
        session_key="agent:main:telegram:dm:123:7",
        session_id="sess-parent",
        profile="work",
        async_delivery=True,
    )
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, boot_id = dt._build_durable_background_spec(
                task_list=[{"goal": "finish", "context": "draft", "role": "leaf"}],
                shared_context="shared",
                top_role="leaf",
                inherit_context=False,
                cfg={"resume_on_restart": True, "provider": "named-provider"},
                creds={
                    "model": "child-model",
                    "provider": "custom",
                    "base_url": "https://child.invalid/v1",
                    "api_mode": "chat_completions",
                    "api_key": "MUST-NOT-PERSIST",
                },
                parent_agent=_parent(),
                session_key="agent:main:telegram:dm:123:7",
                parent_session_id="sess-parent",
                origin_ui_session_id="",
                max_iterations=45,
            )
    finally:
        restore_session_vars(tokens)

    assert boot_id == "100:1.0"
    assert spec is not None
    assert spec["profile"] == "work"
    assert spec["source"]["kind"] == "single"
    assert spec["route"]["chat_type"] == "dm"
    assert spec["route"]["parent_session_id"] == "sess-parent"
    assert spec["execution"]["model"] == "child-model"
    assert spec["execution"]["credential_ref"]["source"] == "provider"
    assert spec["execution"]["credential_ref"]["provider"] == "named-provider"
    assert "MUST-NOT-PERSIST" not in json.dumps(spec)
    assert "api_key" not in json.dumps(spec)


def test_direct_endpoint_spec_persists_credential_reference_not_secret():
    parent = _parent()
    parent.provider = "openrouter"
    tokens = set_session_vars(
        platform="telegram",
        chat_id="123",
        session_key="agent:main:telegram:dm:123",
        session_id="sess-parent",
        profile="work",
    )
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, _ = dt._build_durable_background_spec(
                task_list=[{"goal": "finish", "context": None}],
                shared_context=None,
                top_role="leaf",
                inherit_context=False,
                cfg={
                    "resume_on_restart": True,
                    "base_url": "https://direct.invalid/v1",
                    "api_key": "OLD-MUST-NOT-PERSIST",
                },
                creds={
                    "model": "child-model",
                    "provider": "custom",
                    "base_url": "https://direct.invalid/v1",
                    "api_mode": "chat_completions",
                    "api_key": "OLD-MUST-NOT-PERSIST",
                },
                parent_agent=parent,
                session_key="agent:main:telegram:dm:123",
                parent_session_id="sess-parent",
                origin_ui_session_id="",
                max_iterations=45,
            )
    finally:
        restore_session_vars(tokens)

    assert spec is not None
    credential_ref = spec["execution"]["credential_ref"]
    assert credential_ref == {
        "source": "delegation_config",
        "parent_provider": "openrouter",
    }
    assert "OLD-MUST-NOT-PERSIST" not in json.dumps(spec)


def test_recovered_runner_reuses_delegate_task_with_continuation(monkeypatch):
    record = {
        "source": {
            "kind": "batch",
            "tasks": [
                {"goal": "one", "context": "draft one", "role": "leaf"},
                {"goal": "two", "context": None, "role": "leaf"},
            ],
        },
        "execution": {"max_iterations": 33, "workspace_hint": "/tmp"},
        "profile": "work",
        "route": {
            "platform": "telegram",
            "chat_id": "123",
            "session_key": "agent:main:telegram:dm:123",
            "parent_session_id": "sess-parent",
            "profile": "work",
        },
    }
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        captured["runtime_session_key"] = get_session_env("HERMES_SESSION_KEY", "")
        captured["runtime_profile"] = get_session_env("HERMES_SESSION_PROFILE", "")
        captured["runtime_cwd"] = str(resolve_agent_cwd())
        return json.dumps({"results": [{"task_index": 0}, {"task_index": 1}]})

    monkeypatch.setattr(dt, "delegate_task", fake_delegate_task)
    runner = dt.build_recovered_delegation_runner(
        record,
        "CONTINUE after restart",
        _parent(),
    )
    result = runner()

    assert len(result["results"]) == 2
    assert captured["background"] is False
    assert captured["_recovery_spec"] is record
    assert captured["max_iterations"] == 33
    assert captured["tasks"][0]["context"] == "draft one\n\nCONTINUE after restart"
    assert captured["tasks"][1]["context"] == "CONTINUE after restart"
    assert captured["runtime_session_key"] == "agent:main:telegram:dm:123"
    assert captured["runtime_profile"] == "work"
    assert captured["runtime_cwd"] == "/tmp"


def test_durable_spec_materializes_inherited_parent_context():
    parent = _parent()
    child = SimpleNamespace(
        prefill_messages=[{"role": "user", "content": "folded parent transcript"}]
    )
    tokens = set_session_vars(
        platform="telegram",
        chat_id="123",
        session_key="agent:main:telegram:dm:123",
        session_id="sess-parent",
        profile="work",
    )
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, _ = dt._build_durable_background_spec(
                task_list=[{"goal": "finish", "context": None, "inherit_context": True}],
                shared_context=None,
                top_role="leaf",
                inherit_context=True,
                cfg={"resume_on_restart": True},
                creds={"model": "child-model"},
                parent_agent=parent,
                session_key="agent:main:telegram:dm:123",
                parent_session_id="sess-parent",
                origin_ui_session_id="",
                max_iterations=45,
                children=[child],
            )
    finally:
        restore_session_vars(tokens)
    assert spec is not None
    task = spec["source"]["tasks"][0]
    assert task["materialized_prefill_messages"] == child.prefill_messages


# ── t_9fdac10c: a recovered child must keep its NAMED provider ─────────────
# Ledger signature of the defect: a subagent turn with provider='custom' whose
# parent config pins a named relay (claude-bpr), unkeyed bpr pool routes, and a
# cache_read plateau (read stuck at system+tools while cache_write re-grows by
# the whole history every call) right after async_delegation_redispatched.

_BPR_RUNTIME = {
    "provider": "claude-bpr",
    "base_url": "http://127.0.0.1:18811/v1",
    "api_key": "pool-key",
    "api_mode": "chat_completions",
}


def _named_provider_record(base_url="http://127.0.0.1:18811/v1"):
    return {
        "source": {"kind": "single", "tasks": [{"goal": "finish the research note", "context": None}]},
        "execution": {
            "model": "claude-haiku-4-5",
            "provider": "claude-bpr",
            "base_url": base_url,
            "api_mode": "chat_completions",
            "max_iterations": 10,
            "credential_ref": {"source": "provider", "provider": "claude-bpr", "custom_provider": None},
        },
        "route": {"parent_session_id": "sess-parent"},
    }


def _resolved_recovery_creds(record, runtime):
    captured = {}
    real = dt._resolve_delegation_credentials

    def spy(cfg, parent):
        captured["cfg"] = dict(cfg)
        captured["creds"] = real(cfg, parent)
        raise ValueError("stop after credential resolution")

    with patch.object(dt, "_resolve_delegation_credentials", spy), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=dict(runtime)):
        dt.delegate_task(
            tasks=record["source"]["tasks"],
            parent_agent=_parent(),
            _recovery_spec=record,
        )
    return captured


def test_recovered_named_provider_child_keeps_provider_identity():
    got = _resolved_recovery_creds(_named_provider_record(), _BPR_RUNTIME)
    assert got["creds"]["provider"] == "claude-bpr"
    assert got["creds"]["base_url"] == "http://127.0.0.1:18811/v1"
    assert got["creds"]["api_mode"] == "chat_completions"


def test_recovered_named_provider_reresolves_moved_endpoint():
    # The registry moved the sub (tailnet IP change): the persisted URL is stale.
    moved = dict(_BPR_RUNTIME, provider="claude-bpx-5", base_url="http://100.81.82.111:3556/v1")
    record = _named_provider_record(base_url="http://100.105.238.33:3556/v1")
    record["execution"]["provider"] = "claude-bpx-5"
    record["execution"]["credential_ref"]["provider"] = "claude-bpx-5"
    got = _resolved_recovery_creds(record, moved)
    assert got["cfg"]["base_url"] == ""
    assert got["creds"]["provider"] == "claude-bpx-5"
    assert got["creds"]["base_url"] == "http://100.81.82.111:3556/v1"


def test_recovered_direct_endpoint_still_pins_its_base_url():
    record = _named_provider_record(base_url="https://direct.invalid/v1")
    record["execution"]["provider"] = "custom"
    record["execution"]["credential_ref"] = {"source": "delegation_config", "parent_provider": "openrouter"}
    got = _resolved_recovery_creds(record, dict(_BPR_RUNTIME, provider="custom", base_url=""))
    assert got["cfg"]["base_url"] == "https://direct.invalid/v1"
    assert got["creds"]["provider"] == "custom"
    assert got["creds"]["base_url"] == "https://direct.invalid/v1"


def test_config_provider_plus_its_own_base_url_keeps_provider_identity():
    # delegation: {provider: claude-bpr, base_url: <claude-bpr's endpoint>} is the
    # same shape outside recovery; it must not collapse to "custom" either.
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=dict(_BPR_RUNTIME)):
        creds = dt._resolve_delegation_credentials(
            {"model": "claude-haiku-4-5", "provider": "claude-bpr",
             "base_url": "http://127.0.0.1:18811/v1/", "api_key": "k"},
            _parent(),
        )
    assert creds["provider"] == "claude-bpr"


def test_config_provider_with_foreign_base_url_stays_custom():
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=dict(_BPR_RUNTIME)):
        creds = dt._resolve_delegation_credentials(
            {"model": "m", "provider": "claude-bpr", "base_url": "http://localhost:9999/v1", "api_key": "k"},
            _parent(),
        )
    assert creds["provider"] == "custom"
