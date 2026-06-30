"""Tests for gateway /usage command — agent cache lookup and output fields."""

import threading
from unittest.mock import MagicMock, patch

import pytest


def _make_mock_agent(**overrides):
    """Create a mock AIAgent with realistic session counters."""
    agent = MagicMock()
    defaults = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "base_url": None,
        "session_total_tokens": 50_000,
        "session_api_calls": 5,
        "session_prompt_tokens": 40_000,
        "session_completion_tokens": 10_000,
        "session_input_tokens": 35_000,
        "session_output_tokens": 10_000,
        "session_cache_read_tokens": 5_000,
        "session_cache_write_tokens": 2_000,
        "session_reasoning_tokens": 0,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(agent, k, v)

    # Rate limit state
    rl = MagicMock()
    rl.has_data = True
    agent.get_rate_limit_state.return_value = rl

    # Context compressor
    ctx = MagicMock()
    ctx.last_prompt_tokens = 30_000
    ctx.context_length = 200_000
    ctx.compression_count = 1
    agent.context_compressor = ctx

    return agent


def _make_runner(session_key, agent=None, cached_agent=None):
    """Build a bare GatewayRunner with just the fields _handle_usage_command needs."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.session_store = MagicMock()

    if agent is not None:
        runner._running_agents[session_key] = agent

    if cached_agent is not None:
        runner._agent_cache[session_key] = (cached_agent, "sig")

    # Wire helper
    runner._session_key_for_source = MagicMock(return_value=session_key)

    return runner


SK = "agent:main:telegram:private:12345"


class TestUsageCachedAgent:
    """The main fix: /usage should find agents in _agent_cache between turns."""

    @pytest.mark.asyncio
    async def test_cached_agent_shows_detailed_usage(self):
        agent = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"):
            result = await runner._handle_usage_command(event)

        assert "claude-sonnet-4.6" in result
        # The last-turn card (resident fallback from session counters, since the
        # mock channel has no blackbox row) shows the new /context vocabulary.
        assert "35,000" in result   # uncached (session_input_tokens)
        assert "10,000" in result   # output billed (session_output_tokens)
        assert "50,000" in result   # session total (header label_total)
        assert "uncached" in result
        assert "Total (billed in+out)" in result
        # Cost and cache-hit reporting is removed everywhere (the rich-card "Turn
        # Cost" only renders from a real blackbox row, never the thin fallback).
        assert "$" not in result
        assert "Cache read" not in result
        assert "Cache write" not in result
        assert "Cost" not in result

    @pytest.mark.asyncio
    async def test_running_agent_preferred_over_cache(self):
        """When agent is in both dicts, the running one wins."""
        running = _make_mock_agent(session_api_calls=10, session_total_tokens=80_000)
        cached = _make_mock_agent(session_api_calls=5, session_total_tokens=50_000)
        runner = _make_runner(SK, agent=running, cached_agent=cached)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "80,000" in result   # running agent's total
        assert "API calls: 10" in result

    @pytest.mark.asyncio
    async def test_sentinel_skipped_uses_cache(self):
        """PENDING sentinel in _running_agents should fall through to cache."""
        from gateway.run import _AGENT_PENDING_SENTINEL

        cached = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=cached)
        runner._running_agents[SK] = _AGENT_PENDING_SENTINEL
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "claude-sonnet-4.6" in result
        assert "Session Token Usage" in result

    @pytest.mark.asyncio
    async def test_no_agent_anywhere_falls_to_history(self):
        """No running or cached agent → rough estimate from transcript."""
        runner = _make_runner(SK)
        event = MagicMock()

        session_entry = MagicMock()
        session_entry.session_id = "sess123"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with patch("agent.model_metadata.estimate_messages_tokens_rough", return_value=500):
            result = await runner._handle_usage_command(event)

        assert "Session Info" in result
        assert "Messages: 2" in result
        assert "~500" in result

    @pytest.mark.asyncio
    async def test_cache_read_write_hidden_when_zero(self):
        """Cache token lines should be omitted when zero."""
        agent = _make_mock_agent(session_cache_read_tokens=0, session_cache_write_tokens=0)
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "Cache read" not in result
        assert "Cache write" not in result


class TestUsageAccountSection:
    """Account-limits section appended to /usage output (PR #2486)."""

    @pytest.mark.asyncio
    async def test_usage_command_includes_account_section(self, monkeypatch):
        agent = _make_mock_agent(provider="openai-codex")
        agent.base_url = "https://chatgpt.com/backend-api/codex"
        agent.api_key = "unused"
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        monkeypatch.setattr(
            "gateway.slash_commands.fetch_account_usage",
            lambda provider, base_url=None, api_key=None: object(),
        )
        monkeypatch.setattr(
            "gateway.slash_commands.render_account_usage_lines",
            lambda snapshot, markdown=False: [
                "📈 **Account limits**",
                "Provider: openai-codex (Pro)",
                "Session: 85% remaining (15% used)",
            ],
        )
        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="included")
            result = await runner._handle_usage_command(event)

        assert "📊 **Session Token Usage**" in result
        assert "📈 **Account limits**" in result
        assert "Provider: openai-codex (Pro)" in result

    @pytest.mark.asyncio
    async def test_usage_command_uses_persisted_provider_when_agent_not_running(self, monkeypatch):
        runner = _make_runner(SK)
        runner._session_db = MagicMock()
        runner._session_db.get_last_turn_usage.return_value = None
        runner._session_db.get_session.return_value = {
            "billing_provider": "openai-codex",
            "billing_base_url": "https://chatgpt.com/backend-api/codex",
        }
        session_entry = MagicMock()
        session_entry.session_id = "sess-1"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "earlier"},
        ]

        calls = []

        async def _fake_to_thread(fn, *args, **kwargs):
            # /usage dispatches BOTH the account fetch (fetch_account_usage, called
            # with the provider positionally) and the Nous credits fetch
            # (nous_credits_lines, markdown-only) through to_thread — record every
            # call rather than last-wins so we can pick out the account fetch.
            calls.append({"args": args, "kwargs": kwargs})
            return fn(*args, **kwargs)

        monkeypatch.setattr("gateway.run.asyncio.to_thread", _fake_to_thread)
        monkeypatch.setattr(
            "gateway.slash_commands.fetch_account_usage",
            lambda provider, base_url=None, api_key=None: object(),
        )
        monkeypatch.setattr(
            "gateway.slash_commands.render_account_usage_lines",
            lambda snapshot, markdown=False: [
                "📈 **Account limits**",
                "Provider: openai-codex (Pro)",
            ],
        )
        # The credits block routes through the shared nous_credits_lines() helper;
        # stub it so this account-section test stays hermetic (no portal/auth lookup).
        monkeypatch.setattr("agent.account_usage.nous_credits_lines", lambda markdown=False: [])

        event = MagicMock()
        result = await runner._handle_usage_command(event)

        account_call = next(c for c in calls if c["args"] == ("openai-codex",))
        assert account_call["kwargs"]["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert "📊 **Session Info**" in result
        assert "📈 **Account limits**" in result


class TestUsageLastTurnSnapshot:
    """Between-turn /usage reads the persisted last-turn snapshot (eviction-safe).

    When the agent has been evicted (no running/cached agent) but the sessions
    row holds a last_turn_* snapshot, /usage should surface real last-turn token
    numbers instead of falling through to only a rough history estimate.
    """

    @pytest.mark.asyncio
    async def test_last_turn_snapshot_shown_when_no_agent(self):
        runner = _make_runner(SK)
        runner._session_db = MagicMock()
        runner._session_db.get_session.return_value = {
            "billing_provider": None,
            "model": "anthropic/claude-opus-4-8",
            "input_tokens": 350_000,
            "output_tokens": 40_000,
            "total_tokens": 390_000,
            "api_call_count": 7,
        }
        runner._session_db.get_last_turn_usage.return_value = {
            "input_tokens": 120_000,
            "output_tokens": 8_000,
            "cache_read_tokens": 110_000,
            "cache_write_tokens": 2_000,
            "reasoning_tokens": 1_500,
        }
        session_entry = MagicMock()
        session_entry.session_id = "sess-evicted"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ]

        event = MagicMock()
        with patch("agent.model_metadata.estimate_messages_tokens_rough", return_value=500):
            result = await runner._handle_usage_command(event)

        # Real last-turn numbers must appear in the new /context card vocabulary
        # (not just the rough ~500 estimate). Output + reasoning fold into one
        # billed-out total (8,000 + 1,500 = 9,500); in billed = 120k+110k+2k = 232k.
        assert "120,000" in result   # last-turn uncached input
        assert "110,000" in result   # last-turn cache read
        assert "9,500" in result     # output billed (8,000 + 1,500 reasoning, folded)
        assert "232,000" in result   # tokens-in billed
        assert "Total (billed in+out): 241,500" in result
        assert "uncached" in result
        assert "Last turn" in result or "last turn" in result.lower()
        # Honest fallback label: the agent is genuinely not resident here.
        assert "persisted; agent not resident" in result

    @pytest.mark.asyncio
    async def test_no_snapshot_still_falls_to_history(self):
        """When there is no persisted snapshot, behavior is unchanged."""
        runner = _make_runner(SK)
        runner._session_db = MagicMock()
        runner._session_db.get_session.return_value = {"billing_provider": None}
        runner._session_db.get_last_turn_usage.return_value = None
        session_entry = MagicMock()
        session_entry.session_id = "sess-nosnap"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hello"},
        ]

        event = MagicMock()
        with patch("agent.model_metadata.estimate_messages_tokens_rough", return_value=500):
            result = await runner._handle_usage_command(event)

        assert "Session Info" in result
        assert "~500" in result

