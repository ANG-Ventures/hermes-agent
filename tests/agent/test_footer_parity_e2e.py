"""E2E proof: real done-site, real compressor, footer-basis value present.

Builds on the existing done-site harness so the announce is produced by the
REAL _compress_context path, then asserts the banner leads with the measured
provider number (footer parity) rather than an estimate.
"""

import sys

ROOT = "/Users/alexgierczyk/.hermes/runtime/hermes-agent"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


def test_banner_leads_with_footer_basis(tmp_path, monkeypatch, capsys):
    # Same-package import: this file lives in tests/agent/, so the done-site
    # harness is a sibling module. Do NOT add tests/ to sys.path — that
    # shadows the real `hermes_cli` package with tests/hermes_cli/.
    from tests.agent.test_compaction_announce_lcm import (
        _bulk_messages,
        _lcm_engine,
        _real_agent_with_lcm,
    )
    from plugins.context_engine.lcm.tokens import count_messages_tokens

    emitted: list = []
    engine = _lcm_engine(tmp_path)
    agent = _real_agent_with_lcm(tmp_path, emitted, engine)

    # Seed the compressor with a provider reading -- the SAME field the footer
    # renders (context_compressor.last_prompt_tokens -> last_real_prompt_tokens).
    FOOTER_VALUE = 554_000
    agent.context_compressor.last_real_prompt_tokens = FOOTER_VALUE
    agent.context_compressor.last_prompt_tokens = FOOTER_VALUE

    messages = _bulk_messages()
    approx = count_messages_tokens(messages)
    agent._compress_context(messages, "You are testing LCM.", approx_tokens=approx)

    announce = [m for m in emitted if m.startswith("🗜️ Context compacted")]
    assert announce, f"no announce emitted: {emitted}"
    line = announce[0]
    print("\n" + "=" * 70)
    print("ACTUAL BANNER FROM THE REAL DONE-SITE")
    print("=" * 70)
    print(line)
    print("=" * 70)

    assert f"{FOOTER_VALUE:,}" in line, "banner must lead with the footer's number"
    assert "before measured" in line
    assert "Counters disagree" not in line
    assert "local estimate reads" not in line
