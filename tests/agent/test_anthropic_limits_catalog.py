"""The Anthropic limits catalog (``<fleet root>/config/anthropic-limits.json``) drives the agent.

Spec: hermes-home plans/2026-10-30_anthropic-limits-catalog-SPEC.md 2.2 (card t_f08ad655). Three consumers
read the catalog; the in-code literals are the fallback when it is missing or unreadable:

* the anthropic provider plugin's request-body cap (catalog ``request_body_bytes - agent_headroom_bytes``),
  re-read on catalog mtime change, no restart;
* ``agent.model_metadata.DEFAULT_CONTEXT_LENGTHS`` Anthropic rows (catalog ``models[*].context_window``);
* ``agent.anthropic_adapter`` THINKING_BUDGET (catalog ``thinking_budget``).

Every test points the fleet-root resolver at a temp root via HERMES_HOME (a path outside the native
``~/.hermes`` resolves to itself); the real catalog is never touched.
"""

import json
import logging
import os

import pytest

PLUGIN_FALLBACK = 128_000_000 - 4 * 1024 * 1024
FALLBACK_THINKING = {"xhigh": 32000, "high": 16000, "medium": 8000, "low": 4000}
LEGACY_THINKING_MODEL = "claude-3-7-sonnet-20250219"  # budget_tokens path, not adaptive


def _catalog(**over):
    cat = {
        "version": 1,
        "request_body_bytes": {"value": 64_000_000},
        "agent_headroom_bytes": {"value": 1_000_000},
        "models": {
            "claude-opus-5-5": {"context_window": 777_777},
            "claude-opus-9": {"context_window": 2_000_000, "aliases": ["claude-opus-9.0"]},
            "claude-haiku-4-5": {"context_window": 200_000, "agent_row": False},
            "claude": {"family": True, "context_window": 200_000},
        },
        "thinking_budget": {"xhigh": 40000, "high": 12345, "medium": 6000, "low": 3000},
        "observed": {},
    }
    cat.update(over)
    return cat


def _reset_limits_state():
    try:
        from agent import anthropic_limits
    except ImportError:  # pre-consumer tree: nothing to reset
        return
    anthropic_limits._reset_for_tests()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "fleet-root"
    (r / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(r))
    _reset_limits_state()
    yield r
    # Leave the shared module tables on their fallback rows for the next test.
    (r / "config" / "anthropic-limits.json").unlink(missing_ok=True)
    _reset_limits_state()
    from agent import model_metadata

    model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-5-5")


def _write(root, cat, *, bump=0):
    p = root / "config" / "anthropic-limits.json"
    p.write_text(json.dumps(cat))
    if bump:  # force a distinct mtime_ns even on coarse-mtime filesystems
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + bump))
    return p


def _cap(model="claude-opus-5-5"):
    from providers import get_provider_profile

    return get_provider_profile("anthropic").get_max_request_body_bytes(model)


def test_plugin_body_cap_reads_catalog_and_follows_edit_without_restart(root):
    from agent.request_body_budget import request_body_limit_for_provider

    _write(root, _catalog())
    assert _cap() == 63_000_000
    assert request_body_limit_for_provider("anthropic", "claude-opus-5", api_mode="anthropic_messages") == 63_000_000
    # A relay with no profile of its own inherits the native protocol cap, which is now the catalog's.
    assert request_body_limit_for_provider("apx-0", "claude-opus-5", api_mode="anthropic_messages") == 63_000_000

    _write(root, _catalog(request_body_bytes={"value": 32_000_000}), bump=1_000_000)
    assert _cap() == 31_000_000


def test_model_metadata_anthropic_rows_overlay_from_catalog(root):
    from agent import context_pin, model_metadata

    _write(root, _catalog())
    assert model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-5-5") == (
        "claude-opus-5-5", 777_777)
    # A model the literals do not know rides the catalog row (and its alias).
    assert model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-9")[1] == 2_000_000
    assert model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-9.0")[1] == 2_000_000
    assert context_pin.advertised_context_length("claude-opus-5-5") == 777_777
    # Rows the catalog does not name keep their literal (catalog overlays, never deletes).
    assert model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-sonnet-5-5")[1] == 1_000_000
    # agent_row: false stays out of the table.
    assert "claude-haiku-4-5" not in model_metadata.DEFAULT_CONTEXT_LENGTHS

    # Catalog removed -> the literal rows come back, the catalog-only row goes.
    (root / "config" / "anthropic-limits.json").unlink()
    assert model_metadata._longest_key_match(model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-5-5")[1] == 1_000_000
    assert "claude-opus-9" not in model_metadata.DEFAULT_CONTEXT_LENGTHS


def test_thinking_budget_reads_catalog(root):
    from agent.anthropic_adapter import _thinking_kwargs

    _write(root, _catalog())
    kw = _thinking_kwargs({"effort": "high"}, LEGACY_THINKING_MODEL, 4096)
    assert kw["thinking"]["budget_tokens"] == 12345
    kw = _thinking_kwargs({"effort": "xhigh"}, LEGACY_THINKING_MODEL, 4096)
    assert kw["thinking"]["budget_tokens"] == 40000


def test_missing_catalog_falls_back_to_constants_with_one_warning(root, caplog):
    from agent import anthropic_limits, model_metadata
    from agent.anthropic_adapter import THINKING_BUDGET, _thinking_kwargs

    assert dict(THINKING_BUDGET) == FALLBACK_THINKING
    with caplog.at_level(logging.WARNING, logger=anthropic_limits.logger.name):
        for _ in range(3):
            assert _cap() == PLUGIN_FALLBACK
            assert model_metadata._longest_key_match(
                model_metadata.DEFAULT_CONTEXT_LENGTHS, "claude-opus-5-5")[1] == 1_000_000
            assert _thinking_kwargs({"effort": "high"}, LEGACY_THINKING_MODEL, 4096)["thinking"][
                "budget_tokens"] == 16000
    warnings = [r for r in caplog.records if r.name == anthropic_limits.logger.name and r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "anthropic-limits.json" in warnings[0].getMessage()


def test_unreadable_catalog_falls_back_with_one_warning(root, caplog):
    from agent import anthropic_limits

    _write(root, {"version": 1, "request_body_bytes": "not-an-object"})
    with caplog.at_level(logging.WARNING, logger=anthropic_limits.logger.name):
        assert _cap() == PLUGIN_FALLBACK
        assert _cap() == PLUGIN_FALLBACK
    p = root / "config" / "anthropic-limits.json"
    st = p.stat()
    p.write_text("{ torn json")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    with caplog.at_level(logging.WARNING, logger=anthropic_limits.logger.name):
        assert _cap() == PLUGIN_FALLBACK
    warnings = [r for r in caplog.records if r.name == anthropic_limits.logger.name and r.levelno == logging.WARNING]
    # One per distinct bad state (the second read of the same bad file is silent).
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
