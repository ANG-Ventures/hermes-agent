"""Mac Studio default-deny: a kanban worker starts on the Studio only for an ALLOWLISTED capability (t_062a5948).

Ace 2026-11-11 (Discord 1570218756024569917, scope addition 1570222371485519942): kanban workers stay off the Mac
Studio, which is the orchestration machine. This module adds one rule on top of t_9965b8d5
(:mod:`hermes_cli.kanban_studio_capability`). That rule already makes a ``local`` route name the Studio-only capability
that holds it (``justify``). Here, while the switch is on, that named capability must also be

* on the allowlist ``kanban.placement.studio_default_deny.allow`` (the capabilities the operator has accepted as
  truly Studio-only), or
* covered by a live operator override for this card (``hermes kanban studio-override``),

else the card WAITS: ``studio_default_deny:<capability> (<source>: <evidence>; no pool host has it)``. It is never
dropped and never spawned on the Studio by itself. A portable card already never falls back to the Studio
(t_9965b8d5), so a card held here is one that no pool host can take.

Matching: an allowlist entry matches the capability, or the head before ``:`` (``workspace_kind`` covers
``workspace_kind:origin_not_github``). A ``studio_needs`` capability such as ``control-plane,obsidian-vault`` is
allowed only if EVERY part is allowed.

Exempt: operator-only cards, meaning the assignee is an operator profile (:data:`kanban_db.OPERATOR_PROFILES`) or
the card is ``no_worker``.

Override (``hermes kanban studio-override <card> --capability <cap> --why <reason> [--once | --until <ISO>]``):
operator token only, ``--capability`` required. One ``studio_override`` board event is written. ``--once`` (the
default) is used up by the card's next run that starts ON the Studio; a run on a pool host leaves it unspent and
its ``worker_placed`` event says so (``studio_override_unspent``, t_a94fafef). ``--until`` holds until that time. The override covers a held
capability only when it names one of its parts. Every override also writes one line to the capability-gap ledger
``<root>/state/studio-capability-gap/overrides.jsonl`` (t_b3884ae0's state dir). A ``browser-session`` override
needs ``--need`` from a fixed set (:data:`NEEDS`, t_9d6a5d75): only ``signed-in-identity`` is accepted, ``other`` is
accepted and recorded, every other need is refused with the capability that serves it. The ``--why`` text is never
parsed for the need. A GAP capability (not Studio-only by nature) also needs ``--elsewhere``, ``--why-not-now`` and
``--effort hours|days`` (hours: plus an open ``--fix-card``): :mod:`hermes_cli.kanban_studio_gap` (t_9d619fbb).

Holding: the first tick that holds a card in an episode writes ONE ``mechanical_wake`` event (source
``studio-default-deny``). The gateway owner-wake turns that into a silent turn in the card's home session, not a chat
post. A hold over 60 minutes pages #alerts once per episode through the root's episode pager (producer
``studio-default-deny``, key ``held:<card>``, latched by a ``studio_default_deny_paged`` event). The structural-hold
report (kanban_placement_starvation) also names the fix on the card.

Switch: ``kanban.placement.studio_default_deny.enabled`` in the ROOT config.yaml, read every tick, default false. The
rule is also inert whenever t_9965b8d5's rule is off (its kill switch, Studio not last resort, map unreadable).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

KNOB = "studio_default_deny"
WAIT_PREFIX = "studio_default_deny"
OVERRIDE_EVENT = "studio_override"
HOLD_WAKE_SOURCE = "studio-default-deny"
WAKE_EVENT = "mechanical_wake"
PAGED_EVENT = "studio_default_deny_paged"
PAGER_PRODUCER = "studio-default-deny"
PAGE_AFTER_S = 60 * 60
LEDGER_DIR = ("state", "studio-capability-gap")
LEDGER_FILE = "overrides.jsonl"
# Local starts that never reach ``_route_row`` (Prism 827ad8491946 on #251) name these when no declared Studio-only
# capability does: an unplanned tick (pool disabled/refused/empty) and the review lane (always local).
NO_PLAN_CAP = "pool_unavailable"
REVIEW_CAP = "review_lane"
BROWSER_JOIN_CAP = "browser_join"  # a portable row kept on the Studio because its browser join was cut there
# ``kanban admit --host studio`` on a portable card (Prism c-e7592fee5f5910ad): admit never writes a Studio start the
# rule would not, so it is held under this stand-in until a ``studio-override --capability operator_admit`` exists.
ADMIT_CAP = "operator_admit"
_STAND_IN_EVIDENCE = {NO_PLAN_CAP: "no pool plan this tick", REVIEW_CAP: "the review lane runs only locally",
                      BROWSER_JOIN_CAP: "its browser join was cut for the Studio",
                      ADMIT_CAP: "kanban admit --host studio on a card no Studio-only capability holds"}


def config(kcfg: Optional[Mapping]) -> tuple:
    """``(enabled, allow)`` from the ROOT ``kanban`` section. Default ``(False, frozenset())``."""
    placement = (kcfg or {}).get("placement") if isinstance(kcfg, Mapping) else None
    sect = placement.get(KNOB) if isinstance(placement, Mapping) else None
    if not isinstance(sect, Mapping):
        return False, frozenset()
    val = sect.get("enabled", False)
    if isinstance(val, str):
        on = val.strip().lower() in ("on", "true", "yes", "1")
    else:
        on = bool(val)
    allow = sect.get("allow") or ()
    if isinstance(allow, str):
        allow = [a for a in allow.replace(",", " ").split()]
    words = frozenset(str(a).strip().lower() for a in allow if str(a).strip())
    return on, words


def parts(capability: Optional[str]) -> tuple:
    return tuple(p.strip().lower() for p in str(capability or "").split(",") if p.strip())


def head(part: str) -> str:
    """The capability name before any detail: ``workspace_kind:x`` -> ``workspace_kind``, ``linked(k->t)`` ->
    ``linked`` (Prism e3e on #251: structural rules carry parenthesized details)."""
    return re.split(r"[:(]", part, maxsplit=1)[0].strip()


def _identity_names(part: str) -> frozenset:
    """Both spellings of the identity capability when ``part`` is one (t_f935cb6f, Prism b4a322a16d7c): the map now
    derives ``browser-identity`` where older allow lists and live overrides say ``browser-session``."""
    from hermes_cli.kanban_host_capabilities import BROWSER_IDENTITY_CAPS
    return BROWSER_IDENTITY_CAPS if head(part) in BROWSER_IDENTITY_CAPS else frozenset({part})


def _covered(part: str, words) -> bool:
    return part in words or head(part) in words or bool(_identity_names(part) & set(words))


def allowed(capability: Optional[str], allow) -> bool:
    """True when every part of ``capability`` is on ``allow``. An unnamed capability is never allowed."""
    ps = parts(capability)
    return bool(ps) and all(_covered(p, allow) for p in ps)


def operator_only(row, assignee: Optional[str] = None) -> bool:
    """``assignee`` = the effective assignee (the row's, else the dispatcher's default). Byte-exact compare."""
    from hermes_cli import kanban_db as _kb
    keys = row.keys() if hasattr(row, "keys") else ()
    if "no_worker" in keys and row["no_worker"]:
        return True
    return (assignee or row["assignee"] or "") in _kb.OPERATOR_PROFILES


def live_override(conn, task_id: str, capability: Optional[str], *, now: Optional[float] = None) -> Optional[dict]:
    """The newest ``studio_override`` on the card that still holds and names a part of ``capability``, else None.
    ``once``: no Studio start after it (:func:`_spent_on_studio`). ``until``: ``now`` before it."""
    want = set(parts(capability))
    want |= {head(p) for p in want}
    want |= {n for p in list(want) for n in _identity_names(p)}
    if not want:
        return None
    for p in _live_overrides(conn, task_id, now):
        if str(p.get("capability") or "").strip().lower() in want:
            return p
    return None


def _live_overrides(conn, task_id: str, now: Optional[float]):
    """Every ``studio_override`` on the card that still holds, newest first."""
    now = time.time() if now is None else now
    for ev_id, raw in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
            (task_id, OVERRIDE_EVENT)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        until = p.get("until")
        if isinstance(until, (int, float)):
            if now < until:
                yield p
            continue
        if not _spent_on_studio(conn, task_id, ev_id):
            yield p


def _spent_on_studio(conn, task_id: str, after_id: int) -> bool:
    """True when a ``spawned`` event after ``after_id`` started ON the Studio (t_a94fafef): its run has no
    ``worker_placed`` naming a pool host ("no worker_placed" means ran here). A spawn with no run id counts as a
    Studio start, the old reading. A run on a pool host leaves a ``--once`` override unspent."""
    from hermes_cli import kanban_host_capabilities as _khc
    from hermes_cli import kanban_placement_reasons as _pr
    studio = {_khc.STUDIO_HOST, "local", "studio"}
    for (run_id,) in conn.execute(
            "SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'spawned' AND id > ? ORDER BY id",
            (task_id, after_id)):
        if run_id is None:
            return True
        hosts = []
        for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
                                   f"AND kind IN ({_pr.kind_marks(_pr.WORKER_PLACED_EVENT)})",
                                   (task_id, run_id, *_pr.kind_names(_pr.WORKER_PLACED_EVENT))):
            try:
                h = (json.loads(raw or "{}") or {}).get("host")
            except (TypeError, ValueError, AttributeError):
                h = None
            hosts.append(str(h or "").strip().lower())
        if not any(h and h not in studio for h in hosts):
            return True
    return False


