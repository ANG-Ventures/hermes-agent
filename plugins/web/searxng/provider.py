"""SearXNG search via a user-hosted instance (``/search?format=json``).

Search-only — SearXNG aggregates upstream engines but does not fetch URLs.
Env: ``SEARXNG_URL=http://localhost:8080``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from plugins.web._common import BaseWebSearchProvider, http_get_json, provider_env, search_fail, search_ok, setup_schema, titled_rows

logger = logging.getLogger(__name__)


class SearXNGWebSearchProvider(BaseWebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    NAME = "searxng"
    DISPLAY_NAME = "SearXNG"
    KEY_ENV = "SEARXNG_URL"

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        base_url = provider_env("SEARXNG_URL").rstrip("/")
        if not base_url:
            return search_fail("SEARXNG_URL is not set")
        data, failure = http_get_json(
            "SearXNG", f"{base_url}/search", params={"q": query, "format": "json", "pageno": 1},
            headers={"Accept": "application/json"}, timeout=15, logger=logger, reach_target=f"SearXNG at {base_url}",
        )
        if failure is not None:
            return failure
        raw_results = data.get("results", [])
        # SearXNG answers HTTP 200 with ``results: []`` when every upstream engine it scrapes
        # refused it (CAPTCHA / 403 / 429) — the refusals are listed in ``unresponsive_engines``.
        # That is a provider failure, not an honest "no hits": surface it as one so the keyed
        # ``search_fallbacks`` chain in tools/web_tools.py actually runs (2026-10-09 the whole
        # fleet served empty searches as success for two days this way).
        unresponsive = data.get("unresponsive_engines") or []
        if not raw_results and unresponsive:
            engines = ", ".join(
                f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) >= 2 else str(e)
                for e in unresponsive
            )
            logger.warning("SearXNG search '%s': 0 results, all engines unresponsive (%s)", query, engines)
            return search_fail(f"SearXNG returned no results; upstream engines unresponsive ({engines})")
        # Degraded-engine guard: a soft-blocked engine (Bing from a residential IP, 2026-10-09)
        # still answers, but ignores most of the query and returns generic pages for its first
        # word ("32 (number) - Wikipedia" for a monitor query). If the result set covers fewer
        # than half of the query's significant terms AND engines are reporting trouble, treat it
        # as a failure so a real provider gets the call instead of plausible-looking noise.
        tokens = {t for t in re.findall(r"[a-z0-9]{4,}", query.lower())}
        if raw_results and unresponsive and len(tokens) >= 2:
            haystack = " ".join(f"{r.get('title', '')} {r.get('content', '')}" for r in raw_results).lower()
            coverage = sum(1 for t in tokens if t in haystack) / len(tokens)
            if coverage < 0.5:
                logger.warning("SearXNG search '%s': degraded results (term coverage %.0f%%, engines unresponsive: %s)",
                               query, coverage * 100, unresponsive or "none")
                return search_fail(
                    f"SearXNG results look degraded (only {coverage:.0%} of query terms present; "
                    f"unresponsive engines: {unresponsive or 'none'})")
        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(raw_results, key=lambda r: float(r.get("score", 0)), reverse=True)[:limit]
        web_results = titled_rows(sorted_results, "content")
        logger.info("SearXNG search '%s': %d results (from %d raw, limit %d)", query, len(web_results), len(raw_results), limit)
        return search_ok(web_results)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "SearXNG", "free · self-hosted", "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "SEARXNG_URL", "SearXNG instance URL (e.g. http://localhost:8080)", "https://searx.space/",
        )
