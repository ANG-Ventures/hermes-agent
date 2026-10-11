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

``host_affinity: <fleet host>`` (any value but ``studio``) means the opposite
kind of binding (card t_9eeb9c40): the work TARGETS that host over ssh (ACE-AI's
GPUs, its Bambuddy), so the card may only run where the reach matrix proves a
route to it (:func:`manifest_targets` feeds ``reach_allowed_hosts``). It is
never a pin: the target host's own pool worker usually cannot do the work
(kanbanw has no GPU access under the I-4 device lock).

This module answers ``{skill: why}`` for a card's skills, resolved against the
ASSIGNEE profile's skill dirs (same roots as the worker). Lookups are cached
for ``CACHE_TTL_SECONDS``: placement runs every tick and manifests change
rarely. Everything fails OPEN (no binding): the pool sidecar's
``studio_bound_skills`` list and the card-body signals are the backstops.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

STUDIO = "studio"
CACHE_TTL_SECONDS = 600.0
# Pool hosts run Linux; a skill whose platforms omit it cannot run there.
_POOL_PLATFORMS = ("linux",)

_dirs_cache: Dict[str, Tuple[float, Tuple[Path, ...]]] = {}
_index_cache: Dict[Tuple[Path, ...], Tuple[float, Dict[str, Tuple[Path, ...]]]] = {}
_skill_cache: Dict[Tuple[Tuple[Path, ...], str],
                   Tuple[float, Tuple[Optional[str], Optional[str]]]] = {}


_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _build_index(dirs: Iterable[Path]) -> Dict[str, Tuple[Path, ...]]:
    """``{skill name: every SKILL.md that resolves it}`` across ``dirs``, the
    way the worker resolves (``kanban_skill_resolve.skill_resolves``): by
    directory name at any depth and by frontmatter ``name:``.

    One walk per skill-dir set, cached for ``CACHE_TTL_SECONDS``. All roots
    contribute: a binding declared by ANY resolvable copy must count,
    whichever root the worker would load first (Prism e16f8f5c on #1877).
    """
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter

    index: Dict[str, list] = {}
    for d in dirs:
        for skill_md in iter_skill_index_files(d, "SKILL.md"):
            names = {skill_md.parent.name}
            try:
                fm, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                fm = {}
            fm_name = fm.get("name") if isinstance(fm, Mapping) else None
            if isinstance(fm_name, str) and fm_name:
                names.add(fm_name)
            for n in names:
                lst = index.setdefault(n, [])
                if skill_md not in lst:
                    lst.append(skill_md)
    return {k: tuple(v) for k, v in index.items()}


def _index_for(dirs: Tuple[Path, ...], now: float) -> Optional[Dict[str, Tuple[Path, ...]]]:
    hit = _index_cache.get(dirs)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    try:
        index = _build_index(dirs)
    except Exception:
        # Fail open for THIS call only: a failed walk is never cached as
        # "no skills", or a bound skill could spill for a whole TTL
        # (Prism 7cfa01ca / 94f5d2a0 on #1877).
        _log.debug("kanban host affinity: skill index failed open", exc_info=True)
        return None
    _index_cache[dirs] = (now, index)
    return index


def _find_skill_mds(name: str, dirs: Tuple[Path, ...], now: float) -> Optional[Tuple[Path, ...]]:
    """Every ``SKILL.md`` resolving ``name``; ``None`` when the index could not
    be built. The name is matched LITERALLY (never a glob): a card skill
    like ``browser-acces?`` must not borrow another skill's manifest."""
    if not _SKILL_NAME_RE.fullmatch(name or ""):
        return ()
    index = _index_for(dirs, now)
    if index is None:
        return None
    return index.get(name, ())


def _affinity_value(frontmatter: Mapping) -> Optional[str]:
    """``metadata.hermes.host_affinity`` lower-cased, or None."""
    meta = frontmatter.get("metadata")
    hermes = meta.get("hermes") if isinstance(meta, Mapping) else None
    affinity = hermes.get("host_affinity") if isinstance(hermes, Mapping) else None
    if not isinstance(affinity, str):
        return None
    return affinity.strip().lower() or None