def unspent_override_note(conn, task_id: str, *, now: Optional[float] = None) -> Optional[str]:
    """For a run placed OFF the Studio: the newest override still waiting for a Studio start, as
    ``studio-override <cap> unspent (once|until <epoch>)``, else None (t_a94fafef)."""
    try:
        p = next(_live_overrides(conn, task_id, now), None)
    except Exception:
        return None
    if p is None:
        return None
    until = p.get("until")
    kind = f"until {int(until)}" if isinstance(until, (int, float)) else "once"
    return f"studio-override {p.get('capability')} unspent ({kind})"


def override_command(task_id: str, reason: str) -> Optional[str]:
    """The ``studio-override`` command for a ``studio_default_deny:<cap> (...)`` hold, else None. For a multi-part
    capability it names the first part."""
    if not str(reason or "").startswith(WAIT_PREFIX + ":"):
        return None
    cap = reason[len(WAIT_PREFIX) + 1:].split(" (", 1)[0]
    first = (parts(cap) or ("<cap>",))[0]
    return f'hermes kanban studio-override {task_id} --capability {first} --why "<reason>"'


def wait_reason(why: Mapping) -> str:
    cap = why.get("capability") or "unnamed"
    src, ev = why.get("source") or "?", why.get("evidence") or ""
    return f"{WAIT_PREFIX}:{cap} ({src}: {ev}; no pool host has it; not on the Studio allowlist)"


