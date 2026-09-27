"""Claim headers for auxiliary calls to gemini-bridge (attribution Phase 2).

Spec: ``plans/gemini-bridge-attribution-20260927/SPEC.md`` §3.B. The bridge
(``gemini-bridge`` ``call_log.CLAIM_HEADERS``) records these per call as
*claimed* fields next to the credential-derived ``consumer``:

  x-hermes-profile   active profile name (``default`` for the root home)
  x-hermes-aux-task  auxiliary task (``title_generation``, ``vision``, ...)
  x-hermes-session   session the aux call serves, when one is bound

They are CLAIMS ONLY (I5): the bridge never uses them to decide auth or
consumer. Scope is the gemini-bridge provider (and its aliases) only. The
bridge is our own loopback/LAN service; it hands the prompt to a spawned
``agy`` CLI, so these request headers never egress to a third party. Values
are clipped to the bridge's claim charset and 64 characters, so what we send
is exactly what the bridge stores.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

GEMINI_BRIDGE_PROVIDERS = frozenset({"gemini-bridge", "gemini-ultra", "antigravity"})
PROFILE_HEADER = "x-hermes-profile"
AUX_TASK_HEADER = "x-hermes-aux-task"
SESSION_HEADER = "x-hermes-session"
CLAIM_HEADERS = (PROFILE_HEADER, AUX_TASK_HEADER, SESSION_HEADER)

# Same charset + limit as gemini-bridge call_log.ident().
_CLAIM_BAD = re.compile(r"[^A-Za-z0-9_.:/-]")
_LIMIT = 64


def _clip(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    out = _CLAIM_BAD.sub("_", value.strip()[:_LIMIT])
    return out or None


def _profile() -> Optional[str]:
    try:
        from agent.fork_ext.relay_headers import _call_id_profile

        return _clip(_call_id_profile())
    except Exception:
        return None


def _session() -> Optional[str]:
    # The agent loop (and the background titler thread) publish the session
    # the aux call is accounted to; that is the session this call serves.
    try:
        from agent.aux_accounting import get_accounting_context

        ctx = get_accounting_context()
    except Exception:
        return None
    if not ctx:
        return None
    return _clip(ctx[1])


def is_gemini_bridge(provider) -> bool:
    return isinstance(provider, str) and provider.strip().lower() in GEMINI_BRIDGE_PROVIDERS


def claim_headers(provider, task) -> Dict[str, str]:
    """Claim headers for one auxiliary request, or ``{}`` off gemini-bridge.

    Never raises: attribution must not break an aux call.
    """
    try:
        if not is_gemini_bridge(provider):
            return {}
        out: Dict[str, str] = {}
        for name, value in (
            (PROFILE_HEADER, _profile()),
            (AUX_TASK_HEADER, _clip(task) or "unspecified"),
            (SESSION_HEADER, _session()),
        ):
            if value:
                out[name] = value
        return out
    except Exception:
        return {}


def merge_extra_headers(kwargs: dict, extra_headers) -> None:
    """Apply caller ``extra_headers`` on top of the built ones (caller wins).

    Replaces the old ``kwargs["extra_headers"] = dict(extra_headers)``, which
    silently discarded the claim headers whenever a caller passed its own.
    """
    if not extra_headers:
        return
    merged = dict(kwargs.get("extra_headers") or {})
    merged.update(extra_headers)
    kwargs["extra_headers"] = merged
