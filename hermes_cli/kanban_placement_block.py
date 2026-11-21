"""PLACEMENT blocks: a worker that ran on the wrong host blocks, the dispatcher re-routes it (t_a32116ec).

A ``kind=capability`` block on a run the pool placed on a remote host is a placement fault, not a
human question: the worker found that the host lacks what the card needs (a Studio shell, the vault,
fleet ssh, the lander). On 2026-10-13, 30 such cards sat ``blocked`` for 1-176 h (t_9a038e36 26 h
while the live tree drifted to ahead=429), each saying "re-run on the Studio" to nobody.

Each tick, for every ``blocked``/``capability`` card whose latest block ended a run that carried a
``worker_placed`` event (or whose reason names a host pin), the dispatcher:

* re-routes it ONCE: prepends a ``host:`` pin line to the body (the same pin an operator writes, so
  :func:`kanban_worker_pool.resolve_pin` honours it and the body shows why) and unblocks it with a
  comment. The target is the host a ``host: <x>`` line in the reason names, else the Studio (the
  dispatcher's own machine, which holds every Studio-only resource). The Studio only when the reason
  names a Studio-only capability the map lists (t_9392860c): otherwise the card stays blocked and its
  owner session is woken with the reason (``placement_owner_woken``), and a re-route never writes a
  ``requires:`` word the reason does not support;
* records ``placement_unroutable`` ONCE when it cannot: the named host is not an enabled pool host,
  is the host that already failed, the card carries an operator pin to another host, or a re-routed
  run blocked again. kanban-placement-watch (hermes-home) pages #alerts through episode_pager once
  per card on that event, and flags any placement block older than 60 min with neither event.

:func:`classify` is THE classifier; the watch imports it, never re-implements it.
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional, Tuple

from hermes_cli import kanban_placement_reasons as _pr

REROUTED_EVENT = _pr.PLACEMENT_REROUTED_EVENT
UNROUTABLE_EVENT = "placement_unroutable"
# t_9392860c: a pool-host capability block whose reason names no Studio-only capability stays blocked and goes to
# the card's owner session (verdict OWNER_WAKE). The marker records it once per block; a card without boomerang
# wake also gets a ``mechanical_wake`` (a boomerang card's own ``blocked`` event already wakes it as blocked_assess).
OWNER_WAKE = "owner"
OWNER_WAKE_EVENT = "placement_owner_woken"
OWNER_WAKE_SOURCE = "kanban-placement-block"
# Kill switch (default True). False = the pre-t_9392860c behaviour: every such block re-routes to the Studio and the
# requires: word falls back to studio-state, then the alphabetically first Studio-only word.
STRICT_SWITCH = "placement_block_no_default_studio"


def strict_enabled() -> bool:
    """``kanban.placement_block_no_default_studio`` (default True; non-bool -> default)."""
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("kanban") or {}).get(STRICT_SWITCH, True)
    except Exception:
        return True
    return value if isinstance(value, bool) else True
# A block older than this is not re-routed (the work may be stale): it is recorded unroutable, so the
# watch pages it once for a human to re-pin or close.
REROUTE_MAX_AGE_SECONDS = 24 * 3600
AUTHOR = "kanban-dispatcher"
# A reason that names a host pin or a Studio placement, for blocks on runs with no worker_placed
# event (pre-event runs, or a worker that names the host itself).
PLACEMENT_REASON_RE = re.compile(
    r"(?im)^[ \t]*host:[ \t]*[A-Za-z0-9]|\bhost:[ \t]*(?:studio|mac-studio)\b|studio[- ]placed|"
    r"(?:re-?run|re-?dispatch|re-?place)\w*\s+(?:it\s+|this\s+(?:card\s+)?)?on\s+(?:the\s+)?"
    r"(?:mac[- ]?)?studio|spilled to|placed on (?:ci-box|ace-ai|ace-media)|landed on (?:ci-box|ace-ai|ace-media)")
# Sentence punctuation is not part of a host id ("host: studio." / "host: ace-ai,").
_REASON_PIN_RE = re.compile(r"\bhost:[ \t]*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", re.IGNORECASE)
# t_dde97120: the verdict for a scratch card whose block is "repo path missing on the pool host". The cure
# is a task-private clone on whatever host runs it next, so the card goes back in the queue UNPINNED
# (t_02e30922 was pinned to the Studio for a /Users/<u>/Projects path and never spilled again).
CLONE_CURE = "clone"
# A home-absolute path the worker says is absent. Paths under the live home tree (~/.hermes) are Studio
# state, not a repo a clone recreates, so they never match.
_MISSING_WORD = (r"(?:does not exist|doesn't exist|no such file or directory|is missing|is absent|"
                 r"not found|exists\s*=\s*false)")
_HOME_PATH = r"(?:~|\$\{?HOME\}?|/Users/[^/\s]+|/home/[^/\s]+)/(?!\.hermes\b)[\w.-]+"
_MISSING_REPO_PATH_RE = re.compile(rf"{_HOME_PATH}[^\n]{{0,80}}?\b{_MISSING_WORD}", re.IGNORECASE)
_LIVE_TREE_RE = re.compile(r"(?:~|\$\{?HOME\}?|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes\b")


def _missing_repo_path_only(reason: str, body: Optional[str]) -> bool:
    """True when the block's only complaint is a home-absolute path absent on the pool host, and neither
    the reason nor the body names a Studio-only resource the capability map lists (CARD_NEEDS)."""
    from hermes_cli import kanban_worker_pool as _kwp
    if not _MISSING_REPO_PATH_RE.search(reason or "") or _LIVE_TREE_RE.search(reason or ""):
        return False
    return not _kwp.card_needs(reason) and not _kwp.card_needs(body)


def _reason_pin(reason: str, pool) -> Optional[str]:
    """The canonical host the first resolvable ``host: <x>`` mention in the reason names (studio / a
    pool host); else the first raw word (prose like "qBt host: re-dispatch" is skipped when a real pin
    follows); None when the reason names no pin."""
    from hermes_cli import kanban_worker_pool as _kwp
    first = None
    for m in _REASON_PIN_RE.finditer(reason or ""):
        pin, ignored = _kwp.resolve_pin(f"host: {m.group(1)}", getattr(pool, "hosts", ()) or (),
                                        getattr(pool, "pin_aliases", None))
        if pin:
            return pin
        first = first or ignored
    return first


def classify(*, kind: Optional[str], reason: str, placed_host: Optional[str], body: Optional[str],
             pool, rerouted_before: bool, assignee: Optional[str] = None,
             workspace_kind: Optional[str] = None, studio_reasons: Optional[frozenset] = None,
             strict: Optional[bool] = None) -> Optional[Tuple[str, Optional[str]]]:
    """``("reroute", host)``, ``("clone", None)``, ``("unroutable", why)``, ``("owner", reason)`` or None (not a
    placement block).

    ``strict`` (None = :func:`strict_enabled`, t_9392860c): a Studio re-route needs a Studio-only word the reason
    supports (:func:`supported_studio_words` against ``studio_reasons``). Without one, a reason that names no host
    is :data:`OWNER_WAKE` (stays blocked, the owner session is woken with the reason) and a reason that asks for the
    Studio by pin is unroutable; neither ever reaches the Studio.

    ``placed_host`` is the pool host of the blocked run's ``worker_placed`` event (None = local run).
    ``host`` is ``studio`` or an enabled pool host id. ``clone`` (:data:`CLONE_CURE`): an unpinned
    scratch card that blocked only because a home-absolute repo path is absent on the pool host goes
    back in the queue with no pin and a comment telling the next run to clone (t_dde97120)."""
    from hermes_cli import kanban_worker_pool as _kwp
    if kind != "capability":
        return None
    if rerouted_before:
        # Any capability block after a re-route escalates, whatever its wording: the re-routed run is local.
        return "unroutable", f"re-routed once already and blocked again: {(reason or '')[:160]}"
    if placed_host is None and not PLACEMENT_REASON_RE.search(reason or ""):
        return None
    card_pin, _ign = _kwp.resolve_pin(body, getattr(pool, "hosts", ()) or (),
                                      getattr(pool, "pin_aliases", None))
    if card_pin not in (None, _kwp.PIN_ANY, _kwp.PIN_STUDIO):
        return "unroutable", f"operator pin host:{card_pin} lacks the capability; the pin is not overridden"
    want = _reason_pin(reason, pool)
    if want in (None, _kwp.PIN_STUDIO, _kwp.PIN_ANY):
        if placed_host is None and card_pin == _kwp.PIN_STUDIO:
            return None                      # already ran on the Studio: a real capability block
        if (placed_host is not None and card_pin is None and workspace_kind == "scratch"
                and _missing_repo_path_only(reason, body)):
            return CLONE_CURE, None          # t_dde97120: requeue unpinned; the next run clones
        if (strict_enabled() if strict is None else strict) \
                and not supported_studio_words(reason, studio_reasons):
            if want == _kwp.PIN_STUDIO:
                return "unroutable", (f"the reason asks for host:studio but names no Studio-only capability the "
                                      f"map lists: {(reason or '')[:160]}")
            return OWNER_WAKE, (reason or "")[:1000]
        return "reroute", _kwp.PIN_STUDIO
    if card_pin == _kwp.PIN_STUDIO:
        return "unroutable", f"operator pin host:studio conflicts with the reason's host:{want}; not overridden"
    enabled = {h.name.lower() for h in (getattr(pool, "pool_hosts", ()) or ())
               if getattr(h, "enabled", False) and getattr(h, "state", "active") == "active"}
    if want == (placed_host or "").lower():
        return "unroutable", f"the reason names host:{want}, the host that already failed"
    if want not in enabled:
        return "unroutable", f"the reason names host:{want}, which is not an enabled pool host"
    serves = {h.name.lower(): tuple(getattr(h, "profiles", ()) or ()) for h in pool.pool_hosts}
    if assignee and assignee not in serves.get(want, ()):
        # PoolPlan.take() would refuse the pin with pin_profile on every tick: stranded in ready.
        return "unroutable", f"host:{want} does not serve profile {assignee}"
    return "reroute", want


def _latest_blocks(conn) -> list:
    return conn.execute(
        "SELECT t.id, t.body, t.status, t.assignee, t.workspace_kind, b.payload AS blk, b.run_id, b.created_at AS blocked_at, "
        "(SELECT p.payload FROM task_events p WHERE p.run_id = b.run_id AND p.kind IN (?, ?) "
        " ORDER BY p.id DESC LIMIT 1) AS placed, "
        "EXISTS(SELECT 1 FROM task_events r WHERE r.task_id = t.id AND r.kind IN (?, ?)) AS rerouted, "
        "EXISTS(SELECT 1 FROM task_events u WHERE u.task_id = t.id AND u.kind = ? AND u.id > b.id) AS noted, "
        "EXISTS(SELECT 1 FROM task_events w WHERE w.task_id = t.id AND w.kind = ? AND w.id > b.id) AS owner_woken, "
        "t.boomerang_wake "
        "FROM tasks t JOIN task_events b ON b.id = (SELECT MAX(e.id) FROM task_events e "
        " WHERE e.task_id = t.id AND e.kind IN ('blocked', 'block_loop_detected')) "
        "WHERE t.status IN ('blocked', 'triage') AND t.block_kind = 'capability'",
        (*_pr.kind_names(_pr.WORKER_PLACED_EVENT), *_pr.kind_names(REROUTED_EVENT), UNROUTABLE_EVENT,
         OWNER_WAKE_EVENT),
    ).fetchall()


# t_388c4ab2: a bare ``host: studio`` is DROPPED at dispatch (kanban_worker_pool.effective_pin, #138) unless a
# ``requires:`` word names a reason the Studio serves, so the re-route writes one. Block-reason wording -> map
# word, first match wins; a word the map's Studio row does not list is skipped.
DEFAULT_STUDIO_REQUIRES = "studio-state"
_REQUIRES_HINTS = (
    ("fleet-ssh", re.compile(r"\bssh\b", re.IGNORECASE)),
    ("docker", re.compile(r"\bdocker\b", re.IGNORECASE)),
    ("browser-session", re.compile(r"\bbrowser\b", re.IGNORECASE)),
    # The word "vault" only: a path under ~/Obsidian is the notes-sync tree pool hosts have (t_b4ca4ff0, t_9392860c).
    ("obsidian-vault", re.compile(r"(?<![\w-])vault(?![\w-])", re.IGNORECASE)),
    ("studio-state", re.compile(r"\b(?:launchd|relay|logs?)\b", re.IGNORECASE)),
)
_REQUIRES_HEAD_RE = re.compile(r"^([ \t]*requires:[ \t]*)", re.IGNORECASE | re.MULTILINE)


def supported_studio_words(reason: str, studio_reasons: Optional[frozenset]) -> Tuple[str, ...]:
    """The ``requires:`` words the block reason itself supports (t_9392860c): a word the reason names literally,
    then a :data:`_REQUIRES_HINTS` word whose wording the reason uses, each only if ``studio_reasons`` lists it.
    ``studio_reasons`` None (no map this tick): the hint words alone, unchecked. Never a word the reason lacks."""
    text = reason or ""
    # A word named literally wins over a hint, and a hint never matches inside a hyphenated name: "browser" in
    # "browser-identity" is that capability, not browser-session (Prism ff96bb11f1f1).
    out = [w for w in sorted(studio_reasons or ())
           if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, re.IGNORECASE)]
    for w, rx in _REQUIRES_HINTS:
        if w not in out and (studio_reasons is None or w in studio_reasons) and _bare_hit(rx, text):
            out.append(w)
    return tuple(out)


def _bare_hit(rx, text: str) -> bool:
    """``rx`` matches ``text`` somewhere not joined by a hyphen to a neighbouring word."""
    for m in rx.finditer(text):
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if before != "-" and after != "-":
            return True
    return False


def studio_requires_word(reason: str, studio_reasons: Optional[frozenset],
                         strict: Optional[bool] = None) -> Optional[str]:
    """The ``requires:`` word for a re-route to the Studio: the first hint the block reason names that the map's
    Studio row serves, else ``studio-state``. ``studio_reasons`` None (no map this tick) = ``studio-state``.
    ``strict`` (None = :func:`strict_enabled`): the first :func:`supported_studio_words` word, else None; no
    default and no alphabetical fallback."""
    if strict_enabled() if strict is None else strict:
        words = supported_studio_words(reason, studio_reasons)
        return words[0] if words else None
    if studio_reasons is None:
        return DEFAULT_STUDIO_REQUIRES
    for word, rx in _REQUIRES_HINTS:
        if word in studio_reasons and rx.search(reason or ""):
            return word
    if DEFAULT_STUDIO_REQUIRES in studio_reasons:
        return DEFAULT_STUDIO_REQUIRES
    rest = sorted(studio_reasons - {_pr.ACCESS_CONTROL_PLANE_READ})
    return rest[0] if rest else _pr.ACCESS_CONTROL_PLANE_READ


def reroute_note(reason: str) -> str:
    """The reason a re-route writes on its ``requires:`` line: the block reason's first line, cut short, so the
    line passes hermes-home create_refusal() (t_d0852f2c). Never a newline: the note must stay on the line."""
    first = next((ln.strip() for ln in (reason or "").splitlines() if ln.strip()), "")
    first = re.sub(r"\s+", " ", first)
    if len(first) > 100:
        first = first[:100].rsplit(" ", 1)[0] + " ..."
    return f"re-routed after capability block: {first}" if first else "re-routed after capability block"


def pin_body(body: Optional[str], host: str, *, reason: str, studio_reasons: Optional[frozenset],
             pool, strict: Optional[bool] = None) -> Optional[str]:
    """THE body a re-route writes: ``host: <host>`` unless the body already resolves to that pin, and for the
    Studio a ``requires:`` word :func:`kanban_worker_pool.studio_pin_justified` accepts (the same predicate
    dispatch drops a bare pin with) unless one is already there. An existing ``requires:`` line that names no
    Studio reason gets the word on that line, never a second line. ``studio_reasons`` None = judged against
    ``studio-state`` alone. None (t_9392860c): a Studio pin the body does not already justify and the reason
    supports no word for; the caller records the card unroutable instead of inventing one."""
    from hermes_cli import kanban_worker_pool as _kwp
    text = body or ""
    head = []
    pin, _ign = _kwp.resolve_pin(text, getattr(pool, "hosts", ()) or (), getattr(pool, "pin_aliases", None))
    if pin != host:
        head.append(f"host: {host}")
    if host == _kwp.PIN_STUDIO:
        vocab = studio_reasons if studio_reasons is not None else frozenset({DEFAULT_STUDIO_REQUIRES})
        if not _kwp.studio_pin_justified(text, vocab):
            from hermes_cli import kanban_studio_pin as _ksp
            word = studio_requires_word(reason, studio_reasons, strict=strict)
            if word is None:
                return None
            note = reroute_note(reason)
            if _REQUIRES_HEAD_RE.search(text):
                text = _ksp.add_requires_word(text, word, note)
            else:
                head.append(_ksp.requires_line(word, note))
    return "\n".join(head + [text]) if head else text


def _json(raw) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def reroute_placement_blocks(conn, pool, *, now: Optional[int] = None,
                             studio_reasons: Optional[frozenset] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """One pass over the board: ``([(task_id, host)] re-routed, [task_id] newly unroutable)``.
    ``studio_reasons``: :func:`kanban_worker_pool.studio_map_reasons` this tick (None = no map), for
    :func:`pin_body`."""
    from hermes_cli import kanban_db as _kb
    now = int(time.time()) if now is None else int(now)
    rerouted: List[Tuple[str, str]] = []
    unroutable: List[str] = []
    strict = strict_enabled()
    for row in _latest_blocks(conn):
        blk = _json(row["blk"])
        placed_host = _json(row["placed"]).get("host") if row["placed"] else None
        verdict = classify(kind=blk.get("kind"), reason=str(blk.get("reason") or ""), placed_host=placed_host,
                           body=row["body"], pool=pool, rerouted_before=bool(row["rerouted"]),
                           assignee=row["assignee"], workspace_kind=row["workspace_kind"],
                           studio_reasons=studio_reasons, strict=strict)
        if verdict is None:
            continue
        action, arg = verdict
        tid = row["id"]
        if action == OWNER_WAKE:
            # t_9392860c: the card stays blocked; its owner session decides, now, with the worker's reason.
            if row["owner_woken"]:
                continue
            payload = {"placed_host": placed_host, "run_id": row["run_id"], "reason": arg}
            with _kb.write_txn(conn):
                _kb._append_event(conn, tid, OWNER_WAKE_EVENT, payload)
                if not row["boomerang_wake"]:
                    # A boomerang card's own ``blocked`` event already wakes it (blocked_assess); others need this.
                    _kb._append_event(conn, tid, "mechanical_wake", {
                        "source": OWNER_WAKE_SOURCE,
                        "text": (f"{tid}: run {row['run_id']} on {placed_host or 'a pool host'} blocked "
                                 f"kind=capability and the reason names no Studio-only capability, so it was not "
                                 f"re-routed to the Studio (t_9392860c). Decide: pin a pool host that has it, "
                                 f"correct the card, or ask for a studio-override. Reason: {arg}")})
            continue
        if action == CLONE_CURE and row["status"] == "blocked" \
                and now - int(row["blocked_at"] or now) <= REROUTE_MAX_AGE_SECONDS:
            note = (f"placement re-route: run {row['run_id']} ran on {placed_host} and blocked kind=capability "
                    f"on a home-absolute repo path that host does not have. That path names the repo, not a "
                    f"host: the next run makes a task-private clone in its scratch workspace, so this card "
                    f"is back in the queue with NO host pin (t_dde97120). Once only: a second capability "
                    f"block pages #alerts instead. Reason was: {str(blk.get('reason') or '')[:300]}")
            if not _kb.unblock_task(conn, tid, comment=(AUTHOR, note, None, None)):
                continue
            with _kb.write_txn(conn):
                _kb._append_event(conn, tid, REROUTED_EVENT, {"from_host": placed_host, "to_host": None,
                                                              "cure": CLONE_CURE, "run_id": row["run_id"]})
            rerouted.append((tid, CLONE_CURE))
            continue
        if action == CLONE_CURE:
            action = "reroute"               # triage / too old: the same escalation as a pin re-route
        if action == "reroute" and row["status"] != "blocked":
            # The unblock-loop breaker already sent it to triage (a 2nd same-kind block): a human's card.
            action, arg = "unroutable", "in triage after a repeated capability block"
        elif action == "reroute" and now - int(row["blocked_at"] or now) > REROUTE_MAX_AGE_SECONDS:
            action, arg = "unroutable", f"blocked > {REROUTE_MAX_AGE_SECONDS // 3600} h ago; re-pin or close it by hand"
        if action == "unroutable":
            if row["noted"]:
                continue
            with _kb.write_txn(conn):
                _kb._append_event(conn, tid, UNROUTABLE_EVENT,
                                  {"placed_host": placed_host, "why": arg, "run_id": row["run_id"]})
            unroutable.append(tid)
            continue
        reason = str(blk.get("reason") or "")
        new_body = pin_body(row["body"], arg, reason=reason, studio_reasons=studio_reasons, pool=pool,
                            strict=strict)
        if new_body is None:
            # t_9392860c: no word the reason supports; never invent one.
            if not row["noted"]:
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, UNROUTABLE_EVENT, {
                        "placed_host": placed_host, "run_id": row["run_id"],
                        "why": f"no Studio-only capability the reason supports: {reason[:160]}"})
                unroutable.append(tid)
            continue
        from hermes_cli import kanban_host_capabilities as _khc
        words = _khc.declared_words(new_body)
        carries = f"`host: {arg}`" + (f" and `requires: {', '.join(words)}` (a bare Studio pin is dropped at "
                                      f"dispatch)" if arg == "studio" and words else "")
        note = (f"placement re-route: run {row['run_id']} ran on {placed_host or 'a host'} and blocked "
                f"kind=capability, so this card now carries {carries} and is back in the queue. "
                f"Once only: a second capability block pages #alerts instead. Reason was: "
                f"{reason[:300]}")
        # Order is the atomicity (unblock_task owns its own write txn and cannot nest): the pin goes in with a
        # compare-and-swap on the body read above (a concurrent edit is never overwritten), then the unblock,
        # and the placement_rerouted marker ONLY after the unblock landed. A failed unblock reverts the pin
        # under the same CAS, so the card stays blocked, unmarked, and is retried next tick, never escalated.
        with _kb.write_txn(conn):
            cur = conn.execute("UPDATE tasks SET body = ? WHERE id = ? AND status = 'blocked' AND body IS ?",
                               (new_body, tid, row["body"]))
            if cur.rowcount != 1:
                continue
        if not _kb.unblock_task(conn, tid, comment=(AUTHOR, note, None, None)):
            with _kb.write_txn(conn):
                conn.execute("UPDATE tasks SET body = ? WHERE id = ? AND status = 'blocked' AND body IS ?",
                             (row["body"], tid, new_body))
            continue
        with _kb.write_txn(conn):
            _kb._append_event(conn, tid, REROUTED_EVENT,
                              {"from_host": placed_host, "to_host": arg, "run_id": row["run_id"]})
        rerouted.append((tid, arg))
    return rerouted, unroutable
