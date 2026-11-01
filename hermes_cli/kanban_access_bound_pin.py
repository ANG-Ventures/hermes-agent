"""``access-bound-pool-pin`` on the REAL create body (t_998bce52).

hermes-home's pre_tool_call hook ``hooks/access_bound_create_policy.py`` decides this rule from
the command TEXT, so a body built at run time (``--body "$S"`` from python/sqlite, a
``--body-file`` written by ``sed``/``{ ...; } > F`` in the same shell, ``$TMPDIR/x.md``) is
unreadable there. ``hermes kanban create`` holds the final body, so it runs the same check.

One encoding: the hook module itself is loaded from the fleet root and its ``pool_hosts()`` /
``verdict()`` decide, so the refusal text and rule name are the hook's. Fail OPEN, as the hook
does: no hook module, a load error, an unreadable pool file, or a raising verdict all allow the
create (a placement guard, not a security boundary). The hook's off-marker
``state/access-bound-create-policy-off`` disarms the pool-pin check only; like the hook, the handoff rule has no
off-switch (the ledgered AEGIS_HANDOFF_APOLLO quote is the override).
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

OFF_MARKER = Path("state") / "access-bound-create-policy-off"
# The installed copy is what the hook gate runs; the tracked copy covers a root with no install.
HOOK_CANDIDATES = (
    Path("hooks-installed") / "current" / "hooks" / "access_bound_create_policy.py",
    Path("hooks") / "access_bound_create_policy.py",
)
_MODULE_NAME = "_hermes_access_bound_create_policy"


def _fleet_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def _load_hook(root: Path):
    for rel in HOOK_CANDIDATES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            # The hook prepends its own dir to sys.path to import its sibling access_bound;
            # undo that so hooks/*.py never shadows a module for the rest of this process.
            saved_path, had_sibling = list(sys.path), "access_bound" in sys.modules
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.path[:] = saved_path
                if not had_sibling:
                    sys.modules.pop("access_bound", None)
            return mod
        except Exception:
            _log.debug("access-bound pin: cannot load %s; failing open", path, exc_info=True)
            return None
    return None


def refusal(body: Optional[str]) -> Optional[str]:
    """The hook's refusal text for this create body, or None (allowed / fail open)."""
    if not body:
        return None
    root = _fleet_root()
    if (root / OFF_MARKER).exists():
        return None
    mod = _load_hook(root)
    if mod is None:
        return None
    try:
        return mod.verdict(body, mod.pool_hosts(root))
    except Exception:
        _log.debug("access-bound pin: verdict raised; failing open", exc_info=True)
        return None


# ── aegis-no-apollo-handoff on the REAL parsed assignee (t_d26f1385) ──────────────────────────────
# The hook decides from the command text, so `A=human; B=apollo; ... "$A:$B"` or an assignee a
# script computes reaches the board unread. create/assign/reassign hold the parsed value; the hook
# module's own constants and handoff_refusal() text decide. Fail open like refusal() above.
HANDOFF_LEDGER = Path("state") / "aegis-apollo-handoffs.jsonl"


def _acting_profiles() -> set:
    """Every identity source's profile name for this session. The rule applies when ANY says aegis:
    ``current_profile_name()`` (override / env pin / home), the raw ``HERMES_PROFILE_NAME`` /
    ``HERMES_PROFILE`` pins, the resolved ``HERMES_HOME``, and the home the active secret scope
    is stamped with (the routed profile on an in-process gateway /kanban). A caller-set env pin can
    only ADD the rule: ``HERMES_PROFILE=default`` under ``profiles/aegis`` still resolves aegis."""
    import os
    from hermes_cli.profiles import current_profile_name
    from hermes_constants import get_hermes_home, profile_name_for_home
    names = {current_profile_name(default="default") or "default"}
    for env_name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        names.add(os.environ.get(env_name) or "")
    names.add(profile_name_for_home(get_hermes_home()) or "")
    try:
        from agent.secret_scope import current_secret_scope_home
        names.add(profile_name_for_home(current_secret_scope_home()) or "")
    except ImportError:
        pass
    return {str(n).strip().lower() for n in names if n and str(n).strip()}


def _handoff_quote(env_name: str) -> Optional[str]:
    """The ``AEGIS_HANDOFF_APOLLO`` quote for the ACTING profile. With a profile secret scope bound
    (a gateway turn, incl. the in-process /kanban path) only that scope's mapping counts: the
    process ``os.environ`` is the launch profile's. No scope (plain CLI): ``get_secret`` reads
    ``os.environ``; an unscoped multiplexed read counts as no quote."""
    try:
        from agent.secret_scope import UnscopedSecretError, current_secret_scope, get_secret
    except ImportError:
        import os
        return os.environ.get(env_name)
    scope = current_secret_scope()
    if scope is not None:
        return scope.get(env_name)
    try:
        return get_secret(env_name)
    except UnscopedSecretError:
        return None


def _lock_module():
    """``fcntl`` where it exists (every fleet host), else None (Windows)."""
    try:
        import fcntl
    except ImportError:
        return None
    return fcntl


