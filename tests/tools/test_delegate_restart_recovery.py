"""Shared live/resume adapter coverage for delegate_task."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.session_context import get_session_env, restore_session_vars, set_session_vars
from agent.runtime_cwd import resolve_agent_cwd
from run_agent import AIAgent
from tools import delegate_tool as dt
from tools.delegate_tool_config import _resolve_child_fallback_chain


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


# ── t_620dc5cd: a background unit's child keeps the parent's fallback chain ─────
# The spec read ``fallback_model`` (no such AIAgent attribute), so every kanban/detached unit rebuilt
# its parent with no chain and its ``fallback_providers: auto`` child ran without fallback: under a
# saturated lane it sat through 15 x 503 where the in-process child failed over.

def _real_parent(fallback_model):
    return AIAgent(base_url="http://127.0.0.1:9/v1", api_key="k", provider="custom", model="parent-model",
                   quiet_mode=True, skip_memory=True, skip_context_files=True, enabled_toolsets=["file"],
                   fallback_model=fallback_model)


def _spec_for(parent):
    tokens = set_session_vars(platform="telegram", chat_id="123", session_key="agent:main:telegram:dm:123",
                              session_id="sess-parent", profile="work", async_delivery=True)
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, _ = dt._build_durable_background_spec(
                task_list=[{"goal": "finish", "context": None}], shared_context=None, top_role="leaf",
                inherit_context=False, cfg={"resume_on_restart": True}, creds={"model": "child-model"},
                parent_agent=parent, session_key="agent:main:telegram:dm:123", parent_session_id="sess-parent",
                origin_ui_session_id="", max_iterations=45)
    finally:
        restore_session_vars(tokens)
    return spec


def test_rebuilt_parent_gives_the_child_the_in_process_fallback_chain():
    chain = [{"provider": "claude-dtlr", "model": "claude-opus-5-5"}, {"provider": "openrouter", "model": "x/y"}]
    parent = _real_parent(chain)
    spec = _spec_for(parent)
    rebuilt = _real_parent(spec["execution"]["fallback_chain"])
    for pinned in (False, True):
        in_process = _resolve_child_fallback_chain(parent, {"fallback_providers": "auto"}, pinned,
                                                   child_provider="claude-alr", child_model="child-model")
        assert in_process
        assert _resolve_child_fallback_chain(rebuilt, {"fallback_providers": "auto"}, pinned,
                                             child_provider="claude-alr", child_model="child-model") == in_process


def test_credential_bearing_fallback_chain_is_not_persisted_and_spec_survives():
    parent = _real_parent([{"provider": "custom", "model": "m", "base_url": "https://x.invalid/v1",
                            "api_key": "MUST-NOT-PERSIST"}])
    spec = _spec_for(parent)
    assert spec is not None
    assert spec["execution"]["fallback_chain"] is None
    assert "MUST-NOT-PERSIST" not in json.dumps(spec)


def test_unpicklable_fallback_chain_never_takes_the_durable_spec_down(caplog):
    """A live chain may hold lock/client-bearing objects: the spec still builds (a deepcopy raised
    ``cannot pickle '_thread.lock'`` and every background delegation ran synchronously)."""
    parent = _real_parent([dict(rung) for rung in _CHAIN])
    parent._fallback_chain[0]["client_lock"] = threading.Lock()
    spec = _spec_for(parent)
    assert spec is not None
    assert spec["execution"]["fallback_chain"] == _CHAIN
    json.dumps(spec)
    parent._fallback_chain = MagicMock()  # not a list: not representable
    with caplog.at_level("WARNING", logger="tools.async_delegation_store"):
        spec = _spec_for(parent)
    assert spec is not None and spec["execution"]["fallback_chain"] is None
    assert "fallback chain not persisted" in caplog.text


def test_declared_fallback_policy_never_persists_a_nested_credential():
    """Prism c6cb98245680: the whole declared rung is checked, nested ``options`` included, and only
    the checked JSON copy persists."""
    from tools.async_delegation_store import persistable_fallback_policy

    leaky = [{"source": "s", "options": {"base_url": "https://user:SECRET@example.com/v1"}}]
    assert persistable_fallback_policy({"fallback_providers": leaky}) == {"fallback_providers": []}
    clean = [{"source": "s", "options": {"base_url": "https://example.com/v1"}, "lock": threading.Lock()}, "openrouter"]
    assert persistable_fallback_policy({"fallback_providers": clean}) == {
        "fallback_providers": [{"source": "s", "options": {"base_url": "https://example.com/v1"}}, "openrouter"]}


# ── Prism 3de445560b2a: recovery keeps the route owner's fallback POLICY, not only the chain ──
# A per-call ``credentials_cfg`` (``/review`` -> ``auxiliary.review``) is not replayed on recovery, so the
# recovered children took the GLOBAL delegation block's policy: with ``fallback_providers: auto`` a pinned
# review child failed over onto the parent's lanes its own block never named.

_GLOBAL_AUTO = {"resume_on_restart": True, "fallback_providers": "auto"}
_REVIEW_CFG = {"provider": "claude-bpr", "model": "claude-haiku-4-5", "base_url": "", "api_key": "", "api_mode": ""}
_CHAIN = [{"provider": "claude-dtlr", "model": "claude-opus-5-5"}, {"provider": "openrouter", "model": "x/y"}]


def _recovered_routing_cfg(parent, *, creds, routing_cfg):
    tokens = set_session_vars(platform="telegram", chat_id="123", session_key="agent:main:telegram:dm:123",
                              session_id="sess-parent", profile="work", async_delivery=True)
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, _ = dt._build_durable_background_spec(
                task_list=[{"goal": "review", "context": None}], shared_context=None, top_role="leaf",
                inherit_context=False, cfg=dict(_GLOBAL_AUTO), creds=creds, parent_agent=parent,
                session_key="agent:main:telegram:dm:123", parent_session_id="sess-parent",
                origin_ui_session_id="", max_iterations=45, routing_cfg=routing_cfg)
    finally:
        restore_session_vars(tokens)
    record = json.loads(json.dumps(spec))  # the durable round-trip
    with patch.object(dt, "_load_config", return_value=dict(_GLOBAL_AUTO)):
        got = _resolved_recovery_creds(record, _BPR_RUNTIME)
    return got["cfg"], _real_parent(record["execution"]["fallback_chain"])


def test_recovered_pinned_child_keeps_its_owners_no_fallback_policy():
    parent = _real_parent(_CHAIN)
    in_process = _resolve_child_fallback_chain(parent, _REVIEW_CFG, True, child_provider="claude-bpr",
                                               child_model="claude-haiku-4-5")
    assert in_process is None
    cfg, rebuilt = _recovered_routing_cfg(
        parent, creds={"model": "claude-haiku-4-5", "provider": "claude-bpr"}, routing_cfg=_REVIEW_CFG)
    assert _resolve_child_fallback_chain(rebuilt, cfg, True, child_provider="claude-bpr",
                                         child_model="claude-haiku-4-5") is None


def test_recovered_unpinned_child_still_inherits_the_parent_chain():
    parent = _real_parent(_CHAIN)
    in_process = _resolve_child_fallback_chain(parent, _GLOBAL_AUTO, False, child_model="parent-model")
    assert in_process
    cfg, rebuilt = _recovered_routing_cfg(parent, creds={"model": None}, routing_cfg=_GLOBAL_AUTO)
    assert _resolve_child_fallback_chain(rebuilt, cfg, False, child_model="parent-model") == in_process


# ── Prism 75587d571cd7 / e087e98e6bb8: the recovered policy and pin match the dispatched ones ──

def test_provider_name_fallback_list_round_trips_intact():
    from tools.async_delegation_store import persistable_fallback_policy

    declared = ["openrouter", {"provider": "claude-dtlr", "model": "claude-opus-5-5"}]
    persisted = json.loads(json.dumps(persistable_fallback_policy({"fallback_providers": declared})))
    assert persisted == {"fallback_providers": declared}


def test_recovered_model_only_pin_keeps_no_parent_chain():
    # Custom endpoint parent, delegation.model set, provider inherited, fallback_providers null.
    global_null = {"resume_on_restart": True, "fallback_providers": None}
    parent = _real_parent([{"provider": "openrouter", "model": "x/y"}])
    original = _resolve_child_fallback_chain(parent, global_null, True, child_model="child-model")
    assert original is None
    tokens = set_session_vars(platform="telegram", chat_id="123", session_key="agent:main:telegram:dm:123",
                              session_id="sess-parent", profile="work", async_delivery=True)
    try:
        with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
            spec, _ = dt._build_durable_background_spec(
                task_list=[{"goal": "finish", "context": None}], shared_context=None, top_role="leaf",
                inherit_context=False, cfg=dict(global_null), creds={"model": "child-model"},
                parent_agent=parent, session_key="agent:main:telegram:dm:123", parent_session_id="sess-parent",
                origin_ui_session_id="", max_iterations=45)
    finally:
        restore_session_vars(tokens)
    record = json.loads(json.dumps(spec))
    with patch.object(dt, "_load_config", return_value=dict(global_null)):
        got = _recovered_child_build(record)
    assert got["model"] == "child-model" and not got["override_provider"]
    rebuilt = _real_parent(record["execution"]["fallback_chain"])
    assert _resolve_child_fallback_chain(rebuilt, global_null, bool(got["model"]),
                                         child_model=got["model"]) == original


def _recovered_child_build(record):
    captured = {}

    def spy(**kwargs):
        captured.update(kwargs)
        raise _StopAtChildBuild

    with patch.object(dt, "_build_child_preserving_parent_tools", spy), pytest.raises(_StopAtChildBuild):
        dt.delegate_task(tasks=record["source"]["tasks"], parent_agent=_parent(), _recovery_spec=record)
    return captured


class _StopAtChildBuild(Exception):
    pass


def test_recovered_model_only_pin_is_not_alias_resolved_twice():
    # Prism 0418dcff8ba7: model_aliases.worker -> ("gpt-5.5", inherit), while "gpt-5.5" is itself an alias
    # for openai-codex. Dispatch resolved "worker" once; recovery must reuse that result, not re-resolve it.
    aliases = {"worker": ("gpt-5.5", None), "gpt-5.5": ("gpt-5.5", "openai-codex")}

    def resolve(model, provider):
        return (model, provider) if provider else aliases.get(model, (model, provider))

    global_null = {"resume_on_restart": True, "fallback_providers": None}
    with patch("hermes_cli.model_switch.resolve_model_pair_for_storage", side_effect=resolve):
        dispatched = dt._resolve_delegation_credentials({"model": "worker"}, _parent())
        assert (dispatched["model"], dispatched["provider"]) == ("gpt-5.5", None)
        tokens = set_session_vars(platform="telegram", chat_id="123", session_key="agent:main:telegram:dm:123",
                                  session_id="sess-parent", profile="work", async_delivery=True)
        try:
            with patch("gateway.status.get_current_boot_id", return_value="100:1.0"):
                spec, _ = dt._build_durable_background_spec(
                    task_list=[{"goal": "finish", "context": None}], shared_context=None, top_role="leaf",
                    inherit_context=False, cfg=dict(global_null), creds=dispatched,
                    parent_agent=_real_parent(None), session_key="agent:main:telegram:dm:123",
                    parent_session_id="sess-parent", origin_ui_session_id="", max_iterations=45)
        finally:
            restore_session_vars(tokens)
        record = json.loads(json.dumps(spec))
        with patch.object(dt, "_load_config", return_value=dict(global_null)):
            got = _recovered_child_build(record)
    assert got["model"] == "gpt-5.5"
    assert not got["override_provider"] and not got["override_base_url"]