def manifest_target(frontmatter: Mapping) -> Optional[str]:
    """The fleet host a parsed SKILL.md says its work TARGETS (``host_affinity``
    naming anything but ``studio``), or None."""
    if not isinstance(frontmatter, Mapping):
        return None
    affinity = _affinity_value(frontmatter)
    return affinity if affinity and affinity != STUDIO else None


def manifest_reason(frontmatter: Mapping) -> Optional[str]:
    """Why a parsed SKILL.md frontmatter binds its skill to the Studio, or None."""
    if not isinstance(frontmatter, Mapping):
        return None
    if _affinity_value(frontmatter) == STUDIO:
        return "manifest:host_affinity"
    platforms = frontmatter.get("platforms")
    if isinstance(platforms, str):
        platforms = [platforms]
    if isinstance(platforms, list) and platforms:
        names = {str(p).strip().lower() for p in platforms}
        if not names & set(_POOL_PLATFORMS):
            return "manifest:platforms=" + ",".join(sorted(names))
    return None


def _skill_binding(name: str, dirs: Tuple[Path, ...],
                   now: float) -> Tuple[Optional[str], Optional[str]]:
    """``(studio_reason, target_host)`` for ``name``. Only a COMPLETED lookup
    is cached: on any exception the call fails open and the cache is left
    alone, so the next tick re-reads (Prism 7cfa01ca / 94f5d2a0 on #1877)."""
    key = (dirs, name)
    hit = _skill_cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    try:
        mds = _find_skill_mds(name, dirs, now)
        if mds is None:
            return None, None
        from agent.skill_utils import parse_frontmatter

        reason: Optional[str] = None
        target: Optional[str] = None
        for md in mds:
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8-sig", errors="replace"))
            reason = reason or manifest_reason(fm)
            target = target or manifest_target(fm)
    except Exception:
        _log.debug("kanban host affinity: %s failed open (not cached)", name, exc_info=True)
        return None, None
    _skill_cache[key] = (now, (reason, target))
    return reason, target


def _profile_dirs(assignee: str, now: float) -> Optional[Tuple[Path, ...]]:
    """The assignee profile's skill roots; ``None`` when they could not be
    read this call (never cached, same rule as :func:`_skill_binding`)."""
    hit = _dirs_cache.get(assignee)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    try:
        from hermes_cli.kanban_skill_resolve import profile_skill_dirs
        from hermes_cli.profiles import get_profile_dir

        home = Path(get_profile_dir(assignee))
        dirs: Tuple[Path, ...] = tuple(profile_skill_dirs(home)) if home.is_dir() else ()
    except Exception:
        _log.debug("kanban host affinity: dirs for %s failed open (not cached)", assignee,
                   exc_info=True)
        return None
    _dirs_cache[assignee] = (now, dirs)
    return dirs


def _bindings(skills: Iterable[str], assignee: Optional[str],
              now: Optional[float]) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    names = [s for s in dict.fromkeys(skills or ()) if isinstance(s, str) and s and ":" not in s
             and "/" not in s]
    if not names or not assignee:
        return {}
    now = time.monotonic() if now is None else now
    dirs = _profile_dirs(assignee, now)
    if not dirs:
        return {}
    return {name: _skill_binding(name, dirs, now) for name in names}


def manifest_affinity(skills: Iterable[str], assignee: Optional[str], *,
                      now: Optional[float] = None) -> Dict[str, str]:
    """``{skill: why}`` for the card skills whose manifest binds them to the Studio."""
    return {n: b[0] for n, b in _bindings(skills, assignee, now).items() if b[0]}


def manifest_targets(skills: Iterable[str], assignee: Optional[str], *,
                     now: Optional[float] = None) -> Dict[str, str]:
    """``{skill: host}`` for the card skills whose manifest says the work
    TARGETS a fleet host (``host_affinity: <host>``, card t_9eeb9c40)."""
    return {n: b[1] for n, b in _bindings(skills, assignee, now).items() if b[1]}


