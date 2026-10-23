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

import logging
import os
import pwd  # windows-footgun: ok — POSIX-only module; its one importer (kanban_db_dispatch) gates on os.name == "posix"
import re
from typing import Any, Mapping, Optional

from hermes_cli.kanban_worker_hosts import LOCAL_HOST_NAME
from hermes_cli.kanban_worker_pool import PoolHost

KNOB = "local_worker_uid"
LOCAL_SSH_HOST = "localhost"

# A POSIX login, the hidden ``_name`` form included. Anything else is refused, not guessed.
_LOGIN_RE = re.compile(r"_?[a-z][a-z0-9_-]{0,30}")
_log = logging.getLogger(__name__)


class LocalWorkerUidError(RuntimeError):
    """The knob is set but unusable: the spawn fails instead of silently running as the dispatcher."""


def _my_login() -> str:
    # The process uid, never LOGNAME/USER (a launcher can set those to anything).
    return pwd.getpwuid(os.getuid()).pw_name  # windows-footgun: ok — POSIX-only module; its one importer (kanban_db_dispatch) gates on os.name == "posix"


def resolve_user(kanban_cfg: Optional[Mapping[str, Any]], *, me: Optional[str] = None) -> Optional[str]:
    """The login local workers' tools run as, or None for the dispatcher's own uid.

    None ONLY for an explicitly unset knob (absent / null / empty) or a value naming the
    dispatcher's own uid. A set-but-unusable value (malformed, unknown uid/login) raises
    ``LocalWorkerUidError``: this knob is a privilege boundary, so a typo must fail closed.
    """
    if kanban_cfg is not None and not isinstance(kanban_cfg, Mapping):
        raise LocalWorkerUidError(f"kanban.{KNOB}: the kanban section is a {type(kanban_cfg).__name__}, not a mapping")
    raw = (kanban_cfg or {}).get(KNOB)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise LocalWorkerUidError(f"kanban.{KNOB}: {raw!r} is not a login or uid")
    value = str(raw).strip()
    if me is None:
        me = _my_login()
    if value.isdigit():
        if int(value) == os.getuid():  # windows-footgun: ok — POSIX-only module; its one importer (kanban_db_dispatch) gates on os.name == "posix"
            return None
        try:
            value = pwd.getpwuid(int(value)).pw_name
        except KeyError:
            raise LocalWorkerUidError(f"kanban.{KNOB}: uid {value} has no passwd entry") from None
    elif value == me:
        return None
    elif not _LOGIN_RE.fullmatch(value):
        raise LocalWorkerUidError(f"kanban.{KNOB}: {raw!r} is not a login name")
    else:
        try:
            pwd.getpwnam(value)
        except KeyError:
            raise LocalWorkerUidError(f"kanban.{KNOB}: login {value!r} does not exist") from None
    if value == me:
        return None
    return value


def configured_user() -> Optional[str]:
    """``resolve_user`` over the live ROOT config (per spawn). A config that cannot be read
    raises: the knob's state is unknown, and unknown must not mean "dispatcher uid"."""
    try:
        from hermes_cli.config import load_config_readonly
        kcfg = (load_config_readonly() or {}).get("kanban")
    except Exception as exc:
        raise LocalWorkerUidError(f"kanban.{KNOB}: config unreadable: {exc}") from exc
    if kcfg is not None and not isinstance(kcfg, Mapping):
        raise LocalWorkerUidError(f"kanban.{KNOB}: the kanban section is a {type(kcfg).__name__}, not a mapping")
    return resolve_user(kcfg)


def local_host(user: str) -> PoolHost:
    """The synthetic placement for a local worker whose tools run as ``user``."""
    return PoolHost(
        name=LOCAL_HOST_NAME, ssh_host=LOCAL_SSH_HOST, ssh_user=user, slots=0,
        capacity_pct=1.0, absence="required", profiles=(), state="active",
        enabled=True, priority=0,
    )
