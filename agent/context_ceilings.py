"""Context ceilings observed on fallback lanes (t_5188f4ed).

A lane that rejects a request as too long far below the model's believed window (the claude-bpx
interactive session's own ceiling: 2026-11-17 08:06:24 claude-dtlr/claude-fable-5-2 at about 245k
estimated / 347k real of 1M) has a ceiling the transcript did not cause. The size it rejected is
an upper bound, kept here per (provider, model, endpoint) for the life of the process, so the
fallback walk does not pick that lane again for a request at least that large. Never written to
the model metadata cache: it is an observation, not the model's window.

The endpoint is the fallback entry's DECLARED ``base_url`` ("" when it declares none): the walk
reads it before resolving a client, and entries without one resolve the provider's default
endpoint in this process, so they share it.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

_LOCK = threading.Lock()
_CEILINGS: dict = {}


def _key(provider: Any, model: Any, base_url: Any) -> tuple:
    return (str(provider or "").strip().lower(), str(model or "").strip(),
            str(base_url or "").strip().rstrip("/").lower())


def record(provider: Any, model: Any, base_url: Any, tokens: int) -> None:
    """Keep the smallest request size this lane rejected."""
    key = _key(provider, model, base_url)
    with _LOCK:
        old = _CEILINGS.get(key)
        _CEILINGS[key] = tokens if old is None else min(old, tokens)


def ceiling(provider: Any, model: Any, base_url: Any = None) -> Optional[int]:
    with _LOCK:
        return _CEILINGS.get(_key(provider, model, base_url))


def too_large_for(provider: Any, model: Any, base_url: Any, request_tokens: int) -> bool:
    """The lane already rejected a request no larger than ``request_tokens``."""
    cap = ceiling(provider, model, base_url)
    return cap is not None and request_tokens >= cap


def request_tokens(agent: Any) -> int:
    """Size of the request the next lane would get: the last real prompt, or the request a
    fallback lane rejected this turn when larger (cleared when compression rewrites the
    history, so a shrunk request is not held to the pre-compression size)."""
    return max(int(getattr(getattr(agent, "context_compressor", None), "last_prompt_tokens", 0) or 0),
               int(getattr(agent, "_context_rejected_tokens", 0) or 0))


def clear() -> None:
    with _LOCK:
        _CEILINGS.clear()