def decide(conn, row, why: Optional[Mapping], *, enabled: bool, allow, now: Optional[float] = None,
           assignee: Optional[str] = None):
    """``None`` = the Studio route stands (rule off, exempt, allowlisted, or overridden); else the wait reason.
    ``why`` is what ``kanban_studio_capability.justify`` named, or a stand-in for a local start that never ran it
    (:data:`NO_PLAN_CAP`, :data:`REVIEW_CAP`)."""
    if not enabled or why is None or operator_only(row, assignee):
        return None
    cap = why.get("capability")
    if allowed(cap, allow):
        return None
    if live_override(conn, row["id"], cap, now=now) is not None:
        return None
    return wait_reason(why)


def unrouted_hold(conn, row, caps, *, studio_on: bool, kcfg: Optional[Mapping], stand_in: str,
                  default_assignee: Optional[str] = None) -> Optional[str]:
    """The wait reason for a LOCAL start that ``_route_row`` never classified (no pool plan, or a review row), else
    None. Same switch, allowlist, exemption and override as :func:`decide`. The capability is the card's declared
    Studio-only one when it names one, else ``stand_in``. Inert unless t_9965b8d5's rule is on (``studio_on``)."""
    enabled, allow = config(kcfg)
    if not enabled or not studio_on:
        return None
    why = resolved_capability(row, caps, stand_in)
    return decide(conn, row, why, enabled=enabled, allow=allow, assignee=row["assignee"] or default_assignee)


