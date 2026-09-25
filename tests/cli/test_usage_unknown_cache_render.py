"""The RENDERED cumulative cache segment never fabricates a ratio (t_25f50547).

Sibling of ``tests/agent/test_usage_unknown_persisted_export.py``, which pins
the *producer* (``_get_status_bar_snapshot`` sets ``cache_hit_pct = None`` +
``cache_hit_label = "unknown"``). That is only half the path: ``None`` is
precisely the sentinel that arms ``_cache_hit_rate``'s session-lifetime
fallback, which divides the same raw unflagged counters and prints the ratio
the suppression exists to prevent.

These tests drive the SHIPPED render seam — ``_cache_hit_rate`` itself, and
``_build_status_bar_text`` at both widths — so a guard applied only to the
delta branch fails here.
"""
import re
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS,
    normalize_usage,
    prompt_tokens_unknown,
)

# The literal claude-bpx bridge wire shapes (same five the card requires).
WIRES = {
    "input-only": {
        "prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
        "prompt_tokens_unavailable": True, "unavailable": True,
    },
    "cache-only": {
        "prompt_tokens": 150, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": 0},
    },
    "wholly-unavailable": {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "unavailable": True,
    },
    "output-only": {
        "prompt_tokens": 150, "completion_tokens": None, "total_tokens": None,
        "output_tokens_unavailable": True, "unavailable": True,
    },
    "measured-zero": {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
    },
}
WIRE_IDS = sorted(WIRES)

# A cache segment that reads as a clean 100% hit if the raw counters are
# trusted — the fabricated figure IS the failure mode being tested.
PROMPT_TOTAL = 100
CACHE_READ = 100

_RATIO = re.compile(r"◎\s*[\d.]+\s*%")


def _usage(name):
    return normalize_usage(WIRES[name], api_mode="chat_completions")


def _cli_with_session(*, unknown_flags):
    """A real ``HermesCLI`` wired to an agent carrying ``unknown_flags``."""
    import datetime as _dt

    import cli as cli_mod

    agent = SimpleNamespace(
        model="claude-sonnet-4-5",
        provider="anthropic",
        base_url="",
        session_input_tokens=PROMPT_TOTAL,
        session_output_tokens=50,
        session_cache_read_tokens=CACHE_READ,
        session_cache_write_tokens=0,
        session_prompt_tokens=PROMPT_TOTAL,
        session_completion_tokens=50,
        session_total_tokens=PROMPT_TOTAL + 50,
        session_api_calls=2,
        get_rate_limit_state=lambda: None,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=1000, context_length=200_000, compression_count=0
        ),
    )
    for key in USAGE_UNKNOWN_FIELDS:
        setattr(agent, f"session_{key}", bool(unknown_flags.get(key)))

    shell = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    shell.model = agent.model
    shell.session_start = _dt.datetime.now()
    shell.conversation_history = []
    shell.agent = agent
    return shell


@pytest.mark.parametrize("name", WIRE_IDS)
def test_rendered_cache_segment_never_fabricates_a_ratio(name):
    usage = _usage(name)
    flags = {key: bool(getattr(usage, key)) for key in USAGE_UNKNOWN_FIELDS}
    shell = _cli_with_session(unknown_flags=flags)

    snapshot = shell._get_status_bar_snapshot()
    rendered = shell._cache_hit_rate(snapshot)

    ratio_unknown = prompt_tokens_unknown(usage) or usage.cache_read_tokens_unknown
    if not ratio_unknown:
        # Fully measured for BOTH ratio terms — the real percentage still ships.
        assert rendered is not None
        pct, label = rendered
        assert pct is not None and "unknown" not in label
        assert _RATIO.search(label), label
        return

    assert rendered is not None, "the segment must say unknown, not vanish"
    pct, label = rendered
    assert pct is None, "no percentage may be produced over an unmeasured term"
    assert "unknown" in label and "%" not in label, label
    # The style helper is handed exactly what the renderer produces.
    assert shell._cache_hit_rate_style(pct) == "class:status-bar-dim"

    for width in (60, 120):
        text = shell._build_status_bar_text(width=width)
        assert "◎ unknown" in text, (width, text)
        assert not _RATIO.search(text), (width, text)


def test_measured_session_still_renders_the_real_percentage():
    """Guard scope: providers that never emit unknowns are unchanged."""
    shell = _cli_with_session(unknown_flags={})
    snapshot = shell._get_status_bar_snapshot()
    pct, label = shell._cache_hit_rate(snapshot)
    assert pct == pytest.approx(100.0)
    assert label == "◎ 100.0%"
    assert "◎ 100%" in shell._build_status_bar_text(width=60)


def test_lifetime_fallback_is_gated_not_just_the_delta():
    """The fallback branch is the one the producer's ``None`` hands off to.
    A snapshot with NO delta pct (exactly what the producer writes when it
    suppresses) plus a latched unknown must still refuse the ratio, even
    though the raw counters divide cleanly.
    """
    shell = _cli_with_session(unknown_flags={"input_tokens_unknown": True})
    snapshot = {
        "cache_hit_pct": None,
        "cache_hit_label": "unknown",
        "session_prompt_tokens": PROMPT_TOTAL,
        "session_cache_read_tokens": CACHE_READ,
        "session_prompt_tokens_unknown": True,
    }
    pct, label = shell._cache_hit_rate(snapshot)
    assert pct is None
    assert not _RATIO.search(label), label


def test_render_seam_does_not_inherit_a_stale_producer_label():
    """The render derives its own label instead of echoing the snapshot's.

    If the producer's guard ever regresses, ``cache_hit_label`` holds a
    percentage. The render branch that exists to suppress the ratio must not
    print that value back out.
    """
    shell = _cli_with_session(unknown_flags={"input_tokens_unknown": True})
    snapshot = {
        "cache_hit_pct": 100.0,          # producer guard regressed ...
        "cache_hit_label": "100%",       # ... and left a fabricated label
        "session_prompt_tokens": PROMPT_TOTAL,
        "session_cache_read_tokens": CACHE_READ,
        "session_prompt_tokens_unknown": True,
    }
    pct, label = shell._cache_hit_rate(snapshot)
    assert pct is None
    assert label == "◎ unknown", label


@pytest.mark.parametrize("name", WIRE_IDS)
def test_producer_and_render_seam_agree(name):
    """The producer's inline condition and ``_cache_ratio_unknown`` are twins.

    The producer block cannot call the helper (a committed test source-lifts
    it by AST anchor and execs it without ``self``), so the two conditions are
    written out separately. This pins them to the same verdict on every wire
    shape, which is what stops them drifting apart.
    """
    usage = _usage(name)
    flags = {key: bool(getattr(usage, key)) for key in USAGE_UNKNOWN_FIELDS}
    shell = _cli_with_session(unknown_flags=flags)
    snapshot = shell._get_status_bar_snapshot()

    producer_suppressed = snapshot["cache_hit_pct"] is None and (
        snapshot["cache_hit_label"] == "unknown"
    )
    assert shell._cache_ratio_unknown(snapshot) is producer_suppressed
