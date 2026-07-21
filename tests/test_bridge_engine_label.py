"""Bridge-provider engine relabel (lcm → cc) — behavior contracts.

On bridge providers (claude-bpx-N / claude-bpr / legacy claude-bridge*) the
Claude Code CLI's resident session is the operative context manager, so the
compaction DISPLAY label is "cc". LCM still ingests there (raw lcm.db stays
live), so the relabel is display-only and must never change the compaction
numbers or the recovery affordance.

These are invariant tests, not snapshots: they assert the provider→label
RELATION, not a frozen string for a specific provider list.
"""

import pytest

from agent.provider_model_util import (
    is_bridge_provider,
    engine_display_label,
)
from agent.fork_ext.compaction_ext import _format_compaction_announce


# --- the pure predicate -------------------------------------------------

@pytest.mark.parametrize(
    "provider",
    [
        "claude-bpr",
        "claude-bpx-15",
        "claude-bpx-16",
        "claude-bpx-1",
        "claude-bridge",
        "claude-bridge-f3",
        "claude-bpp",
    ],
)
def test_bridge_providers_detected(provider):
    assert is_bridge_provider(provider) is True


@pytest.mark.parametrize(
    "provider",
    [
        "claude-apr",          # native pool — LCM works normally
        "claude-apx-15",       # native proxy lane
        "claude-app",
        "openai-codex",
        "yunwu",
        "gemini-bridge",       # NOT a claude bridge (poly-vendor gemini)
        "",
        None,
    ],
)
def test_non_bridge_providers_not_detected(provider):
    assert is_bridge_provider(provider) is False


def test_bridge_detected_through_prefixed_model():
    # split_provider_model pulls the provider out of a "provider/model" string.
    assert is_bridge_provider("claude-bpx-15/claude-fable-5") is True
    assert is_bridge_provider("claude-apx-15/claude-fable-5") is False


# --- the display-label mapping -----------------------------------------

def test_label_is_cc_on_bridge_only_when_engine_is_lcm():
    assert engine_display_label("lcm", "claude-bpx-15") == "cc"
    assert engine_display_label("lcm", "claude-bpr") == "cc"


def test_label_stays_lcm_on_native():
    assert engine_display_label("lcm", "claude-apr") == "lcm"
    assert engine_display_label("lcm", "claude-apx-15") == "lcm"
    assert engine_display_label("lcm", None) == "lcm"


def test_non_lcm_engine_never_relabeled():
    # A bridge provider on the built-in compressor is not "cc" — the relabel is
    # specifically the LCM→cc display swap.
    assert engine_display_label("compressor", "claude-bpx-15") == "compressor"
    assert engine_display_label(None, "claude-bpx-15") is None


# --- the builder integration -------------------------------------------

def _announce(provider):
    return _format_compaction_announce(
        engine_name="lcm",
        status="compacted",
        old_session_id="sess-old",
        new_session_id="sess-new",
        old_messages=100,
        new_messages=20,
        pre_tokens=984_000,
        post_tokens=86_000,
        model="claude-fable-5",
        provider=provider,
        raw_store_count=5000,
    )


def test_builder_shows_cc_on_bridge():
    line = _announce("claude-bpx-15")
    assert line is not None
    assert "engine: cc" in line
    assert "engine: lcm" not in line  # noqa: E501


def test_builder_shows_lcm_on_native():
    line = _announce("claude-apr")
    assert line is not None
    assert "engine: lcm" in line
    assert "engine: cc" not in line  # noqa: E501


def test_relabel_does_not_change_the_numbers():
    # The wire/overflow numbers are the SAME regardless of the engine label —
    # only the label token differs (Ace: keep the real number as the gauge).
    bridge = _announce("claude-bpx-15")
    native = _announce("claude-apr")
    assert bridge is not None and native is not None
    for tok in ("984", "86"):  # abbreviated pre/post token magnitudes
        assert tok in bridge
        assert tok in native


def test_recovery_hint_preserved_on_bridge():
    # LCM still ingests on bridge providers, so lcm_grep/lcm_expand recovery is
    # still real — the pointer must survive the relabel.
    line = _announce("claude-bpx-15")
    assert line is not None
    assert "lcm_grep" in line or "lcm.db" in line