def resolved_capability(row, caps, stand_in: str) -> dict:
    """The capability :func:`unrouted_hold` judges a local start by: the card's declared or attached-skill
    Studio-only capability, else ``stand_in``. The review lane records it in its placement event (Prism
    c-0d4f28b3daf38893), so the replay reads what the gate would have resolved instead of assuming the stand-in."""
    from hermes_cli import kanban_host_capabilities as _khc
    from hermes_cli import kanban_studio_capability as _ksc
    try:
        skills = tuple(s for s in (json.loads(row["skills"] or "[]") or []) if isinstance(s, str))
    except (TypeError, ValueError):
        skills = ()
    why = _ksc.studio_capability(_khc.declared_words(row["body"]), skills, getattr(caps, "cmap", None))
    if why is None:
        why = {"capability": stand_in, "source": "dispatch", "evidence": _STAND_IN_EVIDENCE.get(stand_in, stand_in)}
    return why


# t_f914c529: a ported card whose clone on the port host still holds work waits ``port_stranded:<host> (...)``
# (kanban_worktree_port.STRANDED_PREFIX). It takes the same wake + 60-minute page as a default-deny hold.
STRANDED_PREFIX = "port_stranded"


def _cap_of(reason: str) -> str:
    """The held key: ``<cap>`` for a default-deny hold, ``port_stranded:<host>`` for a stranded clone, else ''."""
    r = str(reason or "")
    if r.startswith(WAIT_PREFIX + ":"):
        return r[len(WAIT_PREFIX) + 1:].split(" (", 1)[0]
    if r.startswith(STRANDED_PREFIX + ":"):
        return r.split(" (", 1)[0]
    return ""


def _held_reason(reason) -> bool:
    return str(reason or "").startswith((WAIT_PREFIX + ":", STRANDED_PREFIX + ":"))


def _held_this_episode(conn, task_id: str, reason: Optional[str] = None) -> bool:
    """True when the current wait episode already woke the operator: a ``mechanical_wake`` from this source with no
    episode-ending event after it. With ``reason``, only a wake for the SAME held capability counts (t_f914c529:
    t_17732be2 went from studio-state to workspace_kind:origin_not_ours, and an override names one capability)."""
    want = _cap_of(reason) if reason is not None else None
    for kind, _created, p in _episode_events(conn, task_id, (WAKE_EVENT,)):
        if kind != WAKE_EVENT:
            return False
        if p.get("source") == HOLD_WAKE_SOURCE:
            if want is None:
                return True
            got = p.get("capability")
            if got is None:  # a wake from before the field: its text quotes the reason
                got = _cap_of(str(p.get("text") or "").split("default-deny: ", 1)[-1])
            if got == want:
                return True
    return False


