"""Sibling readers of ``last_prompt_tokens`` must not show a pre-compaction figure.

#1500 (t_6a140e38) fixed the hygiene valve's read of a stale stored
``session_entry.last_prompt_tokens``. This file covers the other readers
(t_64728f32):

* -1 (compaction committed, no real usage yet) rendered as ``-1 / 1,000,000``
  in /status (``~-1 tokens`` from a stored -1) and as "No context data" in
  /context. Both now show the post-compaction estimate the compaction
  recorded, marked ``~``, and never fall back to the stored figure (which
  predates that compaction).
* the footer omitted the context field entirely at -1; it now shows the same
  marked estimate.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionEntry, build_session_key
from tests.gateway.test_status_command import (
    _isolate_gateway_config_from_live,  # noqa: F401  (autouse fixture)
    _make_event,
    _make_runner,
    _make_source,
)

PEAK = 900_000


# ---------------------------------------------------------------------------
# live_context_tokens: the shared resolution rule
# ---------------------------------------------------------------------------

def test_live_context_tokens_rules():
    from gateway.runtime_footer import live_context_tokens

    assert live_context_tokens(None) == (None, False, False)
    assert live_context_tokens(SimpleNamespace(last_prompt_tokens=125_644)) == (125_644, False, False)
    assert live_context_tokens(SimpleNamespace(last_prompt_tokens=0)) == (None, False, False)
    post = SimpleNamespace(last_prompt_tokens=-1, last_compression_rough_tokens=95_100)
    assert live_context_tokens(post) == (95_100, True, True)
    bare = SimpleNamespace(last_prompt_tokens=-1)
    assert live_context_tokens(bare) == (None, False, True)


# ---------------------------------------------------------------------------
# /status and /context
# ---------------------------------------------------------------------------

def _entry(stored):
    entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    entry.last_prompt_tokens = stored
    return entry


def _running(comp):
    return SimpleNamespace(
        model="openai/gpt-test",
        provider="openai",
        context_compressor=comp,
        interrupt=lambda *a, **k: None,
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_status_after_compaction_shows_post_compaction_estimate():
    runner = _make_runner(_entry(stored=PEAK))
    runner._running_agents[build_session_key(_make_source())] = _running(
        SimpleNamespace(last_prompt_tokens=-1, last_compression_rough_tokens=95_100,
                        context_length=1_000_000)
    )
    result = await runner._handle_message(_make_event("/status"))
    # upstream 04767e7aaa marks the percentage too when the figure is an estimate
    assert "**Context:** ~95,100 / 1,000,000 (~10%)" in result
    assert "900,000" not in result
    assert "-1 /" not in result


@pytest.mark.asyncio
async def test_status_after_compaction_without_estimate_never_falls_back_to_stored():
    runner = _make_runner(_entry(stored=PEAK))
    runner._running_agents[build_session_key(_make_source())] = _running(
        SimpleNamespace(last_prompt_tokens=-1, context_length=1_000_000)
    )
    result = await runner._handle_message(_make_event("/status"))
    assert "900,000" not in result
    assert "-1 /" not in result


@pytest.mark.asyncio
async def test_status_stored_negative_sentinel_is_not_rendered():
    runner = _make_runner(_entry(stored=-1))
    result = await runner._handle_message(_make_event("/status"))
    assert "-1" not in result.split("**Context:**")[-1].split("\n")[0]


@pytest.mark.asyncio
async def test_context_after_compaction_shows_post_compaction_estimate():
    runner = _make_runner(_entry(stored=PEAK))
    runner._running_agents[build_session_key(_make_source())] = _running(
        SimpleNamespace(last_prompt_tokens=-1, last_compression_rough_tokens=95_100,
                        context_length=1_000_000, threshold_tokens=750_000,
                        threshold_percent=0.75, compression_count=1)
    )
    result = await runner._handle_context_command(_make_event("/context"))
    assert "In use: ~95,100 / 1,000,000" in result
    assert "900,000 /" not in result


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def test_footer_context_tokens_prefers_display_figure():
    import gateway.run as gw_run

    assert gw_run._footer_context_tokens(
        {"last_prompt_tokens": -1, "context_tokens_display": 95_100}
    ) == 95_100
    assert gw_run._footer_context_tokens(
        {"last_prompt_tokens": -1, "context_tokens_display": -1}
    ) == -1
    assert gw_run._footer_context_tokens({"last_prompt_tokens": 125_644}) == 125_644


def test_footer_renders_post_compaction_estimate_marked():
    from gateway.runtime_footer import format_runtime_footer

    line = format_runtime_footer(
        model="m", context_tokens=95_100, context_length=1_000_000,
        fields=["context_full"], context_estimated=True,
    )
    assert line == "~95.1k/1M (10%)"
    # Real figure: unchanged format.
    assert format_runtime_footer(
        model="m", context_tokens=125_644, context_length=1_000_000, fields=["context_full"],
    ) == "125.6k/1M (13%)"
    # No figure after a compaction: field omitted, never the old peak.
    assert format_runtime_footer(
        model="m", context_tokens=-1, context_length=1_000_000, fields=["context_full"],
    ) == ""


def test_turn_result_carries_footer_display_figure():  # noqa: source-proxy structural: both run_sync result paths share one usage dict
    """Both run_sync result dicts carry the resolved footer figure (source contract,
    same pattern as test_footer_provider_in_turn_result)."""
    import inspect

    import gateway.run as gw_run

    # Parity 2026-10-01: upstream's run_sync builds ONE ``usage`` dict both result paths share
    # through ``common`` (same shape as test_footer_provider_in_turn_result).
    src = inspect.getsource(gw_run.TurnRunner.run_sync)
    assert '"context_tokens_display": ctx_display_toks' in src
    assert '"context_tokens_estimated": ctx_display_estimated' in src
    assert "**common" in src, "both result paths must share the usage dict"