def _append_ledger_line(ledger: Path, line: str) -> None:
    """Append one whole line or nothing: exclusive lock, one ``os.write`` of the full line, fsync;
    any OSError (short write, ENOSPC) truncates back to the pre-append length and re-raises. The
    rollback is safe only under the lock, so a platform without ``fcntl`` raises before writing."""
    import os
    ledger.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    fcntl = _lock_module()
    if fcntl is None:
        # No flock (Windows): the truncate-on-failure rollback could erase another writer's
        # committed row, so refuse instead; the caller fails the hand-off closed
        # (Prism 57e574655dc8, 1671fc3d2bcc). No fleet host runs this CLI on Windows.
        raise OSError(f"no exclusive file lock on this platform; refusing to append to {ledger}")
    fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        length = os.fstat(fd).st_size
        try:
            written = os.write(fd, data)
            if written != len(data):
                raise OSError(f"short write to {ledger}: {written}/{len(data)} bytes")
            os.fsync(fd)
        except OSError:
            try:
                os.ftruncate(fd, length)
                os.fsync(fd)
            except OSError:
                _log.warning("handoff ledger: cannot roll %s back to %d bytes", ledger, length)
            raise
    finally:
        os.close(fd)  # releases the flock


def _ledger_root() -> Path:
    """Where the handoff ledger lives: the KANBAN home, so a sandboxed board (HERMES_KANBAN_SANDBOX /
    HERMES_KANBAN_HOME) ledgers into its own tree, never the live fleet's (Prism 6b5a28f52a8c).
    The hook module itself still loads from the fleet root: policy is the live one."""
    from hermes_cli import kanban_db
    return kanban_db.kanban_home()


def handoff_refusal(verb: str, card: Optional[str], assignee: Optional[str]) -> Optional[str]:
    """The hook's refusal text when an aegis session hands *card* to an Apollo alias without a
    ledgered ``AEGIS_HANDOFF_APOLLO`` quote, else None (an allowed override is ledgered first)."""
    return handoff_check(verb, card, assignee)[0]


def handoff_check(verb: str, card: Optional[str], assignee: Optional[str], *,
                  title: Optional[str] = None) -> tuple:
    """``(refusal, row_id)``. ``row_id`` names the ledgered override row (None when nothing was
    ledgered). For ``create`` the card does not exist yet: pass ``card=None`` + ``title``, then
    :func:`bind_created` once the id is known (Prism 92795be78ffb: the card field never holds a title)."""
    import json
    import os
    import uuid
    from datetime import datetime, timezone

    if not assignee:
        return None, None
    try:
        profiles = _acting_profiles()
    except Exception:
        _log.debug("handoff: cannot resolve the acting profile; failing open", exc_info=True)
        return None, None
    root = _fleet_root()
    # No OFF_MARKER check here: the hook's aegis-no-apollo-handoff rule has no off-switch of its own (the
    # ledgered Ace quote is the only override), and the CLI mirrors that. The marker disarms the pool pin only.
    mod = _load_hook(root)
    if mod is None:
        return None, None
    try:
        profile = str(mod.HANDOFF_PROFILE).lower()
        if profile not in profiles:
            return None, None
        target = str(assignee).strip().lower()
        if target not in {str(a).lower() for a in mod.APOLLO_ASSIGNEES}:
            return None, None
        env_name, minimum = str(mod.HANDOFF_ENV), int(mod.HANDOFF_MIN)
        offered = _handoff_quote(env_name)
        quote = (offered or "").strip()
        if len(quote) < minimum:
            return mod.handoff_refusal(verb, target, offered is not None), None
        rule = str(getattr(mod, "HANDOFF_RULE", "aegis-no-apollo-handoff"))
    except Exception:
        _log.debug("handoff: hook surface unusable; failing open", exc_info=True)
        return None, None
    row_id = uuid.uuid4().hex
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "profile": profile,
           "session_id": os.environ.get("HERMES_SESSION_ID", ""), "tool": "kanban-cli",
           "verb": verb, "card": card, "assignee": target, "quote": quote, "row_id": row_id}
    if title is not None:
        row["title"] = title
    try:
        ledger = _ledger_root() / HANDOFF_LEDGER
    except Exception as exc:  # a sandbox with nowhere isolated to go refuses; so does the handoff
        return (f"BLOCKED by {rule}: no ledger location for this board ({exc}); an unledgered "
                f"hand-off is not allowed."), None
    try:
        _append_ledger_line(ledger, json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return (f"BLOCKED by {rule}: the hand-off quote is valid but the ledger {ledger} could not "
                f"be written, and an unledgered hand-off is not allowed."), None
    return None, row_id


def bind_created(row_id: Optional[str], card: str) -> None:
    """Record the id ``create`` produced for an override ledgered as ``row_id``. Best effort: the
    authorization row is already durable; a failed bind is logged, never un-does the card."""
    import json
    from datetime import datetime, timezone

    if not row_id:
        return
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "verb": "create-bound",
           "row_id": row_id, "card": card, "tool": "kanban-cli"}
    try:
        _append_ledger_line(_ledger_root() / HANDOFF_LEDGER,
                            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        _log.warning("handoff ledger: cannot bind override %s to %s", row_id, card, exc_info=True)
