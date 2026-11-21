"""Cron agents carry the full provider_routing filter set (t_145e2be9).

``_construct_cron_agent`` passed only/ignore/order/sort and dropped ``require_parameters`` and
``data_collection``, so a cron agent ran with no data-collection deny even when config set one.
"""
from __future__ import annotations

from types import SimpleNamespace

from cron import scheduler

ROUTING = {"only": ["a"], "ignore": ["b"], "order": ["a", "c"], "sort": "price",
           "require_parameters": True, "data_collection": "deny"}


def _build(monkeypatch, cfg):
    monkeypatch.setattr(scheduler, "_resolve_cron_enabled_toolsets", lambda job, c: None)
    monkeypatch.setattr(scheduler, "_resolve_cron_disabled_toolsets", lambda c: None)
    built = {}

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    setup = SimpleNamespace(runtime={}, model="m", max_iterations=3, reasoning_config=None,
                            prefill_messages=None, fallback_model=None, credential_pool=None,
                            model_slot=None)
    scheduler._construct_cron_agent(FakeAgent, {"id": "j"}, cfg, setup, workdir=None,
                                    session_id="s", session_db=None)
    return built


def test_cron_agent_gets_all_six_provider_routing_filters(monkeypatch):
    built = _build(monkeypatch, {"provider_routing": ROUTING})
    assert built["providers_allowed"] == ["a"] and built["providers_ignored"] == ["b"]
    assert built["providers_order"] == ["a", "c"] and built["provider_sort"] == "price"
    assert built["provider_require_parameters"] is True
    assert built["provider_data_collection"] == "deny"


def test_cron_agent_without_a_routing_block_passes_the_defaults(monkeypatch):
    built = _build(monkeypatch, {})
    assert built["providers_allowed"] is None and built["provider_data_collection"] is None
    assert built["provider_require_parameters"] is False
