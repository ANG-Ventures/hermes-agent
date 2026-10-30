"""Reader for the fleet's Anthropic limits catalog, ``<fleet root>/config/anthropic-limits.json``.

The catalog (hermes-home, spec plans/2026-10-30_anthropic-limits-catalog-SPEC.md 2.1) is the one source
of truth for Anthropic numbers the API does not publish: the request-body cap, per-model context windows
and the manual-thinking budget ladder. Consumers here (card t_f08ad655):

* ``plugins/model-providers/anthropic`` body cap = ``request_body_bytes - agent_headroom_bytes``;
* ``agent.model_metadata.DEFAULT_CONTEXT_LENGTHS`` Anthropic rows;
* ``agent.anthropic_adapter`` THINKING_BUDGET;
* ``agent.image_eviction_policy`` send-path image limit and byte budget (``images.*``, card t_3a0b1ce4).

Each keeps its in-code literal as the FALLBACK used when the catalog is missing or unreadable; hermes-home's
``scripts/lints/anthropic-limits-fallback-lint.py`` and ``scripts/anthropic-limits-render.py --check`` refuse a
literal that drifts from the catalog. The file is re-read when its ``mtime_ns`` changes (one ``stat`` per
lookup), so editing the catalog takes effect without a restart. A bad state logs ONE warning per episode: the
same missing/unreadable condition stays quiet until the catalog has been read cleanly again.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CATALOG_REL = Path("config") / "anthropic-limits.json"

_lock = threading.Lock()
# (path, mtime_ns) -> parsed catalog, or None when that state was bad.
_cache: Optional[Tuple[str, Optional[int], Optional[Dict[str, Any]]]] = None
_warned_state: Optional[str] = None


def catalog_path() -> Path:
    """``<fleet root>/config/anthropic-limits.json``; the root follows HERMES_HOME (profile homes map to it)."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / CATALOG_REL


def _validate(cat: Any) -> Dict[str, Any]:
    if not isinstance(cat, dict):
        raise ValueError("top level is not an object")
    for key in ("request_body_bytes", "agent_headroom_bytes", "models", "thinking_budget", "images"):
        if key in cat and not isinstance(cat[key], dict):
            raise ValueError(f"`{key}` is not an object")
    return cat


def _warn(state: str, msg: str) -> None:
    global _warned_state
    if _warned_state != state:
        _warned_state = state
        logger.warning("%s; using the in-code Anthropic limit fallbacks", msg)


def load_catalog() -> Optional[Dict[str, Any]]:
    """The parsed catalog, or None when it is missing/unreadable (callers then use their fallback)."""
    global _cache, _warned_state
    path = catalog_path()
    key = str(path)
    try:
        mtime: Optional[int] = path.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    except OSError as e:
        mtime = None
        with _lock:
            _cache = (key, None, None)
        _warn("unreadable:stat", f"Anthropic limits catalog {path} unreadable ({e.strerror or e})")
        return None
    with _lock:
        hit = _cache
    if hit is not None and hit[0] == key and hit[1] == mtime:
        return hit[2]
    if mtime is None:
        with _lock:
            _cache = (key, None, None)
        _warn("missing", f"Anthropic limits catalog {path} not found")
        return None
    try:
        cat: Optional[Dict[str, Any]] = _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        cat = None
        _warn(f"unreadable:{mtime}", f"Anthropic limits catalog {path} unreadable ({e})")
    else:
        _warned_state = None
    with _lock:
        _cache = (key, mtime, cat)
    return cat


def _int_value(cat: Dict[str, Any], key: str) -> Optional[int]:
    entry = cat.get(key)
    v = entry.get("value") if isinstance(entry, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def request_body_cap(fallback: Optional[int]) -> Optional[int]:
    """Agent preflight body cap: catalog ``request_body_bytes - agent_headroom_bytes``, else *fallback*."""
    cat = load_catalog()
    if cat is None:
        return fallback
    body, headroom = _int_value(cat, "request_body_bytes"), _int_value(cat, "agent_headroom_bytes")
    if body is None:
        return fallback
    cap = body - (headroom or 0)
    return cap if cap > 0 else fallback


def context_rows(cat: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, int]]:
    """Agent-table rows ``{model id or alias: context_window}`` (``agent_row: false`` skipped), or None.

    *cat* is a catalog already returned by :func:`load_catalog` (default: load it now)."""
    if cat is None:
        cat = load_catalog()
    if cat is None:
        return None
    rows: Dict[str, int] = {}
    for mid, m in (cat.get("models") or {}).items():
        if not isinstance(m, dict) or m.get("agent_row", True) is False:
            continue
        win = m.get("context_window")
        if not isinstance(win, int) or isinstance(win, bool) or win <= 0:
            continue
        for k in [mid, *(m.get("aliases") or [])]:
            if isinstance(k, str) and k:
                rows[k] = win
    return rows


def thinking_budget(fallback: Dict[str, int]) -> Dict[str, int]:
    """Manual-thinking budget ladder: *fallback* overlaid with the catalog's integer entries."""
    cat = load_catalog()
    if cat is None:
        return fallback
    tb = cat.get("thinking_budget") or {}
    over = {k: v for k, v in tb.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}
    return {**fallback, **over} if over else fallback


def image_limits(limit_fallback: int, budget_fallback: int) -> Tuple[int, int]:
    """Send-path image policy ``(block limit, byte budget)``: catalog ``images.stricter_cap_threshold_count`` and
    ``images.outbound_budget_bytes``, each falling back to its literal when the catalog or the entry is unusable."""
    cat = load_catalog()
    if cat is None:
        return limit_fallback, budget_fallback
    images = cat.get("images")
    if not isinstance(images, dict):
        return limit_fallback, budget_fallback
    limit = _int_value(images, "stricter_cap_threshold_count")
    budget = _int_value(images, "outbound_budget_bytes")
    return (limit or limit_fallback), (budget or budget_fallback)


def _reset_for_tests() -> None:
    global _cache, _warned_state
    with _lock:
        _cache = None
    _warned_state = None
