"""A Studio override for a GAP reason must say how the work would run elsewhere (t_9d619fbb).

Owner ruling (Discord 1575870678035337328 + 1575870800949547108, 2026-11-27): the Mac Studio is the busiest host.
When a card is made Studio-only because something is merely not installed elsewhere, the operator must say how
another computer could run it, why it cannot right now, and how long setting that up would take. The default is to
set it up on the other computers; the Studio is preferred only when that takes DAYS.

Every path that starts a gap card on the Studio ends at ``hermes kanban studio-override`` (t_062a5948 default-deny):
``kanban admit --host studio`` on a card no Studio-only capability holds waits ``studio_default_deny:operator_admit``,
a ``worktree:`` card on a Studio path waits ``studio_default_deny:workspace_kind``, and a bare ``host: studio`` without
a Studio-served ``requires:`` word is dropped by the dispatcher. So the check lives in the override verb only.

Classification (structured, the ``--why`` text is never parsed):

* inherent: every part of the capability is Studio-only in ``fleet/host-capabilities.json``
  (:func:`kanban_studio_capability.studio_only`: keychain, signed-in browser identity, Apple apps, control plane...)
  or on the default-deny allowlist. Unchanged: no new flags needed.
* gap: anything else. The dispatcher stand-ins (:data:`STAND_INS`: ``operator_admit``, ``pool_unavailable``,
  ``review_lane``, ``browser_join``) and ``workspace_kind*`` are gaps even when the map is unreadable; a capability a
  pool host also has (``studio-state``, ``docker``, ``ssh-root:*``) is a gap when the map is readable. With the map
  unreadable a named capability is treated as today (inherent), so a broken map never wedges the operator.

A gap override needs ``--elsewhere`` (which pool host, what to install or move; >= 20 chars and naming a pool host
when the pool file is readable), ``--why-not-now`` (>= 20 chars) and ``--effort hours|days``:

* hours: also ``--fix-card <id>``, an OPEN card that does the setup. Without it the override is refused and the
  refusal prints the create command (on the override's board). It covers ONE run (``--until`` is refused).
* days: passes with the answers recorded; the digest line lists it so the estimate can be challenged.

The fix card is tracked per GAP KEY (the capability, the ``studio-capgap:<key>`` of the gap card), not per card:

* while a fix card named for the key is open, a later override for that key on ANY card passes without the answers
  and is counted on the fix card (one comment line each);
* once that fix card is done, every override for the key is refused ("run it on a pool host: ..."); a NEW open fix
  card (the gap reopened) is accepted with the answers. An archived fix card is abandoned, not done: it closes nothing
  and cannot be named again.
* the same refusal (key + problem) printed again within 24 h is one line plus the fix card, not the full screen
  (``state/studio-capability-gap/gap-refusals.jsonl``).

All answers go in the ``studio_override`` event and the overrides.jsonl ledger line.

Kill switch: the file ``<root>/state/studio-capability-gap/gap-gate.off`` (any content). While it exists the
override behaves exactly as before this module. Cost: one sqlite read per gap override plus one map read; the
``create`` and ``admit`` paths are untouched.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, Optional

KILL_FILE = ("state", "studio-capability-gap", "gap-gate.off")
STAND_INS = frozenset({"operator_admit", "pool_unavailable", "review_lane", "browser_join"})
STRUCTURAL = frozenset({"workspace_kind"})
EFFORTS = ("hours", "days")
MIN_TEXT = 20
INHERENT, GAP = "inherent", "gap"
_CLOSED = ("done", "archived")   # not eligible as a --fix-card
_DONE = ("done",)                # proof the gap was closed; an archived card was abandoned, not finished


class GapRefused(ValueError):
    pass


def kill_path(root: Path) -> Path:
    return Path(root).joinpath(*KILL_FILE)


def disabled(root: Path) -> bool:
    try:
        return kill_path(root).exists()
    except OSError:
        return False


def studio_only_caps(root: Path) -> Optional[frozenset]:
    """Studio-only capability words from ``<root>/fleet/host-capabilities.json``; None when unreadable."""
    try:
        from hermes_cli import kanban_host_capabilities as khc
        from hermes_cli import kanban_studio_capability as ksc
        cmap = khc.load_map(Path(root) / "fleet")
        if cmap is None:
            return None
        # a map that exists but is malformed or has no Studio row is unreadable too: studio_only() says None, and
        # an empty set would turn keychain/browser-identity into gaps (operator read 1, 2026-11-27)
        so = ksc.studio_only(cmap)
        return None if so is None else frozenset(so)
    except Exception:
        return None


def classify(capability: str, *, allow: Iterable[str] = (), studio_only: Optional[frozenset] = None) -> str:
    from hermes_cli import kanban_studio_allowlist as ksa
    ps = ksa.parts(capability)
    if not ps or any(ksa.head(p) in STAND_INS or ksa.head(p) in STRUCTURAL for p in ps):
        return GAP
    words = set(allow)
    if studio_only is None:
        return INHERENT  # map unreadable: today's behaviour for a named capability
    words |= set(studio_only)
    return INHERENT if all(ksa._covered(p, words) for p in ps) else GAP


def pool_hosts(root: Path) -> Optional[tuple]:
    """Placement targets from the root's pool file (``PoolConfig.pool_hosts``: enabled, ``targets`` true, with a
    worker login), lower-case, in pool order, never the Studio itself; ``None`` when unreadable, ``()`` when the read
    worked and no off-Studio target exists (Prism 0363103a464a: the two must not look alike). ``PoolConfig.hosts`` is
    the pin vocabulary and can list the Studio, so naming it would not take the work off the Studio (Prism 9c2756fa53d1)."""
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        from hermes_cli import kanban_worker_pool as kwp
        fleet = Path(root) / "fleet"
        if not (fleet / kwp.ROLES_FILE).exists():
            return None  # no pool file: the host check is skipped, as today (operator read 2)
        cfg = kwp.read_pool(fleet, kanban_cfg=kbd._root_kanban_cfg())
        if getattr(cfg, "refused", None):
            return None  # read_pool never raises: a malformed/refused pool is an empty PoolConfig with refused set
        studio = {str(kwp.STUDIO_HOST).lower(), "studio"}
        names = [str(getattr(h, "name", "")).strip().lower() for h in (getattr(cfg, "pool_hosts", ()) or ())]
        return tuple(dict.fromkeys(n for n in names if n and n not in studio))
    except Exception:
        return None


def _names_host(text: str, hosts: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(re.search(rf"(?<![\w-]){re.escape(h)}(?![\w-])", low) for h in hosts)


def _hosts_text(hosts: Optional[tuple]) -> str:
    if hosts is None:
        return "(pool file unreadable)"
    return ", ".join(hosts) or "(none off the Studio)"


def _card_status(conn, card: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
    return row[0] if row else None


_DATED = re.compile(r"^:\d{8}(?:-\d+)?$")   # the dated reopen key, ``-N`` for the Nth closure that day
_GAP_LINE = re.compile(r"(?m)^[ \t]*capability-gap:[ \t]*([^\\\n]*[^\\\s])")


def is_fix_card_for(conn, card: str, key: str) -> bool:
    """A card may close gap ``key`` only when it says so (operator read 3): its idempotency key is
    ``studio-capgap:<key>`` (or the dated reopen key ``studio-capgap:<key>:<yyyymmdd>[-N]``), or its body has the
    line ``capability-gap: <key>`` (the whole rest of the line, or its first word). Both are written by
    :func:`create_command`. Any other open card is refused."""
    row = conn.execute("SELECT idempotency_key, body FROM tasks WHERE id = ?", (card,)).fetchone()
    if not row:
        return False
    idem, body = (row[0] or "").strip(), row[1] or ""
    base = f"studio-capgap:{key}"
    if idem == base or (idem.startswith(base) and _DATED.match(idem[len(base):])):
        return True
    return any(key in (m.group(1), m.group(1).split()[0]) for m in _GAP_LINE.finditer(body))


def key_fix_cards(conn, key: str) -> dict:
    """Fix cards REGISTERED for gap ``key`` (the capability; ``studio-capgap:<key>``): ``{card: event id of its last
    registration}``, oldest first. A registration is a ``studio_override`` on ANY card that recorded
    ``fix_card_given`` for this key with the answers; a ``counted`` run only uses a registration and never moves it
    (Prism be1f77072d51). Values are ``(event id, created_at)``; a card that is not a fix card for ``key``
    (:func:`is_fix_card_for`) is never a registration. These events survive ``kanban gc`` (``gc_events`` keeps a studio_override that names a fix
    card), so a closed gap stays closed past the 30-day event retention (Prism 506ff9d7da6e).
    Per GAP, not per card (operator ruling 2026-11-27): 25 cards overrode one render-toolchain gap on 11-26."""
    import json
    out: dict = {}
    rows = conn.execute("SELECT id, payload, created_at FROM task_events WHERE kind = 'studio_override' ORDER BY id")
    for ev_id, raw, at in rows.fetchall():
        try:
            doc = json.loads(raw or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("capability") != key or doc.get("counted"):
            continue
        fc = doc.get("fix_card_given")
        if fc and is_fix_card_for(conn, fc, key):
            out.pop(fc, None)
            out[fc] = (int(ev_id), int(at or 0))
    return out


def _completed_at(conn, card: str) -> Optional[int]:
    row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (card,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _completed_event(conn, card: str) -> Optional[int]:
    row = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'", (card,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _after_closure(reg: tuple, done_reg: tuple, completed: Optional[int], done_ev: Optional[int] = None) -> bool:
    """Was a registration ``reg`` (event id, created_at) made after the done card closed the gap? Closure is the
    card's ``tasks.completed_at``, never its newest event: a later comment or a gc prune of its events must not move
    it (operator read 4). With no completion time, by registration order only. In the closing second the order is
    the event id against the card's ``completed`` event (t_dfb01891: not its registration, which an older card's
    later registration also follows); with that event gone, a same-second registration is not after the closure."""
    if completed is None:
        return reg[0] > done_reg[0]
    if reg[1] != completed:
        return reg[1] > completed
    return done_ev is not None and reg[0] > done_ev


def _body_needs(conn, task_id: str) -> str:
    from hermes_cli import kanban_host_capabilities as khc
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    lines = [m.group(0).strip() for m in khc.REQUIRES_RE.finditer((row[0] if row else "") or "")]
    return "; ".join(lines) or "no requires: line"


def create_command(capability: str, board: Optional[str] = None, *, dated: Optional[str] = None) -> str:
    """The gap-card create command, on the override's board so the printed command runs in an unpinned shell and
    the next override on that board finds the card (Prism 506ff9d7da6e). After a closure the same idempotency key
    would return the DONE card, so the reopen command carries ``dated`` (yyyymmdd): ``studio-capgap:<key>:<date>``
    (operator ruling 7). Every argument is shell-quoted (ruling 9)."""
    import shlex
    b = f"--board {shlex.quote(board)} " if board else ""
    idem = f"studio-capgap:{capability}" + (f":{dated}" if dated else "")
    title = f"Pool hosts: install what {capability} work needs so it runs off the Studio"
    # no shell shapes (``>``, ``|``...) in the text: an agent pasting this into its terminal is refused them
    body = (f"capability-gap: {capability}\nInstall the tools, models or checkout this work needs on a pool host; "
            f"prove one card of this kind runs there. Skill: fleet-host-gap-handling.")
    return (f"hermes kanban {b}create {shlex.quote(title)} --assignee daedalus --idempotency-key {shlex.quote(idem)} "
            f"--body {shlex.quote(body)}")


STUDIO_HOST_HINT = ("  If the work is ON the Studio machine itself (its disk, containers, services, GUI), use "
                    "requires: studio-host (inherent, no fix card).\n")


def _studio_host_hint(capability: str) -> str:
    from hermes_cli import kanban_studio_allowlist as ksa
    return STUDIO_HOST_HINT if any(ksa.head(p) == "studio-state" for p in ksa.parts(capability)) else ""


class _Refusal(GapRefused):
    """A refusal that knows its gap key and problem, so a repeat within 24 h prints the short form."""

    def __init__(self, text: str, *, key: str, problem: str, fix_card: Optional[str] = None,
                 short: Optional[str] = None):
        super().__init__(text)
        self.key, self.problem, self.fix_card, self.short = key, problem, fix_card, short


def _refusal(conn, task_id: str, capability: str, problem: str, hosts: tuple,
             board: Optional[str] = None) -> GapRefused:
    return _Refusal(
        f"{problem}\n"
        f"  {task_id} wants the Studio for '{capability}', which is a host GAP (not Studio-only by nature). "
        f"The card says: {_body_needs(conn, task_id)}.\n"
        f"{_studio_host_hint(capability)}"
        f"  Pool hosts: {_hosts_text(hosts)}.\n"
        f"  A gap override needs: --elsewhere \"<which pool host, what to install or move>\" "
        f"--why-not-now \"<why it cannot run there now>\" --effort hours|days; "
        f"with hours also --fix-card <open card>.\n"
        f"  Create the gap card: {create_command(capability, board)}",
        key=capability, problem=problem,
        short=f"(gap {capability}; fix card: none; create: hermes kanban "
              f"{'--board ' + board + ' ' if board else ''}create ... --idempotency-key studio-capgap:{capability})")


REPEAT_WINDOW = 24 * 3600
REFUSALS_MAX_BYTES = 256 * 1024   # rotated to the last REPEAT_WINDOW when larger (ruling 9): it is read on refusal
REFUSALS_FILE = ("state", "studio-capability-gap", "gap-refusals.jsonl")


def refusals_path(root: Path) -> Path:
    return Path(root).joinpath(*REFUSALS_FILE)


def _shorten(exc: GapRefused, task_id: str, root: Path, now: float) -> GapRefused:
    """A refusal identical (gap key + problem) to one printed in the last 24 h prints one line plus the fix card,
    not the full screen (operator ruling 2026-11-27). Every refusal is appended to ``gap-refusals.jsonl``."""
    import json
    if not isinstance(exc, _Refusal):
        return exc
    path = refusals_path(root)
    earlier = None
    keep: list = []
    try:
        raw = path.read_bytes()
    except OSError:
        raw = b""
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        try:
            doc = json.loads(line)
            at = float(doc.get("at") or 0) if isinstance(doc, dict) else None
        except (ValueError, TypeError, AttributeError):
            continue  # one bad line must never turn a refusal into a traceback (operator read 5)
        if at is None or not math.isfinite(at) or now - at >= REPEAT_WINDOW:  # NaN/Infinity/1e999 (t_dfb01891)
            continue
        keep.append(line)
        if doc.get("key") == exc.key and doc.get("problem") == exc.problem:
            earlier = doc
    if len(raw) > REFUSALS_MAX_BYTES:
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": int(now), "key": exc.key, "problem": exc.problem, "task_id": task_id,
                                 "fix_card": exc.fix_card}, sort_keys=True) + "\n")
    except OSError:
        pass
    if earlier is None:
        return exc
    first = exc.problem.splitlines()[0]
    tail = exc.short or f"(gap {exc.key}; fix card: {exc.fix_card or 'none'})"
    return _Refusal(f"{first} {tail} [repeat; full screen printed for {earlier.get('task_id')} "
                    f"{int((now - float(earlier.get('at') or 0)) // 60)} min ago]",
                    key=exc.key, problem=exc.problem, fix_card=exc.fix_card, short=exc.short)


def check(conn, task_id: str, capability: str, *, elsewhere: Optional[str], why_not_now: Optional[str],
          effort: Optional[str], fix_card: Optional[str], until: Optional[str] = None, allow: Iterable[str] = (),
          root: Path, board: Optional[str] = None, now: Optional[float] = None) -> dict:
    """``{}`` when the gate is off; ``{"class": "inherent"}`` for an inherent capability; for a gap the recorded
    answers. Raises :class:`GapRefused` with the operator screen (one line when it repeats within 24 h)."""
    import time
    if disabled(root):
        return {}
    cls = classify(capability, allow=allow, studio_only=studio_only_caps(root))
    if cls == INHERENT:
        return {"class": INHERENT}
    try:
        return _check_gap(conn, task_id, capability, elsewhere=elsewhere, why_not_now=why_not_now, effort=effort,
                          fix_card=fix_card, until=until, root=root, board=board)
    except GapRefused as exc:
        raise _shorten(exc, task_id, root, time.time() if now is None else now) from None


def _reopen_day(conn, key: str) -> str:
    """``yyyymmdd`` for the reopen key, or ``yyyymmdd-N`` when a DONE card already holds that day's key: the gap
    closed twice in one day, and the first dated key would return that day's DONE card (t_dfb01891). A key held by
    an open card is reused, so a repeated refusal names that card instead of minting a duplicate (Prism c10a5eb0769c)."""
    import time
    day = time.strftime("%Y%m%d", time.localtime())
    base = f"studio-capgap:{key}:{day}"
    done = {}
    for idem, status in conn.execute("SELECT idempotency_key, status FROM tasks WHERE substr(idempotency_key, 1, ?) = ? "
                                     "AND status != 'archived'", (len(base), base)).fetchall():
        done[idem] = done.get(idem, True) and status == "done"
    n = 1
    while done.get(base if n == 1 else f"{base}-{n}"):
        n += 1
    return day if n == 1 else f"{day}-{n}"


def _reopen(conn, key: str, board: Optional[str]) -> str:
    day = _reopen_day(conn, key)
    return ("If the gap has reopened, create a new fix card (dated key: the old one returns the done card) and name "
            f"it with --fix-card: {create_command(key, board, dated=day)}")


def _check_gap(conn, task_id: str, key: str, *, elsewhere, why_not_now, effort, fix_card, until, root, board) -> dict:
    hosts = pool_hosts(root)
    on_pool = f"run it on a pool host: {_hosts_text(hosts)}"
    reg = key_fix_cards(conn, key)
    known = list(reg)
    status = {c: _card_status(conn, c) for c in known}
    completed = {c: _completed_at(conn, c) for c in known if status[c] in _DONE}
    done_known = sorted(completed, key=lambda c: (completed[c] if completed[c] is not None else reg[c][1], reg[c][0]))
    # Only a fix card registered AFTER every closure can count a run (Prism 434fc10f5122, be1f77072d51): an older
    # card left open when another one closed the gap predates the closure.
    done_ev = {d: _completed_event(conn, d) for d in done_known}
    since = [c for c in known if all(_after_closure(reg[c], reg[d], completed[d], done_ev[d]) for d in done_known)]
    open_known = [c for c in since if status[c] not in _CLOSED and c != task_id]
    ew, wn, eff = (elsewhere or "").strip(), (why_not_now or "").strip(), (effort or "").strip().lower()
    fc = (fix_card or "").strip() or None
    if fc:
        st = _card_status(conn, fc)
        if st is None:
            raise _refusal(conn, task_id, key, f"--fix-card {fc}: no such card", hosts, board)
        if fc == task_id:
            raise _refusal(conn, task_id, key, "--fix-card must be the card that closes the gap, not this card",
                           hosts, board)
        if not is_fix_card_for(conn, fc, key):
            raise _refusal(conn, task_id, key, f"--fix-card {fc} is not a gap card for {key}: its idempotency key "
                           f"must be studio-capgap:{key} or its body must carry the line 'capability-gap: {key}'",
                           hosts, board)
        if st in _DONE:
            p = f"--fix-card {fc} is done: the gap {key} is closed"
            raise _Refusal(f"{p}, {on_pool}. {_reopen(conn, key, board)}", key=key, problem=p, fix_card=fc,
                           short=f"({on_pool})")
        if st in _CLOSED:
            raise _refusal(conn, task_id, key, f"--fix-card {fc} is {st}, not open: name an open gap card",
                           hosts, board)
    # An open fix card for this gap already exists (named earlier on any card): this run is counted on it.
    counted = fc if fc in open_known else (open_known[-1] if not fc and open_known else None)
    if counted:
        if until:
            raise _Refusal(f"gap {key} is being closed by {counted}: an override covers ONE run, drop --until {until}",
                           key=key, problem="until on a counted gap", fix_card=counted)
        return {"class": GAP, "elsewhere": ew or None, "why_not_now": wn or None, "effort": eff or None,
                "fix_card_given": counted, "counted": True}
    if not fc and done_known:
        p = f"the gap card {done_known[-1]} for {key} is done: the gap is closed"
        raise _Refusal(f"{p}, {on_pool}. {_reopen(conn, key, board)}", key=key, problem=p, fix_card=done_known[-1],
                       short=f"({on_pool})")
    if len(ew) < MIN_TEXT:
        raise _refusal(conn, task_id, key, f"--elsewhere is required ({MIN_TEXT}+ characters)", hosts, board)
    if hosts == ():
        raise _refusal(conn, task_id, key, "the pool file has no off-Studio placement target, so no --elsewhere "
                       "can name one: add a pool host first", hosts, board)
    if hosts and not _names_host(ew, hosts):
        raise _refusal(conn, task_id, key, "--elsewhere must name the pool host that would run it "
                       "(it only restates the gap)", hosts, board)
    if len(wn) < MIN_TEXT:
        raise _refusal(conn, task_id, key, f"--why-not-now is required ({MIN_TEXT}+ characters)", hosts, board)
    if eff not in EFFORTS:
        raise _refusal(conn, task_id, key, "--effort is required: hours or days", hosts, board)
    if eff == "hours" and not fc:
        raise _refusal(conn, task_id, key, "--effort hours: setting it up elsewhere is the default, so name the "
                       "open gap card with --fix-card (this override then covers this run only)", hosts, board)
    if eff == "hours" and until:
        raise _Refusal(f"--effort hours covers ONE run: drop --until {until} (override again for the next run "
                       f"while {fc} is open)", key=key, problem="until with effort hours", fix_card=fc)
    return {"class": GAP, "elsewhere": ew, "why_not_now": wn, "effort": eff, "fix_card_given": fc}
