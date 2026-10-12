"""Studio-local kanban workers under a dedicated non-sudo uid (``kanban.local_worker_uid``).

Q8 (a) of the Browser Sandboxes spec (Ace 2026-10-12, card t_405acdf0): a worker that runs as the
dispatcher's uid inherits that uid's NOPASSWD sudo and its Docker socket, so the boundary has to
be the WORKER's uid. The knob names that uid's login (``_kanbanw``); unset, empty or the
dispatcher's own login keeps today's spawn byte for byte.

Mechanism: the worker-pool placement the Linux pool hosts already prove (``kanban_worker_hosts``).
The worker process stays in the dispatcher's uid (same pid-anchored owner grant, same kanban DB,
same model credentials); its terminal and file tools run as the knob's user over
``ssh <user>@localhost``, so nothing the tools execute inherits the dispatcher's env, sudo or
sockets. No sudo or setuid helper is involved on the spawn path.

Read per tick (``load_config_readonly`` is mtime-cached), so the flip and the rollback are one
config edit, no restart.
"""

from __future__ import annotations

import getpass
import logging
import re
from typing import Any, Mapping, Optional

from hermes_cli.kanban_worker_pool import PoolHost

KNOB = "local_worker_uid"
LOCAL_HOST_NAME = "studio-local"
LOCAL_SSH_HOST = "localhost"

# A POSIX login, the hidden ``_name`` form included. Anything else is refused, not guessed.
_LOGIN_RE = re.compile(r"_?[a-z][a-z0-9_-]{0,30}")
_log = logging.getLogger(__name__)
_warned: set = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        _log.warning("kanban.%s: %s", KNOB, msg)


def resolve_user(kanban_cfg: Optional[Mapping[str, Any]], *, me: Optional[str] = None) -> Optional[str]:
    """The login local workers' tools run as, or None for the dispatcher's own uid.

    None for: knob absent/empty, a value naming the dispatcher's own login or uid, or a malformed
    value (logged once; a typo must never strand every local card).
    """
    raw = (kanban_cfg or {}).get(KNOB) if isinstance(kanban_cfg, Mapping) else None
    if raw is None or isinstance(raw, bool):
        return None
    value = str(raw).strip()
    if not value:
        return None
    if me is None:
        try:
            me = getpass.getuser()
        except Exception:
            me = ""
    if value == me:
        return None
    if value.isdigit():
        import os
        if int(value) == os.getuid():
            return None
        try:
            import pwd
            value = pwd.getpwuid(int(value)).pw_name
        except (KeyError, ImportError):
            _warn_once(f"uid {raw!r} has no passwd entry; local workers keep the dispatcher uid")
            return None
        if value == me:
            return None
    if not _LOGIN_RE.fullmatch(value):
        _warn_once(f"{raw!r} is not a login name; local workers keep the dispatcher uid")
        return None
    return value


def configured_user() -> Optional[str]:
    """``resolve_user`` over the live ROOT config (per tick)."""
    try:
        from hermes_cli.config import load_config_readonly
        kcfg = (load_config_readonly() or {}).get("kanban")
    except Exception:
        return None
    return resolve_user(kcfg if isinstance(kcfg, Mapping) else None)


def local_host(user: str) -> PoolHost:
    """The synthetic placement for a local worker whose tools run as ``user``."""
    return PoolHost(
        name=LOCAL_HOST_NAME, ssh_host=LOCAL_SSH_HOST, ssh_user=user, slots=0,
        capacity_pct=1.0, absence="required", profiles=(), state="active",
        enabled=True, priority=0,
    )
