"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the checkout is updated underneath it (a manual ``git pull``, or the
window before ``hermes update``'s graceful restart fires), a first-time lazy
import on a new code path can resolve a freshly-pulled consumer module against a
stale cached dependency -> ImportError (see
``tests/test_stale_utils_module_import.py`` for the exact failure).

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. ``/model`` switching) can refuse with a clear "restart the
gateway" message instead of crashing on a cryptic import error.

If the revision can't be read (non-git install, IO error), the boot snapshot
stays ``None`` and skew detection no-ops — it never produces a false positive.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None
# Modules imported by gateway.boot_preload (None = preload never ran).
_preloaded_modules: int | None = None
_skew_logged = False


def record_preload(modules: int) -> None:
    """Record how many first-party modules the boot preload pinned in memory."""
    global _preloaded_modules
    _preloaded_modules = modules


def _fingerprint() -> str | None:
    """Current checkout fingerprint, reusing the CLI's git-rev reader.

    ``hermes_cli.main`` is always already imported in a gateway process (it's
    the entry point), so this import is free and avoids duplicating the
    worktree-aware ref resolution.
    """
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def detect_code_skew() -> tuple[str, str] | None:
    """Return ``(boot_rev, disk_rev)`` short labels if the checkout drifted
    since boot, else ``None``."""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    # Same commit reached through another ref is not skew: compare the SHA part.
    boot_sha, cur_sha = _boot_fingerprint.rsplit(":", 1)[-1], current.rsplit(":", 1)[-1]
    if cur_sha and cur_sha != "unresolved" and cur_sha == boot_sha:
        return None
    skew = _short(_boot_fingerprint), _short(current)
    _log_skew_once(*skew)
    return skew


def _log_skew_once(boot: str, disk: str) -> None:
    """One grep-able line per process: was the in-memory snapshot preloaded?"""
    global _skew_logged
    if _skew_logged:
        return
    _skew_logged = True
    modules = "none" if _preloaded_modules is None else _preloaded_modules
    logger.warning("PHASE=code_skew_preloaded modules=%s boot=%s disk=%s", modules, boot, disk)
