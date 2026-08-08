"""E2E: reproduce Ace's 2026-08-07 below-threshold compaction and prove the banner.

This drives the REAL LCM engine through the REAL preflight decision (no mocked
gate), then the REAL host resolver, and asserts a user-visible banner naming the
cause. Deliberately exceeds the 12,000-char externalization threshold: synthetic
fixtures under it never engage externalization, which is why four earlier
clean-room repros all falsely reported "no divergence".
"""
import tempfile

import pytest

from agent.context_engine import (
    ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
    automatic_compaction_status_message,
)
from agent.conversation_compression import (
    ENGINE_PREFLIGHT_MAINTENANCE_REASON_STATUS_TEMPLATE,
)

lcm_engine = pytest.importorskip("plugins.context_engine.lcm.engine")
lcm_config = pytest.importorskip("plugins.context_engine.lcm.config")
lcm_tokens = pytest.importorskip("plugins.context_engine.lcm.tokens")


def _conversation(n_pairs, payload_chars):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append({"role": "assistant", "content": f"answer {i}"})
    if payload_chars:
        msgs.append({"role": "user", "content": "here is a big thing"})
        msgs.append({"role": "assistant", "content": "X" * payload_chars})
    return msgs


def _engine(tmp_path, session):
    eng = lcm_engine.LCMEngine(
        config=lcm_config.LCMConfig(), hermes_home=str(tmp_path)
    )
    eng._session_id = session
    eng._set_context_length(1_000_000, source="test")
    return eng


def test_large_payload_forces_below_threshold_compaction_and_explains_itself(tmp_path):
    eng = _engine(tmp_path, "e2e-big")
    msgs = _conversation(120, 200_000)

    rough = lcm_tokens.count_messages_tokens(msgs)
    assert rough < eng.threshold_tokens, (
        "premise broken: this fixture must sit BELOW the token threshold, "
        f"got {rough:,} vs {eng.threshold_tokens:,}"
    )

    assert eng.should_compress_preflight(msgs) is True, (
        "expected the engine to request a below-threshold compaction"
    )
    reason = eng.last_preflight_reason
    assert reason, "engine requested a compaction without recording why"

    banner = automatic_compaction_status_message(
        eng,
        phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
        default_message=ENGINE_PREFLIGHT_MAINTENANCE_REASON_STATUS_TEMPLATE.format(
            engine=eng.name, tokens=rough, threshold=eng.threshold_tokens, reason=reason
        ),
    )
    assert banner is not None, "below-threshold compaction was silent"
    assert reason in banner
    assert "BELOW the" in banner
    assert "not token pressure" in banner


def test_small_payload_does_not_trigger_a_below_threshold_compaction(tmp_path):
    """Negative control. Without it, a gate that ALWAYS fires would pass above."""
    eng = _engine(tmp_path, "e2e-small")
    msgs = _conversation(120, 1_000)
    assert lcm_tokens.count_messages_tokens(msgs) < eng.threshold_tokens
    assert eng.should_compress_preflight(msgs) is False
    assert eng.last_preflight_reason == ""


def test_no_payload_control(tmp_path):
    eng = _engine(tmp_path, "e2e-none")
    msgs = _conversation(120, 0)
    assert eng.should_compress_preflight(msgs) is False
