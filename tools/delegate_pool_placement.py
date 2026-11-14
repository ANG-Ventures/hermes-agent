"""Opt-in pool placement for delegate_task / fork children (t_30537365).

Assessment: plans/2026-11-13_delegate-children-on-pool-hosts-ASSESSMENT.md section 7, option (a). The child's
model loop stays in the gateway; only its terminal, file and execute_code commands run over the ``ssh``
terminal backend on a fleet pool host, the same way a spilled kanban worker's do (KWLB v0.1).

- ``delegation.pool_placement: off | opt_in`` (default ``off``) is the kill switch.
- The parent asks per task with ``placement: "pool"``.
- An eligible child leases a host from the Fleet Work Allocation System through ``alloc_client`` (consumer
  ``kanban``: the pool hosts enroll that consumer, and a child's commands cost what a worker's do), builds the
  ``TERMINAL_*`` values with ``kanban_worker_hosts.placement_env`` and binds them as a terminal scope inside the
  child's own turn only. Its environment is keyed by its own task id (:func:`bound_env_key`), never the
  parent's ``session:<key>``, so parent and child never share an environment object.
- The lease is released when the child's handles are released (every exit path, the late-completion path too).
- Every failure fails OPEN to local with a loud line in the child's goal (its transcript) and a counter.
  Placement never makes a child wait in a queue and never refuses one.
- Nothing from the operator's home is copied (``TERMINAL_SSH_SYNC_FILES=false``): commands run as the pool
  worker account with that host's own credentials.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLACEMENT_POOL = "pool"
MODE_OFF, MODE_OPT_IN = "off", "opt_in"
ENV_KEY_PREFIX = "delegate-pool:"
NOTE_PREFIX = "[pool placement]"
_RM_TIMEOUT_S = 8

_env_key_var: ContextVar[Optional[str]] = ContextVar("hermes_delegate_pool_env_key", default=None)

COUNTERS: Counter = Counter()
_counters_lock = threading.Lock()


def _count(key: str) -> None:
    with _counters_lock:
        COUNTERS[key] += 1


def counters() -> Dict[str, int]:
    """Snapshot of the placement counters (``placed``, ``fallback:<reason>``)."""
    with _counters_lock:
        return dict(COUNTERS)


def bound_env_key() -> Optional[str]:
    """The env-cache key of the pool-placed child whose turn is running in this context, else None."""
    return _env_key_var.get()


def mode() -> str:
    """``delegation.pool_placement``; anything but ``opt_in`` is ``off``."""
    try:
        from tools.delegate_tool_config import _cfg
        value = str((_cfg() or {}).get("pool_placement") or MODE_OFF).strip().lower()
    except Exception:
        return MODE_OFF
    return MODE_OPT_IN if value == MODE_OPT_IN else MODE_OFF


@dataclass
class PoolLease:
    host: str                 # pool host id (fleet-roles name)
    reservation_id: str
    env_key: str
    workspace: str
    values: Dict[str, str]    # the TERMINAL_* overlay
    pool_host: object = None  # kanban_worker_pool.PoolHost (for the workspace cleanup)
    _stop: threading.Event = field(default_factory=threading.Event)
    _released: bool = False


def _safe_id(child_task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(child_task_id or "")) or "child"


def _lease_slug(child_task_id: str) -> str:
    """One id per lease for the env key, remote workspace and ssh control scope. The child id alone is not
    unique enough: _safe_id is lossy, child ids carry only 8 hex chars, and the workspace sits on a scratch
    root other gateways' children share, which release() deletes."""
    return f"{_safe_id(child_task_id)}-{uuid.uuid4().hex[:12]}"


def _pool_hosts() -> List[object]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    cfg = kwp.read_pool(kb.kanban_home() / "fleet", kanban_cfg=kbd._root_kanban_cfg())
    return [h for h in cfg.pool_hosts if h.enabled and h.state == "active"]


