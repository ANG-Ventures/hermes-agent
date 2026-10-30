"""``access-bound-pool-pin`` on the REAL create body (t_998bce52).

hermes-home's pre_tool_call hook ``hooks/access_bound_create_policy.py`` decides this rule from
the command TEXT, so a body built at run time (``--body "$S"`` from python/sqlite, a
``--body-file`` written by ``sed``/``{ ...; } > F`` in the same shell, ``$TMPDIR/x.md``) is
unreadable there. ``hermes kanban create`` holds the final body, so it runs the same check.

One encoding: the hook module itself is loaded from the fleet root and its ``pool_hosts()`` /
``verdict()`` decide, so the refusal text and rule name are the hook's. Fail OPEN, as the hook
does: no hook module, a load error, an unreadable pool file, or a raising verdict all allow the
create (a placement guard, not a security boundary). The hook's off-marker
``state/access-bound-create-policy-off`` disarms this check too.
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
