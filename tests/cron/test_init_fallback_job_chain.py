"""Init-time (resolve-failure) fallback must walk the job's OWN ``fallback`` chain.

Regression (2026-09-21, debug-log-analysis / weekly-pr-sweep): the codex primary
failed at provider-resolve time ("Codex credential is in cooldown"), the scheduler
walked the GLOBAL ``fallback_providers`` chain, landed on a 0-eligible claude pool
and the run died with ``HTTP 503 {"error":"no eligible sub"}``. The job's declared
chain was only consulted mid-run, never at init.
"""

from unittest.mock import MagicMock, patch

from cron.scheduler import run_job

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}

_CONFIG = (
    "model:\n"
    "  default: gpt-6-sol\n"
    "  provider: openai-codex\n"
    "fallback_providers:\n"
    "  - provider: global-pool\n"
    "    model: global-model\n"
)


def _run(tmp_path, job, resolve_runtime):
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=resolve_runtime), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        success, _, _, error = run_job(job)
    return success, error, mock_agent_cls


def _job(**extra):
    job = {
        "id": "job-chain",
        "name": "job chain",
        "prompt": "hi",
        "provider": "openai-codex",
        "model": "gpt-6-sol",
        "allow_cross_provider_fallback": True,
    }
    job.update(extra)
    return job


def test_init_fallback_uses_job_declared_chain(tmp_path):
    from hermes_cli.auth import AuthError

    requested = []

    def resolve_runtime(**kwargs):
        requested.append(kwargs.get("requested"))
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("Codex credential is in cooldown.")
        return {**_RUNTIME, "provider": kwargs["requested"]}

    job = _job(fallback=[
        {"provider": "openai-codex", "model": "gpt-terra"},   # same pool, also cooling
        {"provider": "declared-pool", "model": "declared-model"},
    ])
    success, error, agent_cls = _run(tmp_path, job, resolve_runtime)

    assert success is True, error
    assert "global-pool" not in requested
    assert requested == ["openai-codex", "openai-codex", "declared-pool"]
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["provider"] == "declared-pool"
    assert kwargs["model"] == "declared-model"


def test_init_fallback_without_job_chain_keeps_global_chain(tmp_path):
    """Control: a job with no declared chain is unchanged (global chain)."""
    from hermes_cli.auth import AuthError

    requested = []

    def resolve_runtime(**kwargs):
        requested.append(kwargs.get("requested"))
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("Codex credential is in cooldown.")
        return {**_RUNTIME, "provider": kwargs["requested"]}

    success, error, agent_cls = _run(tmp_path, _job(), resolve_runtime)

    assert success is True, error
    assert requested == ["openai-codex", "global-pool"]
    assert agent_cls.call_args.kwargs["model"] == "global-model"
