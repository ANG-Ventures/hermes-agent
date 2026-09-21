"""The quota gate is WIRED into the live fallback walker, not just a library.

Regression for the 2026-09-21 cascade: ten "Rate limited — switching to
fallback provider..." lines and ~60 s of round-trips spent discovering what
``var/usage-portal/site/usage.json`` already recorded.

These tests drive the REAL ``try_activate_fallback`` / ``apply_quota_gate``
seam — a stubbed registry in, an observed chain walk out.
"""

from __future__ import annotations

import time
import types

import pytest

import agent.auxiliary_client as ac
from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason
from agent.quota_registry_gate import apply_quota_gate, rate_limited_status_line


@pytest.fixture(autouse=True)
def _isolate_runtime_globals():
    ac.clear_runtime_main()
    try:
        yield
    finally:
        ac.clear_runtime_main()


def _exhausted(resets_in=3 * 86400):
    return {
        "observed_at": time.time(),
        "windows": [{
            "key": "seven_day", "pct": 100.0, "status": "rejected",
            "resets_at": time.time() + resets_in,
        }],
    }


def _healthy():
    return {
        "observed_at": time.time(),
        "windows": [{
            "key": "seven_day", "pct": 17.0, "status": "allowed",
            "resets_at": time.time() + 5 * 86400,
        }],
    }


def _agent(chain, snapshot):
    a = types.SimpleNamespace()
    a.model = "claude-opus-5"
    a.provider = "claude-bpx-17"
    a.base_url = "http://127.0.0.1:3556"
    a.api_mode = "anthropic_messages"
    a.api_key = "primary-key"
    a.reasoning_config = None
    a.reasoning_effort = "high"
    a._config_context_length = 1_000_000
    a._fallback_activated = False
    a._transport_cache = {}
    a._credential_pool = None
    a._primary_runtime = None
    a._fallback_index = 0
    a._rate_limited_until = 0.0
    a._fallback_chain = list(chain)
    a.fallback_model = list(chain)
    a._quota_registry_snapshot = snapshot
    a.context_compressor = None
    a._snapshot_primary_runtime = lambda: None
    a._restore_primary_runtime = lambda: None
    a._anthropic_prompt_cache_policy = lambda **k: (False, False)
    a._ensure_lmstudio_runtime_loaded = lambda: None
    a._is_azure_openai_url = lambda u: False
    a._is_direct_openai_url = lambda u: False
    a._provider_model_requires_responses_api = lambda *x, **k: True
    a._replace_primary_openai_client = lambda **k: None
    a._last_fallback_announced = None
    a.log_prefix = ""
    a._vprint = lambda *x, **k: None
    a.status_messages = []
    a._buffer_status = lambda m: a.status_messages.append(m)
    a._announced = []
    a.status_callback = lambda kind, msg: a._announced.append((kind, msg))
    a._emit_status = lambda msg: a._announced.append(("lifecycle", msg))
    a._try_activate_fallback = (
        lambda *x, **k: try_activate_fallback(a, *x, **k)
    )
    return a


def _entry(provider):
    return {"provider": provider, "model": "claude-opus-5"}


def _patch_resolver(monkeypatch):
    """Every resolved provider produces a distinct usable client."""
    seen = []

    def _resolve(provider, model, **kw):
        seen.append(provider)
        return types.SimpleNamespace(
            api_key="k",
            base_url=f"https://{provider}.example/anthropic",
            _custom_headers=None,
            default_headers=None,
        ), model

    monkeypatch.setattr(ac, "resolve_provider_client", _resolve, raising=False)
    import agent.chat_completion_helpers as cch
    monkeypatch.setattr(
        cch, "get_model_context_length", lambda *a, **k: 200_000, raising=False
    )
    return seen


# ── the headline: the walk stops trying known-dead subs ─────────────────


def test_exhausted_subs_are_never_resolved(monkeypatch):
    """The 2026-09-21 shape: 3 dead subs in front of one live one."""
    seen = _patch_resolver(monkeypatch)
    snapshot = {
        "claude-apx-1": _exhausted(),
        "claude-apx-2": _exhausted(),
        "claude-apx-15": _exhausted(),
        "claude-apx-17": _healthy(),
    }
    agent = _agent(
        [_entry("claude-apx-1"), _entry("claude-apx-2"),
         _entry("claude-apx-15"), _entry("claude-apx-17")],
        snapshot,
    )
    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is True
    # The dead three were never even constructed — that is the ~60 s saved.
    assert seen == ["claude-apx-17"]
    assert agent.provider == "claude-apx-17"


