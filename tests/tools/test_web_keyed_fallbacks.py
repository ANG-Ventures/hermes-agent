"""Configured keyed web fallbacks precede the anonymous rescue ring."""
import json

import pytest

from tools import web_tools
from tools.web_result_cache import search_memo


class Provider:
    def __init__(self, name, calls, *, search=None, extract=None):
        self.name = name
        self.calls = calls
        self._search = search
        self._extract = extract

    def supports_search(self):
        return self._search is not None

    def supports_extract(self):
        return self._extract is not None

    def is_available(self):
        return True

    def search(self, query, limit):
        self.calls.append(self.name)
        return self._search

    def extract(self, urls, **kwargs):
        self.calls.append(self.name)
        return [dict(r) for r in self._extract]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.web_search_provider.get_provider_env",
                        lambda key: "test-key" if key in ("TAVILY_API_KEY", "EXA_API_KEY") else "")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
        "search_backend": "firecrawl", "extract_backend": "firecrawl",
        "search_fallbacks": ["tavily", "exa"],
        "extract_fallbacks": ["tavily", "exa"],
    })
    async def safe(url):
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)
    calls = []
    providers = {}
    monkeypatch.setattr("agent.web_search_registry.get_provider", providers.get)
    return calls, providers


def test_search_order_and_identity(setup, monkeypatch):
    calls, providers = setup
    fail = {"success": False, "error": "HTTP 402"}
    ok = {"success": True, "data": {"web": [{"url": "https://ok.example"}]}}
    providers.update(firecrawl=Provider("firecrawl", calls, search=fail),
                     tavily=Provider("tavily", calls, search=fail),
                     exa=Provider("exa", calls, search=ok))
    monkeypatch.setattr(web_tools, "_rescue_search", lambda *args: pytest.fail("ring before keyed chain"))
    result = json.loads(web_tools.web_search_tool("unique keyed order"))
    assert calls == ["firecrawl", "tavily", "exa"]
    assert result["data"]["metadata"] == {"served_by": "exa", "fallback_from": "firecrawl"}


@pytest.mark.asyncio
async def test_extract_partial_failure_does_not_fallback(setup, monkeypatch):
    calls, providers = setup
    urls = ["https://a.example", "https://b.example"]
    providers["firecrawl"] = Provider("firecrawl", calls, extract=[
        {"url": urls[0], "content": "ok", "error": None},
        {"url": urls[1], "content": "", "error": "404"},
    ])
    monkeypatch.setattr(web_tools, "_rescue_extract", lambda *args: pytest.fail("ring called"))
    out = json.loads(await web_tools.web_extract_tool(urls))
    assert calls == ["firecrawl"]
    assert out["results"][1]["error"] == "404"
    assert out["results"][0]["metadata"] == {"served_by": "firecrawl"}


@pytest.mark.asyncio
async def test_extract_402_to_keyed_tavily_real_dispatch(setup, monkeypatch):
    calls, providers = setup
    urls = ["https://a.example"]
    providers.update(firecrawl=Provider("firecrawl", calls, extract=[
        {"url": urls[0], "content": "", "error": "HTTP 402 credits exhausted"}]),
        tavily=Provider("tavily", calls, extract=[
            {"url": urls[0], "content": "tavily content", "error": None}]))
    monkeypatch.setattr(web_tools, "_rescue_extract", lambda *args: pytest.fail("ring before keyed"))
    out = json.loads(await web_tools.web_extract_tool(urls))
    assert calls == ["firecrawl", "tavily"]
    assert out["results"][0]["content"] == "tavily content"
    assert out["results"][0]["metadata"] == {"served_by": "tavily", "fallback_from": "firecrawl"}


@pytest.mark.asyncio
async def test_exhausted_chain_reaches_ring(setup, monkeypatch):
    calls, providers = setup
    urls = ["https://a.example"]
    bad = [{"url": urls[0], "error": "HTTP 402"}]
    for name in ("firecrawl", "tavily", "exa"):
        providers[name] = Provider(name, calls, extract=bad)
    monkeypatch.setattr(web_tools, "_rescue_extract", lambda name, urls, results: [
        {"url": urls[0], "content": "ring", "metadata": {"rescued_from": name}}])
    monkeypatch.setattr(web_tools, "_rescue_eligible", lambda provider: True)
    out = json.loads(await web_tools.web_extract_tool(urls))
    assert calls == ["firecrawl", "tavily", "exa"]
    assert out["results"][0]["content"] == "ring"
