"""Who may WRITE or REMOVE a gateway service definition (systemd unit, launchd plist).

INVARIANT (t_8749a807): a service definition for gateway G may be written or removed only by a process
whose resolved HERMES_HOME equals the home G's definition pins (or, for a fresh install, the home the
caller is installing, which must be admitted: inside the account's Hermes tree, a bare-name owner, or
``--force-unit-path``). A kanban worker (``HERMES_GATEWAY_INSTALL_DISABLED=1``) may do neither.

ONE chokepoint enforces it: :func:`assert_may_mutate`. Every writer and every remover (``systemd_install``,
``launchd_install``, ``refresh_*_if_needed``, the ``--replace`` drop-in retirement, ``*_uninstall``, the
launchd ``start`` self-heal, ``remove_legacy_hermes_units``, and both migration paths) calls it FIRST,
before any filesystem or service-manager side effect, and before anything adopts the unit's own home into
``os.environ``. ``tests/hermes_cli/test_gateway_service_owner.py`` proves by AST walk that no
``write_text``/``unlink``/``disable``/``bootout`` of a definition in ``gateway*.py`` is reached without it.

Why: on 2026-10-04 a scratch E2E gateway (``HERMES_HOME=/srv/ci/scratch/...``) started by a kanban worker
resolved to the bare ``hermes-gateway`` name on ACE-AI, and its on-boot refresh rewrote the host's real unit
(WorkingDirectory and HERMES_HOME pointed at the scratch dir; every later restart failed ``200/CHDIR``).
The name decided who wrote the file; now the file's pinned home does.
"""

from __future__ import annotations

import contextlib
import io
import os
import plistlib
from pathlib import Path

from hermes_cli.gateway_unit_parse import pinned_hermes_home

# Set by the kanban dispatcher on every worker it spawns: a worker never writes the host's gateway
# service definitions, whatever its HERMES_HOME (a scratch E2E home included).
INSTALL_DISABLED_ENV = "HERMES_GATEWAY_INSTALL_DISABLED"


class ServiceMutationRefused(SystemExit):
    """A gateway service-definition write/remove the invariant forbids. ``SystemExit(1)`` so every CLI path
    that reaches a writer exits non-zero (a refusal that returned normally went on to START the other home's
    service); callers that must survive it (setup wizard, migration) catch it by name."""

    def __init__(self, reason: str):
        super().__init__(1)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