def _episode_events(conn, task_id: str, kinds):
    """Newest first: ``kinds`` rows and the episode-ending rows, as ``(kind, created_at, payload dict)``. A
    ``placement_wait`` with ``cleared: true`` is yielded as an episode end too (Prism c-51c01dfda9372e31): the hold
    it named is over, so the next hold is a new episode (a new wake, a fresh 60-minute clock)."""
    from hermes_cli import kanban_placement_reasons as _pr
    ends = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    want = (*kinds, *_pr.kind_names("placement_wait"), *ends)
    for kind, created, raw in conn.execute(
            "SELECT kind, created_at, payload FROM task_events WHERE task_id = ? AND kind IN (%s) ORDER BY id DESC"
            % ",".join("?" * len(want)), (task_id, *want)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            p = {}
        p = p if isinstance(p, dict) else {}
        if _pr.normalize_kind(kind) == "allocation_wait":
            if not p.get("cleared"):
                continue
            kind = "placement_wait_cleared"
        yield kind, created, p


def wake_text(task_id: str, reason: str) -> str:
    if str(reason or "").startswith(STRANDED_PREFIX + ":"):
        host = _cap_of(reason)[len(STRANDED_PREFIX) + 1:]
        return (f"{task_id} is held: its clone on {host} still holds work and that host cannot serve the card "
                f"any more: {reason}. On {host}, push that clone's commits (and commit or drop any uncommitted "
                f"or stashed work); the next dispatcher tick then hands the card over to a fresh clone. Nothing "
                f"starts elsewhere until then.")
    cmd = override_command(task_id, reason) or \
        f'hermes kanban studio-override {task_id} --capability <cap> --why "<reason>"'
    return (f"{task_id} is held off the Mac Studio by default-deny: {reason}. No pool host can take it. "
            f"If it truly needs the Studio, run `{cmd}` (one run), or add the capability to "
            f"kanban.placement.studio_default_deny.allow. "
            f"Otherwise fix the gap (a pool host that advertises it, or a card that does not need it).")


def _episode_start(conn, task_id: str) -> Optional[int]:
    """When the current hold episode began: the oldest ``mechanical_wake`` from this source after the last
    episode-ending event, or None."""
    start = None
    for kind, created, p in _episode_events(conn, task_id, (WAKE_EVENT, PAGED_EVENT)):
        if kind not in (WAKE_EVENT, PAGED_EVENT):
            break
        if kind == PAGED_EVENT:
            return None  # this episode already paged
        if p.get("source") == HOLD_WAKE_SOURCE:
            start = int(created)
    return start


def _root_pager():
    """An ``EpisodePager`` from the running root's ``scripts/lib/episode_pager.py``, or None (hermetic home)."""
    import importlib.util
    import sys
    from hermes_cli import kanban_db as _kb
    path = _kb.kanban_home() / "scripts" / "lib" / "episode_pager.py"
    if not path.is_file():
        return None
    name = "_hermes_episode_pager"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod.EpisodePager(PAGER_PRODUCER)


def notify_holds(conn, waits: Mapping[str, str], *, now: Optional[int] = None, pager_factory=None) -> list:
    """Per held card: ONE ``mechanical_wake`` per hold episode (silent turn in the card's home session), and ONE
    #alerts page once the episode is :data:`PAGE_AFTER_S` old (latched by a ``studio_default_deny_paged`` event).
    Returns the ids woken this call."""
    from hermes_cli import kanban_db as _kb
    now = int(time.time()) if now is None else int(now)
    factory = _root_pager if pager_factory is None else pager_factory
    pager = None
    woke = []
    for tid, reason in (waits or {}).items():
        if not _held_reason(reason):
            continue
        try:
            if not _held_this_episode(conn, tid, reason):
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, WAKE_EVENT,
                                      {"source": HOLD_WAKE_SOURCE, "capability": _cap_of(reason),
                                       "text": wake_text(tid, reason)})
                woke.append(tid)
                continue
            start = _episode_start(conn, tid)
            if start is None or now - start < PAGE_AFTER_S:
                continue
            if pager is None:
                pager = factory()
            if pager is None:
                continue
            if str(reason).startswith(STRANDED_PREFIX + ":"):
                text = (f"🔒 Stranded clone: {tid} held {(now - start) // 60} min. {reason}.")
            else:
                text = (f"🔒 Studio default-deny: {tid} held {(now - start) // 60} min, no pool host can take it. "
                        f"{reason}. Decide: `hermes kanban studio-override {tid} --capability <cap> --why \"...\"`, "
                        f"allowlist the capability, or fix the gap.")
            outcome = pager.page(f"held:{tid}", text)
            if outcome != "failed":
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, PAGED_EVENT, {"waited": now - start, "outcome": outcome})
        except Exception as exc:  # one bad card never stops the tick
            _kb._log.warning("kanban studio default-deny wake/page on %s failed: %s", tid, exc)
    return woke


# -- the operator verb -------------------------------------------------------------------------------------------

class OverrideRefused(ValueError):
    pass


