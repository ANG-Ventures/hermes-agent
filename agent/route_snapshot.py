"""Synchronize delegation snapshots with in-place route activation, per agent."""
from __future__ import annotations

import copy
import functools
import threading



def route_lock(agent):
    return vars(agent).setdefault("_route_snapshot_lock", threading.RLock())


def route_mutation(fn):
    @functools.wraps(fn)
    def synchronized(agent, *args, **kwargs):
        with route_lock(agent):
            return fn(agent, *args, **kwargs)
    return synchronized


def snapshot_parent(parent, *, timeout=2.0):
    """An execution view with copied configuration and no borrowed transport clients.

    Non-route parent facilities (callbacks, session store and ownership locks) remain
    the parent's; the view never runs a parent turn or resets its primary runtime.
    """
    from tools.delegate_tool_config import _inherit_parent_endpoint

    lock = route_lock(parent)
    if not lock.acquire(timeout=timeout):
        raise RuntimeError("boomerang routing-unavailable: parent route activation is still in progress")
    try:
        fields = dict(vars(parent))
        base_url, key = _inherit_parent_endpoint(parent, parent.base_url, getattr(parent, "api_key", None))
        fields.update(base_url=base_url, api_key=key, client=None, _anthropic_client=None)
        fields["_client_kwargs"] = {"base_url": base_url, "api_key": key}
        for name in ("_fallback_chain", "request_overrides", "reasoning_config", "capabilities",
                     "providers_allowed", "providers_ignored", "providers_order", "acp_args"):
            if name in fields:
                fields[name] = copy.deepcopy(fields[name])
        view = copy.copy(parent)
        vars(view).clear()
        vars(view).update(fields)
        return view
    finally:
        lock.release()
