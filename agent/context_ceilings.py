"""Context ceilings observed on fallback lanes (t_5188f4ed).

A lane that rejects a request as too long far below the model's believed window (the claude-bpx
interactive session's own ceiling: 2026-11-17 08:06:24 claude-dtlr/claude-fable-5-2 at about 245k
estimated / 347k real of 1M) has a ceiling the transcript did not cause. The size it rejected is
an upper bound, kept here per (provider, model) for the life of the process, so the fallback walk
does not pick that lane again for a request at least that large. Never written to the model
metadata cache: it is an observation, not the model's window.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

_LOCK = threading.Lock()
_CEILINGS: dict = {}


def _key(provider: Any, model: Any) -> tuple:
    return (str(provider or "").strip().lower(), str(model or "").strip())


def record(provider: Any, model: Any, tokens: Any) -> None:
    """Keep the smallest request size this lane rejected."""
    try:
        n = int(tokens)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    key = _key(provider, model)
    with _LOCK:
        old = _CEILINGS.get(key)
        _CEILINGS[key] = n if old is None else min(old, n)


def ceiling(provider: Any, model: Any) -> Optional[int]:
    with _LOCK:
        return _CEILINGS.get(_key(provider, model))


def too_large_for(provider: Any, model: Any, request_tokens: Any) -> bool:
    """The lane already rejected a request no larger than ``request_tokens``."""
    cap = ceiling(provider, model)
    try:
        return cap is not None and int(request_tokens) >= cap
    except (TypeError, ValueError):
        return False


def clear() -> None:
    with _LOCK:
        _CEILINGS.clear()