def _workspace_for(slug: str) -> str:
    """Same absolute path on every pool host: the default board's scratch root, which pool workers write."""
    from hermes_cli import kanban_db as kb
    return str(kb.workspaces_root() / f"delegate-{slug}")


def _admit(child_task_id: str, hosts: List[object]):
    """``(PoolHost, reservation id)`` or ``(None, reason)``. Never raises."""
    from hermes_cli import kanban_headroom as kh
    from hermes_cli.fleet_placement import alloc_client
    names = [h.name for h in hosts]
    table = kh.load_table(alloc_client._fleet_root())
    cands = [(table.resolve(n) or n) for n in names] if table is not None else names
    try:
        r = alloc_client.admit(kh.CONSUMER, job_id=f"delegate:{_safe_id(child_task_id)}#{uuid.uuid4().hex[:12]}",
                               ttl_s=kh.RESERVE_TTL_S, candidates=cands)
    except Exception as exc:  # alloc_client.Refused (terminal answer) or anything unexpected
        return None, f"allocator refused ({type(exc).__name__}: {exc})"
    if not r.host or not r.reservation_id:
        return None, f"no lease ({r.reason or 'refused'})"
    key = kh.pool_key(table, names, r.host) if table is not None else (r.host if r.host in names else None)
    host = next((h for h in hosts if h.name == key), None)
    if host is None:
        kh.release_alloc(r.reservation_id)
        return None, f"allocator picked {r.host}, not a pool host"
    return host, r.reservation_id


def acquire(child_task_id: str, placement) -> Tuple[Optional[PoolLease], Optional[str]]:
    """Lease a pool host for one child.

    ``(lease, note)`` when placed, ``(None, note)`` when the child falls back to local (the note is the loud
    transcript line), ``(None, None)`` when the task did not ask. Never raises, never waits on a queue.
    """
    if not isinstance(placement, str) or not placement:
        return None, None

    def _fallback(reason: str, detail: str) -> Tuple[None, str]:
        _count(f"fallback:{reason}")
        note = (f"{NOTE_PREFIX} placement \"pool\" was requested but this child runs its commands LOCALLY on "
                f"the gateway host: {detail}.")
        logger.warning("delegate pool placement: child %s falls back to local: %s", child_task_id, detail)
        return None, note

    if placement != PLACEMENT_POOL:
        return _fallback("invalid_value", f"unknown placement value {placement!r} (only \"pool\")")
    if mode() != MODE_OPT_IN:
        return _fallback("knob_off", "delegation.pool_placement is off")
    try:
        hosts = _pool_hosts()
    except Exception as exc:
        return _fallback("no_pool", f"pool unreadable ({type(exc).__name__}: {exc})")
    if not hosts:
        return _fallback("no_pool", "no active pool host")
    try:
        host, rid = _admit(child_task_id, hosts)
    except Exception as exc:
        return _fallback("no_lease", f"allocator error ({type(exc).__name__}: {exc})")
    if host is None:
        return _fallback("no_lease", rid)
    from hermes_cli import kanban_headroom as kh
    slug = _lease_slug(child_task_id)
    try:
        workspace = _workspace_for(slug)
        from hermes_cli.kanban_worker_hosts import placement_env, prepare_remote_workspace
        prepare_remote_workspace(host, workspace)  # the probe: proves ssh + a writable root (bounded, 8 s)
        values = placement_env(host, workspace)
        values["TERMINAL_SSH_CONTROL_SCOPE"] = f"dlg-{slug}"
    except Exception as exc:
        kh.release_alloc(rid)
        return _fallback("probe_failed", f"probe of {host.name} failed ({type(exc).__name__}: {exc})")
    lease = PoolLease(host=host.name, reservation_id=rid, env_key=f"{ENV_KEY_PREFIX}{slug}",
                      workspace=workspace, values=values, pool_host=host)
    threading.Thread(target=_renew_loop, args=(lease,), daemon=True, name=f"delegate-pool-renew-{slug}").start()
    _count("placed")
    logger.info("delegate pool placement: child %s placed on %s (%s), reservation %s",
                child_task_id, host.name, host.target, rid)
    note = (f"{NOTE_PREFIX} Your terminal, file and execute_code tools run on fleet pool host {host.name} as "
            f"{host.target}, in {workspace}. It has its own credentials and none of the operator's home or "
            f"live state: fetch what you need there (git, gh), and push anything you must keep.")
    return lease, note