def _parse_until(value: str, now: float) -> int:
    from hermes_cli import kanban_db as _kb
    try:
        ts = _kb.parse_wake_at(value, now=int(now))
    except Exception as exc:
        raise OverrideRefused(f"--until {value!r} is not a time ({exc})") from exc
    if ts <= now:
        raise OverrideRefused(f"--until {value!r} is in the past")
    return int(ts)


def ledger_path(root: Optional[Path] = None) -> Path:
    if root is None:
        from hermes_cli import kanban_db as _kb
        root = _kb.kanban_home()
    return Path(root).joinpath(*LEDGER_DIR, LEDGER_FILE)


def _fix_card(conn, capability: str) -> Optional[str]:
    """An open card whose idempotency key is ``studio-capgap:<cap>`` (t_b3884ae0's key), or None."""
    row = conn.execute("SELECT id FROM tasks WHERE idempotency_key = ? AND status NOT IN ('done', 'archived') "
                       "ORDER BY created_at DESC LIMIT 1", (f"studio-capgap:{capability}",)).fetchone()
    return row[0] if row else None


# t_9d6a5d75 (design t_e7f581f6 card 4; operator ruling 11-19): a ``browser-session`` override declares its need from
# this fixed set. 13 of the 34 browser-session override cards (11-14..11-20) needed an SSH lane, the Gmail lane, LAN
# reach or a public page, not a signed-in identity. need -> (what it is, the capability to use instead or None).
BROWSER_CAP = "browser-session"
# t_f935cb6f split browser-session: browser-identity is its target (browser-session its alias for one release), a
# public page is browser-public and a .ace LAN web UI is lan-reach. An override of either identity name declares a need.
BROWSER_CAPS = frozenset({BROWSER_CAP, "browser-identity"})
NEEDS = {
    "signed-in-identity": ("a signed-in browser identity", None),
    "public-page": ("a public web page", "browser-public"),
    "lan-reach": ("LAN reach to a host (slicer, dashboard, .ace API)", "lan-reach"),
    "ssh": ("an SSH lane (operator lane, portal host)", "fleet-ssh"),
    "gmail": ("the Gmail read lane (a Studio job; a pool host reaches it over `ssh studio`)", "studio-state"),
    "other": ("a need not in this list (say it in --why)", None),
}


def need_refusal(capability: str, need: Optional[str]) -> Optional[str]:
    """The refusal line for a ``browser-session`` override's ``need``, or None when it may go through."""
    if (capability or "").strip().lower() not in BROWSER_CAPS:
        return None
    n = (need or "").strip().lower()
    if n not in NEEDS:
        return (f"--capability {BROWSER_CAP} needs --need, one of: {', '.join(NEEDS)}"
                + (f" (got {need!r})" if n else ""))
    what, instead = NEEDS[n]
    if instead is None:
        return None
    return (f"--need {n} is {what}, not a signed-in browser identity: override with --capability {instead} instead, "
            f"and drop the {BROWSER_CAP} hold at its source (the card's requires: line or the attached skill)")


