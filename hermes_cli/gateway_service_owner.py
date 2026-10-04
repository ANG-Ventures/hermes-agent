"""Who may WRITE a gateway service definition (systemd unit, launchd plist).

A service definition pins one ``HERMES_HOME``; that pinned home owns it, whatever the file is named.
The writers (``gateway install``, and the refresh that every ``gateway run``/``start``/``restart``
performs) resolve the target path from the service NAME, and a name can collide: on 2026-10-04 a
scratch E2E gateway (``HERMES_HOME=/srv/ci/scratch/...``) started by a kanban worker resolved to the
bare ``hermes-gateway`` name, and its on-boot refresh rewrote the host's real unit (WorkingDirectory
and HERMES_HOME pointed at the scratch dir; every later restart failed ``200/CHDIR``). The uninstall
path already refused a unit pinning another home; these guards give the writers the same rule, refuse
an install from a home outside the account's Hermes tree, and honour a worker-level kill switch.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

# Set by the kanban dispatcher on every worker it spawns: a worker never writes the host's gateway
# service definitions, whatever its HERMES_HOME (a scratch E2E home included).
INSTALL_DISABLED_ENV = "HERMES_GATEWAY_INSTALL_DISABLED"


def service_writes_disabled(action: str) -> bool:
    """True (with the refusal printed) when this process may not write gateway service definitions."""
    if os.environ.get(INSTALL_DISABLED_ENV, "").strip().lower() not in ("1", "true", "yes"):
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
    from hermes_cli.gateway import _hermes_home_pinned_by_unit
    return _hermes_home_pinned_by_unit(definition_path)


def _resolve(raw: str | Path) -> Path | None:
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def definition_belongs_to_home(definition_path: Path, home: Path, action: str) -> bool:
    """False (with a warning) when the existing definition pins a ``HERMES_HOME`` other than *home*.

    A definition with no pinned home (hand-written, pre-pinning) is not claimed by anyone else.
    """
    raw = pinned_home(definition_path)
    if raw is None or _resolve(raw) == _resolve(home):
        return True
    print(f"✗ Refusing to {action} {definition_path}: it runs HERMES_HOME={raw}, "
          f"but this process has HERMES_HOME={home}.")
    print("  That file is another install's gateway. Use that install, or pass --force-unit-path")
    print("  to `hermes gateway install` if you really mean to repoint it at this home.")
    return False


def home_may_install_service(home: Path) -> bool:
    """True for a home that may own a host gateway service: the account's Hermes tree (default root and
    ``profiles/<name>``), any bare-name owner (native default, sudo invoker's, the home the installed bare
    unit pins), or a home whose service is already installed (a re-install of its own unit)."""
    from hermes_cli import gateway as gw

    resolved = _resolve(home)
    if resolved is None:
        return False
    # Platform-native default root(s): ~/.hermes, plus the sudo invoker's under sudo.
    if any(resolved == root or root in resolved.parents for root in gw._native_service_homes()):
        return True
    return gw._home_owns_bare_service_name(resolved) or gw._is_service_installed()


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
