"""Which profile a memory-provider instance belongs to (#140058, #146293).

Ported from upstream NousResearch/hermes-agent (419e6627aa, d0154fe03b, c9de60702c): the
instance-claim half only. Each agent's ``MemoryManager`` binds its provider instance to one
profile home in ``initialize_all``; a plugin that hands several agents the SAME instance is
refused for every profile after the first, instead of being re-initialized (re-pointed) to it.
Single-profile processes name the same home on every claim and are unaffected.

Upstream's caller half (refusing tool calls and lifecycle hooks whose context names another
profile) is not ported here.

Refusals log the check's code and both homes; the user only ever sees a generic startup warning.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHARED_INSTANCE_REFUSAL = "memory_provider_instance_shared"

# id(provider) -> (weak reference to that very instance, profile home key). Keyed by identity, never
# by the provider's own __hash__/__eq__: an unhashable or value-equal provider is still tracked.
_claims: dict[int, tuple[weakref.ref, str]] = {}
_claims_lock = threading.RLock()  # reentrant: a weakref callback can fire from GC inside the lock
# A no-weakref provider is kept alive by its managers. Weak manager refs keep its home
# claim until the last live manager is gone, without pinning the provider itself.
_no_weakref_claims: dict[int, tuple[str, list[weakref.ref]]] = {}


def _home_key(home: Any) -> Optional[str]:
    if not home:
        return None
    from hermes_constants import hermes_home_key
    try:
        return hermes_home_key(home)
    except (OSError, RuntimeError, ValueError):  # unresolvable path: no key
        return None


def bound_scope(hermes_home: Any, agent_identity: Any = None) -> dict[str, Any]:
    """What ``initialize_all`` records as ``MemoryManager._bound_scope``: the home (and agent identity)
    its providers were bound to, with the home's key resolved once."""
    return {"hermes_home": hermes_home, "agent_identity": agent_identity, "home_key": _home_key(hermes_home)}


def _forget(pid: int, ref: weakref.ref) -> None:
    # Only this dead instance's own entry: once it is collected its id() can be reused by a new
    # provider whose claim already replaced the entry, and that claim must survive.
    with _claims_lock:
        if _claims.get(pid, (None,))[0] is ref:
            del _claims[pid]


def _forget_no_weakref(pid: int, dead: weakref.ref) -> None:
    with _claims_lock:
        entry = _no_weakref_claims.get(pid)
        if entry is None or not any(ref is dead for ref in entry[1]):
            return
        live = [ref for ref in entry[1] if ref is not dead and ref() is not None]
        if live:
            _no_weakref_claims[pid] = (entry[0], live)
        else:
            del _no_weakref_claims[pid]


def claim_instance(provider: Any, home: Any, *, manager: Any = None) -> bool:
    """Record that ``provider`` (the instance) is bound to ``home``. False when another profile
    already bound this same live instance, whatever the multiplex flag says (#146293).
    No-weakref instances can be tracked through a live manager that holds them.

    ``MemoryManager.initialize_all`` looks this up through the ``agent.memory_manager`` module
    global, so a host (hermes-webui) can wrap it there."""
    from agent.secret_scope import is_multiplex_active
    key = _home_key(home) or str(home)
    multiplexed = is_multiplex_active()
    pid = id(provider)
    with _claims_lock:
        entry = _claims.get(pid)
        owner = entry[1] if entry is not None and entry[0]() is provider else None
        if owner is not None and owner != key:
            logger.warning("Memory provider '%s' refused for profile home %r (%s): this same provider instance "
                           "is already bound to %r; a provider must give every agent its own instance",
                           getattr(provider, "name", "?"), str(home), SHARED_INSTANCE_REFUSAL, owner)
            return False
        try:
            ref = weakref.ref(provider, lambda dead, pid=pid: _forget(pid, dead))
        except TypeError:
            if multiplexed:
                logger.warning("Memory provider '%s' refused for profile home %r (%s): the instance can't be "
                               "tracked (no weak reference support), so sharing it can't be ruled out",
                               getattr(provider, "name", "?"), str(home), SHARED_INSTANCE_REFUSAL)
                return False
            # In non-multiplexed hosts, a manager can anchor a claim even when the
            # provider has no weakref slot. This also covers hosts serving multiple homes.
            if manager is not None:
                previous = _no_weakref_claims.get(pid)
                live = [ref for ref in previous[1] if ref() is not None] if previous else []
                if live and previous[0] != key:
                    logger.warning("Memory provider '%s' refused for profile home %r (%s): this same provider "
                                   "instance is already bound to %r; a provider must give every agent its own "
                                   "instance", getattr(provider, "name", "?"), str(home),
                                   SHARED_INSTANCE_REFUSAL, previous[0])
                    return False
                ref = weakref.ref(manager, lambda dead, pid=pid: _forget_no_weakref(pid, dead))
                _no_weakref_claims[pid] = (key, [*live, ref])
            # Preserve the historical direct-call fallback without an owning manager.
            return True
        _claims[pid] = (ref, key)
    return True
