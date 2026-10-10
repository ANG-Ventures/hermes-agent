"""Daily spend guard for PAID web search (fleet-wide, per HERMES_HOME).

Firecrawl/Tavily/Exa bill per call; one runaway day (2026-09-08: ~3,190 web calls from a
5-deep delegate tree in 32 min) emptied a monthly pool. This module counts paid-provider
search calls per UTC day in ``<hermes_home>/state/web_search_budget.json`` and refuses the
primary once ``web.search_daily_budget`` is reached, so the keyed fallback chain (and
eventually the keyless ring) serves the rest of the day instead of the paid pool going to zero.

Config: ``web.search_daily_budget`` (int, default 300; 0 disables). The cap applies to
providers in ``PAID_PROVIDERS``; free/self-hosted ones (searxng, ddgs, brave-free) are uncounted.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DAILY_BUDGET = 300
PAID_PROVIDERS = frozenset({"firecrawl", "tavily", "exa", "parallel", "perplexity", "xai", "brave", "serper", "linkup"})
_LOCK = threading.Lock()


def _budget() -> int:
    try:
        from tools.web_tools import _load_web_config
        raw = _load_web_config().get("search_daily_budget", DEFAULT_DAILY_BUDGET)
        return max(0, int(raw))
    except Exception:  # noqa: BLE001 — config problems must never break tools
        return DEFAULT_DAILY_BUDGET


def _state_path() -> Path:
    from hermes_constants import get_hermes_home
    p = Path(get_hermes_home()) / "state" / "web_search_budget.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load() -> dict:
    try:
        d = json.loads(_state_path().read_text())
        if d.get("day") == _today():
            return d
    except Exception:  # noqa: BLE001 — missing/corrupt file = fresh day
        pass
    return {"day": _today(), "counts": {}}


def budget_exceeded(provider_name: str) -> Optional[str]:
    """Return a refusal message if ``provider_name`` is paid and today's paid-search count has
    reached the budget; else None. Does not increment."""
    if provider_name not in PAID_PROVIDERS:
        return None
    cap = _budget()
    if cap <= 0:
        return None
    with _LOCK:
        d = _load()
        used = sum(d["counts"].values())
    if used >= cap:
        return (f"web_search daily budget reached ({used}/{cap} paid searches today, UTC); refusing "
                f"'{provider_name}' to protect the paid pool — set web.search_daily_budget to raise it")
    return None


def record_call(provider_name: str) -> int:
    """Count one paid search; returns today's total paid count."""
    if provider_name not in PAID_PROVIDERS:
        return 0
    with _LOCK:
        d = _load()
        d["counts"][provider_name] = d["counts"].get(provider_name, 0) + 1
        total = sum(d["counts"].values())
        try:
            from utils import atomic_json_write
            atomic_json_write(_state_path(), d)
        except Exception as exc:  # noqa: BLE001
            logger.debug("web_search_budget write failed: %s", exc)
    cap = _budget()
    if cap and total in (cap // 2, int(cap * 0.9)):
        logger.warning("web_search daily budget at %d/%d paid searches", total, cap)
    return total
