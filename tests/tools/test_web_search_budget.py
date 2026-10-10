"""Paid web_search daily budget + Firecrawl search cost caps (2026-10-09)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def budget_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.web_search_budget as b
    monkeypatch.setattr(b, "_budget", lambda: 3)
    return b


def test_free_providers_are_never_counted_or_refused(budget_home):
    b = budget_home
    assert b.budget_exceeded("searxng") is None
    assert b.record_call("searxng") == 0
    assert b.budget_exceeded("searxng") is None


def test_paid_provider_refused_once_budget_reached(budget_home):
    b = budget_home
    for _ in range(3):
        assert b.budget_exceeded("firecrawl") is None
        b.record_call("firecrawl")
    msg = b.budget_exceeded("firecrawl")
    assert msg and "daily budget reached (3/3" in msg
    # the cap is shared across paid providers, so a fallback paid vendor is refused too
    assert b.budget_exceeded("tavily")
    # ...but a free one still serves
    assert b.budget_exceeded("searxng") is None


def test_budget_state_resets_on_a_new_day(budget_home, monkeypatch):
    b = budget_home
    b.record_call("exa"); b.record_call("exa"); b.record_call("exa")
    assert b.budget_exceeded("exa")
    monkeypatch.setattr(b, "_today", lambda: "2099-01-01")
    assert b.budget_exceeded("exa") is None
    assert json.loads(b._state_path().read_text())["day"] != "2099-01-01"  # not rewritten until a call


def test_zero_budget_disables_the_guard(budget_home, monkeypatch):
    b = budget_home
    monkeypatch.setattr(b, "_budget", lambda: 0)
    for _ in range(10):
        b.record_call("firecrawl")
    assert b.budget_exceeded("firecrawl") is None


def test_breaker_search_refuses_without_calling_the_provider(budget_home, monkeypatch):
    b = budget_home
    for _ in range(3):
        b.record_call("firecrawl")
    from tools import web_tools
    provider = MagicMock(); provider.name = "firecrawl"
    resp = web_tools._breaker_search(provider, "q", 5)
    assert resp["success"] is False and "daily budget" in resp["error"]
    provider.search.assert_not_called()


def test_breaker_search_counts_a_paid_call(budget_home, monkeypatch):
    b = budget_home
    from tools import web_tools
    monkeypatch.setattr("tools.web_backend_breaker.open_until", lambda name: None)
    provider = MagicMock(); provider.name = "tavily"
    provider.search.return_value = {"success": True, "data": {"web": []}}
    web_tools._breaker_search(provider, "q", 5)
    assert json.loads(b._state_path().read_text())["counts"] == {"tavily": 1}


def test_firecrawl_search_caps_limit_at_ten(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    from plugins.web.firecrawl import provider as fcp
    client = MagicMock()
    client.search.return_value = {"data": {"web": [{"url": "https://a", "title": "A", "description": "d"}]}}
    monkeypatch.setattr(fcp, "_get_firecrawl_client", lambda: client)
    monkeypatch.setattr(fcp, "_use_keyless_ring", lambda: False)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    fcp.FirecrawlWebSearchProvider().search("q", limit=50)
    kwargs = client.search.call_args.kwargs
    assert kwargs["limit"] == fcp.FIRECRAWL_SEARCH_MAX_LIMIT == 10
    assert "scrapeOptions" not in kwargs and "scrape_options" not in kwargs