def _kill_switch_set() -> bool:
    return os.environ.get(INSTALL_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes")


def service_writes_disabled(action: str) -> bool:
    """True (with the refusal printed) when this process may not touch gateway service definitions."""
    if not _kill_switch_set():
        return False
    print(f"✗ Refusing to {action}: {INSTALL_DISABLED_ENV}=1 (set for kanban workers).")
    print("  Gateway services are installed by an operator from a normal shell, never from a worker.")
    return True


def pinned_home(definition_path: Path) -> str | None:
    """``HERMES_HOME`` pinned by the systemd unit or launchd plist at *definition_path*, or None."""
    if definition_path.suffix == ".plist":
        try:
            with definition_path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception:  # unreadable / not a plist (hand-edited, truncated): pins nobody
            return None
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        value = env.get("HERMES_HOME") if isinstance(env, dict) else None
        return str(value).strip() or None if value is not None else None
    return pinned_hermes_home(definition_path)


def _resolve(raw: str | Path) -> Path | None:
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def caller_home() -> Path:
    """The home THIS process acts for: its explicit ``HERMES_HOME`` / profile override, else its native default.

    Read before anything adopts a unit's own home into ``os.environ`` (``_sync_hermes_home_from_systemd_unit``):
    an ownership check that ran after that adoption compared the unit with itself.
    """
    from hermes_cli import gateway as gw
    return Path(gw.get_hermes_home())


def foreign_pinned_home(definition_path: Path, home: Path) -> str | None:
    """The ``HERMES_HOME`` the definition at *definition_path* pins when that is not *home*, else None.

    A definition with no pinned home (hand-written, pre-pinning) is not claimed by anyone else.
    """
    raw = pinned_home(definition_path)
    if raw is None or _resolve(raw) == _resolve(home):
        return None
    return raw


def definition_belongs_to_home(definition_path: Path, home: Path, action: str) -> bool:
    """False (with a warning) when the existing definition pins a ``HERMES_HOME`` other than *home*."""
    raw = foreign_pinned_home(definition_path, home)
    if raw is None:
        return True
    print(f"✗ Refusing to {action} {definition_path}: it runs HERMES_HOME={raw}, "
          f"but this process has HERMES_HOME={home}.")
    print("  That file is another install's gateway. Use that install, or pass --force-unit-path")
    print("  to `hermes gateway install` if you really mean to repoint it at this home.")
    return False


def _installed_definition_pins(home: Path) -> bool:
    """True when a gateway definition already installed at this home's service path pins *home* itself.

    Mere existence is not ownership: a same-named definition can belong to another home (the collision
    these guards exist for), so only a definition whose pinned ``HERMES_HOME`` is *home* counts.
    """
    from hermes_cli import gateway as gw

    paths = [gw.get_systemd_unit_path(system=False), gw.get_systemd_unit_path(system=True)]
    if gw.is_macos():
        paths.append(gw.get_launchd_plist_path())
    for path in paths:
        raw = pinned_home(path) if path.exists() else None
        if raw is not None and _resolve(raw) == home:
            return True
    return False


def home_may_install_service(home: Path) -> bool:
    """True for a home that may own a host gateway service: the account's Hermes tree (default root and
    ``profiles/<name>``), any bare-name owner (native default, sudo invoker's, the home the installed bare
    unit pins), or a home whose installed definition already pins it (a re-install of its own unit)."""
    from hermes_cli import gateway as gw

    resolved = _resolve(home)
    if resolved is None:
        return False
    # Platform-native default root(s): ~/.hermes, plus the sudo invoker's under sudo.
    if any(resolved == root or root in resolved.parents for root in gw._native_service_homes()):
        return True
    return gw._home_owns_bare_service_name(resolved) or _installed_definition_pins(resolved)


def refuse_foreign_home_install(home: Path, force_unit_path: bool) -> bool:
    """True (with guidance printed) when ``gateway install`` must not register a service for *home*."""
    if force_unit_path or home_may_install_service(home):
        return False
    print(f"✗ Refusing to install a gateway service for HERMES_HOME={home}.")
    print("  It is outside this account's Hermes tree and is not a registered profile, so it looks like")
    print("  a scratch / test / E2E home. A service installed for it would outlive the run and restart a")
    print("  gateway on this host forever. Run that gateway in the foreground (`hermes gateway run`),")
    print("  or pass --force-unit-path if this custom home really is meant to own a host service.")
    return True


def mutation_blocker(
    definition_path: Path, action: str, home: Path, *, install: bool = False, force_unit_path: bool = False,
    admit_home: Path | None = None, quiet: bool = False,
) -> str | None:
    """The reason the invariant forbids *action* on *definition_path* for *home*, or None when it is allowed.

    * kill switch (``HERMES_GATEWAY_INSTALL_DISABLED``): everything is refused, writes and removals alike;
    * an existing definition pinning another home than *home* (the home the definition will carry):
      refused unless *force_unit_path* (an explicit repoint);
    * *install* (a definition will be created or repointed): *admit_home* (default *home*; for a
      ``--system --run-as-user`` install the CALLER's home, since the pinned one is remapped to the service
      user) must be admitted (:func:`home_may_install_service`) or *force_unit_path* given.
    A definition that does not exist, or pins no home, is nobody else's. Pure: no side effects.
    """
    if _kill_switch_set():
        if not quiet:
            service_writes_disabled(action)
        return f"{INSTALL_DISABLED_ENV}=1 (kanban worker): may not {action}"
    # Decided through definition_belongs_to_home / home_may_install_service (the test seams), not around them.
    sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    with sink:
        if not force_unit_path and definition_path.exists() and not definition_belongs_to_home(
                definition_path, home, action):
            return (f"{definition_path} runs HERMES_HOME={pinned_home(definition_path)}, "
                    f"this process has HERMES_HOME={home}")
        admit = home if admit_home is None else admit_home
        if install and not force_unit_path and refuse_foreign_home_install(admit, False):
            return f"HERMES_HOME={admit} is outside this account's Hermes tree and not a registered profile"
    return None


def assert_may_mutate(
    definition_path: Path, action: str, home: Path | None = None, *, install: bool = False,
    force_unit_path: bool = False, admit_home: Path | None = None,
) -> Path:
    """THE chokepoint. Raise :class:`ServiceMutationRefused` unless *home* (default: :func:`caller_home`)
    may perform *action* on the service definition at *definition_path*. Returns the home it checked, so a
    writer keeps using the caller's home rather than one a later sync adopts. No side effects."""
    home = caller_home() if home is None else home
    reason = mutation_blocker(definition_path, action, home, install=install, force_unit_path=force_unit_path,
                              admit_home=admit_home)
    if reason is not None:
        raise ServiceMutationRefused(reason)
    return home