def test_the_switching_spam_collapses_to_one_line(monkeypatch):
    """×10 'switching…' lines must become ONE line naming the skip count.

    Drives the loop's quota-failover status site (``rate_limited_status_line``)
    followed by the walk, exactly as ``agent.conversation_loop`` does.
    """
    _patch_resolver(monkeypatch)
    snapshot = {f"claude-apx-{i}": _exhausted() for i in range(1, 10)}
    snapshot["claude-apx-17"] = _healthy()
    chain = [_entry(f"claude-apx-{i}") for i in range(1, 10)]
    chain.append(_entry("claude-apx-17"))
    agent = _agent(chain, snapshot)

    agent._buffer_status(rate_limited_status_line(agent))
    try_activate_fallback(agent, reason=FailoverReason.rate_limit)

    switching = [m for m in agent.status_messages if "switching" in m.lower()]
    assert len(switching) == 1, switching
    assert "skipping 9 exhausted subs" in switching[0]


def test_healthy_chain_emits_the_plain_line(monkeypatch):
    _patch_resolver(monkeypatch)
    agent = _agent([_entry("claude-apx-17")], {"claude-apx-17": _healthy()})
    line = rate_limited_status_line(agent)
    assert "skipping" not in line
    assert line == "⚠️ Rate limited — switching to fallback provider..."


def test_all_exhausted_fails_fast_with_the_soonest_reset(monkeypatch):
    seen = _patch_resolver(monkeypatch)
    snapshot = {
        "claude-apx-1": _exhausted(resets_in=3 * 86400),
        "claude-apx-2": _exhausted(resets_in=6 * 3600),
    }
    agent = _agent([_entry("claude-apx-1"), _entry("claude-apx-2")], snapshot)

    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is False
    assert seen == []           # zero wasted round-trips
    assert "6h" in (agent._quota_gate_soonest_reset_text or "")


# ── the gate must not change behaviour it has no evidence about ─────────


def test_non_quota_failures_do_not_prune(monkeypatch):
    """A transport timeout says nothing about quota — walk the chain as before."""
    seen = _patch_resolver(monkeypatch)
    agent = _agent([_entry("claude-apx-1")], {"claude-apx-1": _exhausted()})
    assert try_activate_fallback(agent, reason=FailoverReason.timeout) is True
    assert seen == ["claude-apx-1"]


def test_empty_registry_preserves_the_historical_walk(monkeypatch):
    seen = _patch_resolver(monkeypatch)
    agent = _agent([_entry("claude-apx-1"), _entry("claude-apx-2")], {})
    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is True
    assert seen == ["claude-apx-1"]


def test_already_consumed_chain_entries_are_not_re_pruned(monkeypatch):
    """Pruning must only touch entries the walker has not reached yet."""
    _patch_resolver(monkeypatch)
    snapshot = {"claude-apx-1": _exhausted(), "claude-apx-17": _healthy()}
    agent = _agent([_entry("claude-apx-1"), _entry("claude-apx-17")], snapshot)
    agent._fallback_index = 1  # entry 0 already walked this turn
    apply_quota_gate(agent, snapshot=snapshot)
    # The consumed prefix is untouched, so the index still means the same thing.
    assert agent._fallback_chain[0]["provider"] == "claude-apx-1"
    assert agent._fallback_index == 1


def test_gate_runs_once_per_turn(monkeypatch):
    snapshot = {"claude-apx-1": _exhausted(), "claude-apx-17": _healthy()}
    agent = _agent([_entry("claude-apx-1"), _entry("claude-apx-17")], snapshot)
    first = apply_quota_gate(agent, snapshot=snapshot)
    second = apply_quota_gate(agent, snapshot=snapshot)
    assert first is not None and first.skipped_count == 1
    assert second is None, "a second call in the same turn must be a no-op"


def test_gate_failure_never_breaks_the_walk(monkeypatch):
    """A registry read that raises must leave the chain exactly as it was."""
    seen = _patch_resolver(monkeypatch)
    import agent.quota_registry_gate as qrg

    def _boom(*a, **k):
        raise RuntimeError("registry on fire")

    monkeypatch.setattr(qrg, "load_registry_snapshot", _boom)
    agent = _agent([_entry("claude-apx-1")], None)
    agent._quota_registry_snapshot = None
    assert try_activate_fallback(agent, reason=FailoverReason.rate_limit) is True
    assert seen == ["claude-apx-1"]