def manifest_targets_safe(skills, assignee) -> Dict[str, str]:
    """:func:`manifest_targets` that can never raise into the dispatch tick."""
    try:
        return manifest_targets(skills, assignee)
    except Exception:
        _log.debug("kanban host affinity: manifest target lookup failed open", exc_info=True)
        return {}


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
    known = tuple(cfg.hosts or ()) or _kwp.known_host_ids(_kb.kanban_home() / "fleet")
    for r in rows:
        # Same pin rule as the planned path: only a REAL pin (any/studio/a known
        # host) is the operator's call; a prose ``host: example.com`` line is not.
        pin, _ignored = _kwp.resolve_pin(r["body"], known)
        if pin is not None:
            continue
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


def _last_payloads(conn, task_ids, kind: str) -> Dict[str, dict]:
    """``{task_id: latest payload}`` of ``kind`` for the given cards, one query."""
    ids = [t for t in dict.fromkeys(task_ids) if t]
    out: Dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for tid, payload in conn.execute(
                f"SELECT task_id, payload FROM task_events e WHERE kind = ? AND task_id IN ({marks}) "
                "AND id = (SELECT MAX(id) FROM task_events WHERE task_id = e.task_id AND kind = ?)",
                [kind, *chunk, kind]):
            out[tid] = json.loads(payload or "{}")
    return out


CLEARED = {"cleared": True}


def record_affinity_events(conn, result, evaluated_ids: Iterable[str] = (),
                           wait_cleared_ids: Iterable[str] = ()) -> None:
    """Persist the placement history of every ready card evaluated this tick.

    ``placement_affinity`` is written on each CHANGE of binding, including
    the change to no binding: a card whose last recorded binding is live but
    which is no longer bound gets ``{"cleared": true}``, so re-binding later
    is a new transition (Prism d3ef126d on #1877). ``placement_wait`` is
    written on each change of wait reason; a bound card that was evaluated
    with no wait this tick (``wait_cleared_ids``: it spawned, or capacity
    was there and something else deferred it) gets ``{"cleared": true}``,
    so the next identical wait is a new episode (Prism 1ccf2233 / 5a51e524).
    A ``claimed`` event after the last wait also ends it. Best-effort: never
    breaks a tick.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    affinity = result.placement_affinity or {}
    waits = result.placement_waits or {}
    evaluated = list(dict.fromkeys([*evaluated_ids, *affinity.keys()]))
    if not evaluated:
        return
    try:
        with _kbc.write_txn(conn):
            last_aff = _last_payloads(conn, evaluated, "placement_affinity")
            cleared_wait: set = set()
            for tid in evaluated:
                aff = affinity.get(tid)
                last = last_aff.get(tid)
                if aff is not None:
                    if last != aff:
                        _kb._append_event(conn, tid, "placement_affinity", dict(aff))
                elif last is not None and not last.get("cleared"):
                    _kb._append_event(conn, tid, "placement_affinity", dict(CLEARED))
                    # The binding's wait ends with it, or the same wait after a
                    # re-bind would read as a duplicate (Prism 0f21fb8c).
                    cleared_wait.add(tid)
            cleared = set(wait_cleared_ids)
            for tid in cleared_wait:
                lw = _last_payloads(conn, [tid], "placement_wait").get(tid)
                if lw is not None and not lw.get("cleared"):
                    _kb._append_event(conn, tid, "placement_wait", dict(CLEARED))
            for tid, aff in affinity.items():
                reason = waits.get(tid)
                last = conn.execute(
                    "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? "
                    "ORDER BY id DESC LIMIT 1", (tid, "placement_wait")).fetchone()
                last_payload = None if last is None else json.loads(last[1] or "{}")
                if reason is None:
                    if tid in cleared and last_payload is not None and not last_payload.get("cleared"):
                        _kb._append_event(conn, tid, "placement_wait", dict(CLEARED))
                    continue
                payload = {"host": aff["host"], "reason": reason, "matched_by": aff["matched_by"]}
                claimed_since = last is not None and conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claimed' AND id > ? "
                    "LIMIT 1", (tid, last[0])).fetchone() is not None
                if last is None or claimed_since or last_payload != payload:
                    _kb._append_event(conn, tid, "placement_wait", payload)
    except Exception:
        _log.warning("kanban dispatch: could not record placement affinity events", exc_info=True)


def clear_cache() -> None:
    _dirs_cache.clear()
    _index_cache.clear()
    _skill_cache.clear()