def override(conn, task_id: str, capability: str, why: str, *, once: bool = True, until: Optional[str] = None,
             actor: Optional[str] = None, now: Optional[float] = None, root: Optional[Path] = None,
             need: Optional[str] = None, elsewhere: Optional[str] = None, why_not_now: Optional[str] = None,
             effort: Optional[str] = None, fix_card: Optional[str] = None) -> dict:
    """Record one override. Raises :class:`OverrideRefused` with the line to print."""
    from hermes_cli import kanban_db as _kb
    now = time.time() if now is None else now
    state = _kb._operator_token_state()
    if state != "ok":
        raise OverrideRefused(f"studio-override is operator-only: {_kb.OPERATOR_TOKEN_ENV} {state.replace('_', ' ')}")
    cap = (capability or "").strip().lower()
    if not cap or "," in cap:
        raise OverrideRefused("--capability is required: name ONE capability (the one the wait reason names)")
    if len((why or "").split()) < 3:
        raise OverrideRefused("--why is required: at least 3 words saying why this card needs the Studio")
    refused = need_refusal(cap, need)
    if refused:
        raise OverrideRefused(refused)
    row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise OverrideRefused(f"unknown card {task_id}")
    until_ts = _parse_until(until, now) if until else None
    # t_9d619fbb: a GAP capability (not Studio-only by nature) must say how it would run elsewhere.
    from hermes_cli import kanban_studio_gap as _gap
    gap_root = Path(root) if root is not None else _kb.kanban_home()
    try:
        gap = _gap.check(conn, task_id, cap, elsewhere=elsewhere, why_not_now=why_not_now, effort=effort,
                         fix_card=fix_card, until=until, allow=config(_kb._board_home_kanban_cfg())[1], root=gap_root)
    except _gap.GapRefused as exc:
        raise OverrideRefused(str(exc)) from exc
    payload = {"capability": cap, "why": why.strip(), "by": actor or "operator", "at": int(now),
               "once": until_ts is None, "until": until_ts,
               "fix_card": gap.get("fix_card_given") or _fix_card(conn, cap),
               **({"need": need.strip().lower()} if need else {}), **gap}
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, OVERRIDE_EVENT, payload)
    line = {"kind": "override", "task_id": task_id, **payload}
    path = ledger_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        payload["ledger"] = str(path)
    except OSError as exc:  # the board event is the record; say the ledger write failed
        payload["ledger_error"] = str(exc)
    from hermes_cli import kanban_placement_reasons as _pr
    last = conn.execute(f"SELECT payload FROM task_events WHERE task_id = ? AND kind IN ({_pr.kind_marks('placement_wait')}) "
                        "ORDER BY id DESC LIMIT 1", (task_id, *_pr.kind_names("placement_wait"))).fetchone()
    held = other = None
    if last:
        try:
            doc = json.loads(last[0] or "{}") or {}
            reason = "" if doc.get("cleared") else (doc.get("reason") or "")
        except (TypeError, ValueError, AttributeError):
            reason = ""
        if reason.startswith(WAIT_PREFIX + ":"):
            held = reason[len(WAIT_PREFIX) + 1:].split(" (", 1)[0]
        elif reason:
            other = reason  # t_f914c529: a different rule holds it; the override cannot release that
    payload["held_capability"] = held
    payload["held_by_other"] = other
    return payload


def cmd_studio_override(args) -> int:
    """``hermes kanban studio-override``: one line on success (rc 0), one REFUSED line otherwise (rc 1)."""
    import sys
    from hermes_cli import kanban_db as _kb
    actor = (os.environ.get("HERMES_PROFILE") or "").strip() or "operator"
    try:
        with _kb.connect_closing(board=getattr(args, "board", None)) as conn:
            p = override(conn, args.task_id, args.capability, args.why, until=getattr(args, "until", None),
                         actor=actor, need=getattr(args, "need", None), elsewhere=getattr(args, "elsewhere", None),
                         why_not_now=getattr(args, "why_not_now", None), effort=getattr(args, "effort", None),
                         fix_card=getattr(args, "fix_card", None))
    except OverrideRefused as exc:
        print(f"kanban studio-override: REFUSED {exc}", file=sys.stderr)
        return 1
    scope = "once (next run only)" if p["once"] else f"until {p['until']}"
    need = f" (need {p['need']})" if p.get("need") else ""
    print(f"kanban studio-override: {args.task_id} may start on the Studio for {p['capability']}{need} {scope}; "
          f"fix card: {p['fix_card'] or 'none'}; event={OVERRIDE_EVENT}; ledger={p.get('ledger') or 'NOT WRITTEN'}")
    held = parts(p.get("held_capability"))
    if held and p["capability"] not in set(held) | {head(x) for x in held}:  # live_override's match
        print(f"kanban studio-override: WARNING {args.task_id} is held for {p['held_capability']}, not "
              f"{p['capability']}: this override does not release it", file=sys.stderr)
    if p.get("held_by_other"):
        print(f"kanban studio-override: WARNING {args.task_id}'s latest placement wait is {p['held_by_other']}, "
              f"not a studio_default_deny hold: this override does not release it. Read "
              f"`hermes kanban allocation {args.task_id}` for the rule that holds it", file=sys.stderr)
    return 0
