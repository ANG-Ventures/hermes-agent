"""The one ``placement:`` line ``kanban create`` prints: where the card will run, and which line decided it.

Card t_0a5740f3. Ace 2026-11-11 (Discord 1570197131329937549): kanban workers stay off the Mac Studio
unless truly necessary. Before this the author saw nothing at mint time, and the first sign of a Studio
placement was the placement event after the spawn.

The route is the dispatcher's own (:func:`kanban_db_dispatch._route_row`) for the card as it now stands,
computed from the pool files and the capability map. It does not know this tick's load, slots or probe
results, and it does not know links added later. So the line says where the card goes, not when. Binds are read
dry-run and no placement event is written. The one write a dropped bare ``host: studio`` pin can make is the
dispatcher's own once-per-card notice (``kanban_studio_pin.note_drop``), which the first tick would write anyway.
Never raises: on any error there is no line.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Optional

_log = logging.getLogger(__name__)

_ROW_SQL = ("SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker, priority, "
            "origin_host, workspace_of, branch_name, asap FROM tasks WHERE id = ?")
_HOST_LINE_RE = re.compile(r"^[ \t]*host:[^\n]*$", re.IGNORECASE | re.MULTILINE)
STUDIO = "the Mac Studio"


def _pool_config():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    return kwp.read_pool(kb.kanban_home() / "fleet", kanban_cfg=kbd._root_kanban_cfg())


def _requires_line(body: str, cap: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_host_capabilities as khc
    for m in khc.REQUIRES_RE.finditer(body or ""):
        if cap is None or cap in khc.declared_words(m.group(0)):
            return m.group(0).strip()
    return None


def _host_line(body: str) -> Optional[str]:
    m = _HOST_LINE_RE.search(body or "")
    return m.group(0).strip() if m else None


def _quote(line: Optional[str], fallback: str) -> str:
    return f"`{line}`" if line else fallback


def _decided_by(body: str, rule: Optional[str], studio: Optional[dict]) -> str:
    """The line (quoted) or the rule that decided a Studio route."""
    rule = rule or ""
    rec = studio or {}
    if rec.get("source") == "declared" or rule.startswith("requires:"):
        cap = rec.get("capability") or rule[len("requires:"):] or None
        line = _requires_line(body, cap)
        if line:
            return _quote(line, "")
    if rule == "host_studio":
        return " + ".join(f"`{x}`" for x in (_host_line(body), _requires_line(body, None)) if x) or "rule host_studio"
    if rec.get("source") == "skill":
        return f"skill {rec.get('evidence')} (needs {rec.get('capability')})"
    if rec.get("kind") == "structural":
        return f"rule {rec.get('capability')} ({rec.get('source')}: {rec.get('evidence')})"
    return f"rule {rule or 'local'}"


def _unplanned_route(conn, row, cfg, held: dict, pause_cache: dict) -> "tuple[str, Optional[str], str]":
    """The dispatcher's no-plan branch for one row (``dispatch_once``, ``spillover is None``), in its order:
    a registered remote pin waits ``pool_unavailable`` (an unknown one ``pin_unknown_host``), a carried scratch
    card waits on its carry host, an identity-only capability waits, an enforced continuation origin is resolved,
    then the pause hold and the worktree operator pins. ``(route, arg, rule)``; ``local`` only when none applies.
    Read-only: the dispatcher's ``note_ignored_pin`` notice is not written."""
    from hermes_cli import kanban_continuation as kcont
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_placement_reasons as kpr
    from hermes_cli import kanban_worker_pool as kwp
    tid = row["id"]
    fleet = kb.kanban_home() / "fleet"
    why = getattr(cfg, "refused", None) or "no worker pool configured"
    route: "Optional[tuple]" = None
    rule = why
    if kwp.card_pin(row["body"]) is not None:
        known = kwp.known_host_ids(fleet)
        pin, ignored = kwp.resolve_pin(row["body"], known, kwp.pin_aliases(fleet))
        if ignored is not None and pin is None and known:
            route, rule = ("wait", kpr.pin_unknown_host(ignored)), "pin_unknown_host"
        if pin is not None and pin not in (kwp.PIN_ANY, kwp.PIN_STUDIO):
            route, rule = ("wait", "pool_unavailable"), f"no pool plan ({why})"
    if route is None and tid in kb._kwh.carried_ids(conn, [tid]) \
            and (row["workspace_kind"] or "scratch") == "scratch" \
            and kb._kwh.carried_host(conn, tid, row["workspace_path"]):
        route, rule = ("wait", "pool_unavailable"), f"carried workspace, no pool plan ({why})"
    if route is None:
        ident = kbd._identity_only_unplanned([row]).get(tid)
        if ident:
            route, rule = ("wait", ident), ident
    if row["origin_host"] and tid not in held and kcont.mode(kbd._root_kanban_cfg()) == "enforce":
        oroute = kcont.origin_route(conn, row, cfg, config=kbd._root_kanban_cfg(), states={}, dry_run=True,
                                    caps=kbd._capability_tick(cfg))
        if oroute:
            route, rule = oroute, "continuation origin"
        # No origin route: keep the pin / carry / identity wait already set (Prism "Lost wait"); local only if none.
    route = route or ("local", None)
    pins = kbd._wt_pin_hosts(row, None)
    if pins:
        pinned = kbd._pinned_route(row, route, pins, None, {}, planned=False)
        if pinned != route:
            route, rule = pinned, "worktree operator pin"
    return route[0], route[1], rule


