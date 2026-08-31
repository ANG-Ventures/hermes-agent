"""Dispatcher-to-CLI seam coverage for Kanban worker chat identity.

Kanban workers are spawned as ``hermes ... chat -q``, which constructs the
agent through ``CLIAgentSetupMixin._init_agent``.  ``hermes_cli.oneshot`` is the
separate ``-z`` path, so testing or fixing only that module leaves dispatched
worker turns anonymous.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


def _task() -> kb.Task:
    return kb.Task(
        id="t_identity",
        title="worker identity",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _capture_spawn_env(monkeypatch, tmp_path) -> dict[str, str]:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _home: False)

    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert kb._default_spawn(_task(), str(workspace), board="identity-board") == 4242
    return captured["env"]  # type: ignore[return-value]


class _FakeCLI(CLIAgentSetupMixin):
    pass


def _fake_cli() -> _FakeCLI:
    noop = lambda *_args, **_kwargs: None
    cli = _FakeCLI()
    cli.__dict__.update(vars(SimpleNamespace(
        agent=None,
        _install_tool_callbacks=noop,
        _ensure_tirith_security=noop,
        finalize_preloaded_skills=noop,
        _ensure_runtime_credentials=lambda: True,
        _current_reasoning_callback=lambda: None,
        _session_db=object(),
        _resumed=False,
        conversation_history=[],
        api_key="test-key",
        base_url="https://example.invalid/v1",
        provider="test",
        requested_provider="test",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        model="test-model",
        max_tokens=None,
        max_turns=3,
        enabled_toolsets=[],
        disabled_toolsets=[],
        verbose=False,
        tool_progress_mode="off",
        system_prompt="",
        prefill_messages=[],
        reasoning_config=None,
        service_tier=None,
        _providers_only=None,
        _providers_ignore=None,
        _providers_order=None,
        _provider_sort=None,
        _provider_require_params=False,
        _provider_data_collection=None,
        _openrouter_min_coding_score=None,
        session_id="session-test",
        _clarify_callback=noop,
        _fallback_model=[],
        _on_thinking=noop,
        checkpoints_enabled=False,
        checkpoint_max_snapshots=20,
        checkpoint_max_total_size_mb=500,
        checkpoint_max_file_size_mb=10,
        pass_session_id=False,
        ignore_rules=False,
        _on_tool_progress=noop,
        _inline_diffs_enabled=False,
        _on_tool_start=noop,
        _on_tool_complete=noop,
        streaming_enabled=False,
        _stream_delta=noop,
        _on_tool_gen_start=noop,
        _on_notice=noop,
        _on_notice_clear=noop,
        _on_reaction=noop,
        _pending_title=False,
    )))
    return cli


def test_dispatcher_env_reaches_chat_q_agent_identity(monkeypatch, tmp_path):
    """The real dispatcher env must identify the agent built by ``chat -q``."""
    env = _capture_spawn_env(monkeypatch, tmp_path)
    assert env["HERMES_KANBAN_TASK"] == "t_identity"
    assert env["HERMES_KANBAN_BOARD"] == "identity-board"
    monkeypatch.setenv("HERMES_KANBAN_TASK", env["HERMES_KANBAN_TASK"])
    monkeypatch.setenv("HERMES_KANBAN_BOARD", env["HERMES_KANBAN_BOARD"])

    import cli as cli_mod
    from agent import credits_tracker
    from hermes_cli import mcp_startup

    captured: dict[str, object] = {}

    def fake_agent(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(cli_mod, "AIAgent", fake_agent)
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
    monkeypatch.setattr(cli_mod, "_prepare_deferred_agent_startup", lambda: None)
    monkeypatch.setattr(mcp_startup, "wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr(credits_tracker, "seed_credits_at_session_start", lambda _agent: None)

    assert _fake_cli()._init_agent() is True
    assert captured["chat_id"] == "t_identity"
    assert captured["chat_name"] == "kanban / identity-board / t_identity"
