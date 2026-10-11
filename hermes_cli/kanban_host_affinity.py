"""Skill-manifest host affinity for kanban placement (card t_3e6c9b2b).

Some card work is bound to one host's STATE, not its CPU: the Studio's
signed-in browsers (CDP on loopback :9222-:9224), Messages.app, a macOS GUI
session. A load-based spill to a Linux pool host cannot do that work; the
worker finds out minutes later and blocks (t_79436ebf, 2026-10-10).

The skill that needs the host says so once, in its own SKILL.md, and every
card that loads it inherits the binding:

* ``metadata.hermes.host_affinity: studio`` (explicit), or
* a ``platforms:`` list that excludes ``linux`` (pool hosts are Linux by
  construction: the probe reads ``/proc/loadavg``).

This module answers ``{skill: why}`` for a card's skills, resolved against the
ASSIGNEE profile's skill dirs (same roots as the worker). Lookups are cached
for ``CACHE_TTL_SECONDS``: placement runs every tick and manifests change
rarely. Everything fails OPEN (no binding): the pool sidecar's
``studio_bound_skills`` list and the card-body signals are the backstops.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

STUDIO = "studio"
CACHE_TTL_SECONDS = 600.0
# Pool hosts run Linux; a skill whose platforms omit it cannot run there.
_POOL_PLATFORMS = ("linux",)

_dirs_cache: Dict[str, Tuple[float, Tuple[Path, ...]]] = {}
_skill_cache: Dict[Tuple[Tuple[Path, ...], str], Tuple[float, Optional[str]]] = {}


def _find_skill_md(name: str, dirs: Iterable[Path]) -> Optional[Path]:
    """``<dir>/<name>/SKILL.md`` or one level deeper (category layout)."""
    dirs = list(dirs)
    for d in dirs:
        direct = d / name / "SKILL.md"
        if direct.is_file():
            return direct
    for d in dirs:
        try:
            for hit in d.glob(f"*/{name}/SKILL.md"):
                if not any(part.startswith(".") for part in hit.relative_to(d).parts):
                    return hit
        except OSError:
            continue
    return None


def manifest_reason(frontmatter: Mapping) -> Optional[str]:
    """Why a parsed SKILL.md frontmatter binds its skill to the Studio, or None."""
    if not isinstance(frontmatter, Mapping):
        return None
    meta = frontmatter.get("metadata")
    hermes = meta.get("hermes") if isinstance(meta, Mapping) else None
    affinity = hermes.get("host_affinity") if isinstance(hermes, Mapping) else None
    if isinstance(affinity, str) and affinity.strip().lower() == STUDIO:
        return "manifest:host_affinity"
    platforms = frontmatter.get("platforms")
    if isinstance(platforms, str):
        platforms = [platforms]
    if isinstance(platforms, list) and platforms:
        names = {str(p).strip().lower() for p in platforms}
        if not names & set(_POOL_PLATFORMS):
            return "manifest:platforms=" + ",".join(sorted(names))
    return None


def _skill_reason(name: str, dirs: Tuple[Path, ...], now: float) -> Optional[str]:
    key = (dirs, name)
    hit = _skill_cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    reason: Optional[str] = None
    try:
        md = _find_skill_md(name, dirs)
        if md is not None:
            from agent.skill_utils import parse_frontmatter

            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8-sig", errors="replace"))
            reason = manifest_reason(fm)
    except Exception:
        _log.debug("kanban host affinity: %s failed open", name, exc_info=True)
        reason = None
    _skill_cache[key] = (now, reason)
    return reason


def _profile_dirs(assignee: str, now: float) -> Tuple[Path, ...]:
    hit = _dirs_cache.get(assignee)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    dirs: Tuple[Path, ...] = ()
    try:
        from hermes_cli.kanban_skill_resolve import profile_skill_dirs
        from hermes_cli.profiles import get_profile_dir

        home = Path(get_profile_dir(assignee))
        if home.is_dir():
            dirs = tuple(profile_skill_dirs(home))
    except Exception:
        _log.debug("kanban host affinity: dirs for %s failed open", assignee, exc_info=True)
    _dirs_cache[assignee] = (now, dirs)
    return dirs


def manifest_affinity(skills: Iterable[str], assignee: Optional[str], *,
                      now: Optional[float] = None) -> Dict[str, str]:
    """``{skill: why}`` for the card skills whose manifest binds them to the Studio."""
    names = [s for s in dict.fromkeys(skills or ()) if isinstance(s, str) and s and ":" not in s
             and "/" not in s]
    if not names or not assignee:
        return {}
    now = time.monotonic() if now is None else now
    dirs = _profile_dirs(assignee, now)
    if not dirs:
        return {}
    out: Dict[str, str] = {}
    for name in names:
        why = _skill_reason(name, dirs, now)
        if why:
            out[name] = why
    return out


def manifest_affinity_safe(skills, assignee) -> Dict[str, str]:
    """:func:`manifest_affinity` that can never raise into the dispatch tick."""
    try:
        return manifest_affinity(skills, assignee)
    except Exception:
        _log.debug("kanban host affinity: manifest lookup failed open", exc_info=True)
        return {}


def studio_affinity_unplanned(rows, default_assignee: Optional[str], result) -> None:
    """Dry-run view with no pool plan: which ready cards ARE Studio-bound, so
    ``kanban dispatch --dry-run`` shows the binding before a spill ever happens."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_worker_pool as _kwp
    try:
        cfg = _kwp.read_pool(_kb.kanban_home() / "fleet")
    except Exception:
        cfg = _kwp.PoolConfig()
    for r in rows:
        if _kwp.card_pin(r["body"]) is not None:
            continue  # an explicit pin is the operator's call
        try:
            skills = tuple(s for s in (json.loads(r["skills"] or "[]") or []) if isinstance(s, str))
        except (TypeError, ValueError):
            skills = ()
        assignee = r["assignee"] or default_assignee
        manifest = manifest_affinity_safe(skills, assignee) if skills and assignee else {}
        hit = _kwp.studio_affinity(r["body"], skills, cfg, manifest=manifest)
        if hit is not None:
            result.placement_affinity[r["id"]] = {
                "host": _kwp.STUDIO_HOST, "rule": hit[0], "matched_by": hit[1]}


def _last_payload(conn, task_id: str, kind: str):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()
    return None if row is None else json.loads(row[0] or "{}")


def record_affinity_events(conn, result) -> None:
    """``placement_affinity`` on each change of binding and ``placement_wait`` on
    each change of wait reason, per bound card. Best-effort: never breaks a tick."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    try:
        with _kbc.write_txn(conn):
            for tid, aff in result.placement_affinity.items():
                if _last_payload(conn, tid, "placement_affinity") != aff:
                    _kb._append_event(conn, tid, "placement_affinity", dict(aff))
                reason = (result.placement_waits or {}).get(tid)
                if reason is None:
                    continue
                payload = {"host": aff["host"], "reason": reason, "matched_by": aff["matched_by"]}
                if _last_payload(conn, tid, "placement_wait") != payload:
                    _kb._append_event(conn, tid, "placement_wait", payload)
    except Exception:
        _log.warning("kanban dispatch: could not record placement affinity events", exc_info=True)


def clear_cache() -> None:
    _dirs_cache.clear()
    _skill_cache.clear()