def _format(route: str, arg, rule: Optional[str], body: str, studio: Optional[dict], studio_on: bool,
            only: "Optional[tuple]" = None, wt_pin: Optional[str] = None) -> str:
    if route == "portable" and only:
        # Only these pool hosts serve the card (capability or worktree pin): no local slot, no Studio last resort,
        # whatever t_9965b8d5's switch says (Prism 14d195cbf4f1).
        why = "rule worktree operator pin" if wt_pin else _quote(_requires_line(body, None), "rule capability")
        return f"will run on {', '.join(only)}, decided by: {why}"
    if route == "portable":
        where = "a pool host" if studio_on else f"a pool host or {STUDIO} (whichever has room)"
        return f"will run on {where}, decided by: no line pins it"
    if route in ("pin", "origin"):
        if rule and rule.startswith("bind_host:"):
            return f"will run on {arg}, decided by: rule {rule}"
        return f"will run on {arg}, decided by: {_quote(_host_line(body), 'rule ' + str(rule))}"
    if route == "local":
        return f"will run on {STUDIO}, decided by: {_decided_by(body, rule or arg, studio)}"
    return f"will wait ({arg}), decided by: rule {rule or arg}"


def preview_line(conn, task_id: str) -> Optional[str]:
    """``will run on <where>, decided by <line or rule>`` for one card, or None."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_dispatch as kbd
        from hermes_cli import kanban_pause_hold as kph
        row = conn.execute(_ROW_SQL, (task_id,)).fetchone()
        if row is None:
            return None
        body = row["body"] or ""
        # Fleet pause hold first, as the dispatcher does (t_133d6efd), plan or no plan: a held card is never handed
        # to a route entry, and the line says it is held instead of naming a host it will not run on.
        pause_cache: dict = {}
        held = kph.held_routes([row], pause_cache)
        if task_id in held:
            return f"will wait ({held[task_id][1]}), decided by: fleet pause hold"
        cfg = _pool_config()
        if cfg is None or not tuple(getattr(cfg, "hosts", ()) or ()):
            # No plan: the dispatcher's no-plan branch, never a bare "Studio" (Prism 99e5feafc2ff).
            route, arg, rule = _unplanned_route(conn, row, cfg, held, pause_cache)
            if route == "local" and rule and not rule.startswith(("continuation", "worktree")):
                # Local only while the pool is unplanned: say so, never a definite Studio (Apollo F2).
                return f"will run on {STUDIO} while there is no pool plan ({rule}), decided by: no pool plan"
            return _format(route, arg, rule, body, None, False)
        plan = SimpleNamespace(config=cfg, continuation_states={}, continuation_dry_run=True)
        caps = kbd._capability_tick(cfg)
        last_resort = kbd._studio_last_resort(kb.kanban_home() / "fleet")
        studio_on = kbd._studio_capability_on(caps, last_resort)
        plan.studio_capability_only = studio_on
        # The dispatcher's effective assignee for an unassigned card (Prism d0d266085414).
        default_assignee = (kbd._root_kanban_cfg().get("default_assignee") or "").strip() or None
        rules: dict = {}
        studio: dict = {}
        caps_out: dict = {}
        route, arg = kbd._route_row(
            conn, row, plan, default_assignee, {}, None, None, {}, unknown_out={}, caps=caps, caps_out=caps_out,
            rules_out=rules, access_out={}, binds=kbd._workspace_binds(conn, [row], dry_run=True), held=held,
            worktree_out={}, carry_out={}, pause_cache=pause_cache, studio_on=studio_on, studio_out=studio)
        # A portable card skips the Studio only under t_9965b8d5's rule (``_portable_goes_remote``), not under
        # last_resort alone (Prism ce9e08fd61ec).
        return _format(route, arg, rules.get(task_id), body, studio.get(task_id), studio_on,
                       tuple((caps_out.get("only") or {}).get(task_id) or ()),
                       (caps_out.get("wt_pin") or {}).get(task_id))
    except Exception:  # noqa: BLE001 - a preview must never fail a create
        _log.debug("kanban create: placement preview for %s failed", task_id, exc_info=True)
        return None