def _renew_loop(lease: PoolLease) -> None:
    from hermes_cli import kanban_headroom as kh
    from hermes_cli.fleet_placement import alloc_client
    while not lease._stop.wait(kh.RENEW_EVERY_S):
        try:
            alloc_client.renew(lease.reservation_id, ttl_s=kh.RESERVE_TTL_S, consumer=kh.CONSUMER)
        except Exception as exc:
            logger.warning("delegate pool placement: renew of %s failed (TTL bounds it): %s",
                           lease.reservation_id, exc)


def _scope_with(values: Dict[str, str]) -> Optional[Dict[str, str]]:
    """The active terminal policy overlaid with ``values``; None under a refusal scope (it stays refused)."""
    from tools.terminal_scope import TerminalPolicyRefusal, get_terminal_scope
    current = get_terminal_scope()
    if isinstance(current, TerminalPolicyRefusal):
        return None
    if isinstance(current, dict):
        base = dict(current)
    else:  # no scope bound: the process env is today's policy, copied so the overlay is total
        base = {k: v for k, v in os.environ.items() if k.startswith("TERMINAL_")}
    base.update(values)
    return base


@contextmanager
def bound(lease: Optional[PoolLease]) -> Iterator[None]:
    """Bind the child's pool scope + env key for one child turn (no-op without a lease). Always resets."""
    if not isinstance(lease, PoolLease) or lease._released:
        yield
        return
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    scope = _scope_with(lease.values)
    if scope is None:
        yield
        return
    scope_token = set_terminal_scope(scope)
    key_token = _env_key_var.set(lease.env_key)
    try:
        yield
    finally:
        _env_key_var.reset(key_token)
        reset_terminal_scope(scope_token)


def release(lease: Optional[PoolLease]) -> None:
    """End the lease: tear down the child's env, release the reservation, remove the workspace. Idempotent,
    never raises."""
    if not isinstance(lease, PoolLease) or lease._released:
        return
    lease._released = True
    lease._stop.set()
    try:
        from tools.terminal_tool_lifecycle import cleanup_vm
        cleanup_vm(lease.env_key)
    except Exception as exc:
        logger.debug("delegate pool placement: env cleanup of %s failed: %s", lease.env_key, exc)
    if lease.pool_host is not None and lease.workspace:
        # The rm runs on the leased host, so it runs under the reservation; the reservation is released after.
        _start_cleanup(lease)
    else:
        _release_reservation(lease)


def _release_reservation(lease: PoolLease) -> None:
    try:
        from hermes_cli import kanban_headroom as kh
        kh.release_alloc(lease.reservation_id)
    except Exception as exc:
        logger.warning("delegate pool placement: release of %s failed (TTL bounds it): %s",
                       lease.reservation_id, exc)


def _remove_then_release(lease: PoolLease) -> None:
    try:
        _remove_workspace(lease)
    finally:
        _release_reservation(lease)


def _start_cleanup(lease: PoolLease) -> None:
    threading.Thread(target=_remove_then_release, args=(lease,), daemon=True).start()


def _remove_workspace(lease: PoolLease) -> None:
    import shlex
    try:
        from hermes_cli.kanban_worker_pool import _ssh_argv
        from tools.environments.local import build_subprocess_env
        env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
        env["ALLOC_RESERVATION"] = lease.reservation_id
        subprocess.run(_ssh_argv(lease.pool_host, f"rm -rf -- {shlex.quote(lease.workspace)}"),
                       capture_output=True, timeout=_RM_TIMEOUT_S, stdin=subprocess.DEVNULL,
                       env=env)
    except Exception as exc:
        logger.debug("delegate pool placement: workspace removal on %s failed: %s", lease.host, exc)
