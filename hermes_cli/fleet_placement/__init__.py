"""Byte copies of hermes-home ``scripts/lib/{placement,admission,work_estimate,admission_demand,alloc_client}.py``
(t_29403e19).

The fleet has ONE placement scorer and ONE admission predicate; the fork may not import hermes-home code, so it
carries copies. ``VENDORED.json`` pins the source ref and each file's sha256, and
``tests/hermes_cli/test_fleet_placement_vendored.py`` fails when a copy's digest differs from the pin or the pin is
older than ``max_age_days``. Re-vendor by copying the files byte for byte and updating ``VENDORED.json``; never edit
the copies here. Every fork-side difference lives in this adapter.

The one difference: the state root. hermes-home resolves ``PLACEMENT_HOME`` else the checkout two levels above
``scripts/lib``; here the root is the shared kanban root (``<root>/config/fleet-hosts.json``, ``<root>/var/placement``),
resolved at call time so a test's ``HERMES_KANBAN_HOME`` binds it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import admission, placement, work_estimate

__all__ = ["admission", "admission_demand", "placement", "work_estimate"]


def __getattr__(name):
    # admission_demand imports fcntl/pwd at module top (POSIX-only); loading it eagerly made
    # `import gateway.run` fail on native Windows. It loads on first use instead, root bound here.
    if name != "admission_demand":
        raise AttributeError(name)
    import importlib

    mod = importlib.import_module(f"{__name__}.admission_demand")
    mod._root = _demand_root
    return mod


def _fleet_root(root=None) -> Path:
    # The canonical fleet_root(root=None) takes an explicit root first (call-scoped, FWA-2); keep that order.
    if root is not None:
        return Path(root)
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    from hermes_cli import kanban_db

    return kanban_db.kanban_home()


def _demand_root(root=None) -> Path:
    return Path(root) if root is not None else placement.fleet_root()


placement.fleet_root = _fleet_root


#: t_77aa2e0e: the fleet-root receipt the canonical admission_demand requires for every reservation/ledger write
#: (``_receipt_path``). Unset in a gateway env = every kanban reservation write refuses, one error per write.
ROOT_RECEIPT_ENV = "PLACEMENT_ROOT_RECEIPT"


def root_receipt_missing(environ=None):
    """One loud line for the gateway log when ``PLACEMENT_ROOT_RECEIPT`` is unset, else None. Startup check only:
    the per-write refusal in admission_demand stays (fail closed); this names the cause once, at boot."""
    env = os.environ if environ is None else environ
    if (env.get(ROOT_RECEIPT_ENV) or "").strip():
        return None
    return (f"{ROOT_RECEIPT_ENV} is UNSET in this process: every kanban placement reservation write will refuse "
            f"('{ROOT_RECEIPT_ENV} required'). Set it in the gateway's launchd EnvironmentVariables (the value "
            f"ai.hermes.allocd.plist carries) and FULL-reload the gateway (kickstart does not reread plist env).")
