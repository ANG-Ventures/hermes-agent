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
  refusal prints the create command. It covers ONE run (``--until`` is refused); a second override for the same gap
  passes while that card is open.
* days: passes with the answers recorded; the digest line lists it so the estimate can be challenged.
* a card whose earlier override named a fix card that is now done: the gap is closed, every gap override is refused.

All answers go in the ``studio_override`` event and the overrides.jsonl ledger line.

Kill switch: the file ``<root>/state/studio-capability-gap/gap-gate.off`` (any content). While it exists the
override behaves exactly as before this module. Cost: one sqlite read per gap override plus one map read; the
``create`` and ``admit`` paths are untouched.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

KILL_FILE = ("state", "studio-capability-gap", "gap-gate.off")
STAND_INS = frozenset({"operator_admit", "pool_unavailable", "review_lane", "browser_join"})
STRUCTURAL = frozenset({"workspace_kind"})
EFFORTS = ("hours", "days")
MIN_TEXT = 20
INHERENT, GAP = "inherent", "gap"
_CLOSED = ("done", "archived")


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
        return frozenset(ksc.studio_only(cmap) or ())
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


def pool_hosts(root: Path) -> tuple:
    """Pool host names from the root's pool file (not reach aliases: those include the Studio), lower-case, in
    pool order; () when unreadable."""
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        from hermes_cli import kanban_worker_pool as kwp
        cfg = kwp.read_pool(Path(root) / "fleet", kanban_cfg=kbd._root_kanban_cfg())
        names = [str(n).strip().lower() for n in (getattr(cfg, "hosts", ()) or ())]
        return tuple(dict.fromkeys(n for n in names if n))
    except Exception:
        return ()


def _names_host(text: str, hosts: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(re.search(rf"(?<![\w-]){re.escape(h)}(?![\w-])", low) for h in hosts)


def _card_status(conn, card: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
    return row[0] if row else None


def _closed_fix_cards(conn, task_id: str) -> list:
    """Fix cards earlier overrides on ``task_id`` named that are now done/archived."""
    import json
    out = []
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'studio_override' "
                               "ORDER BY id", (task_id,)):
        try:
            fc = (json.loads(raw or "{}") or {}).get("fix_card_given")
        except (TypeError, ValueError, AttributeError):
            fc = None
        if fc and fc not in out and _card_status(conn, fc) in _CLOSED:
            out.append(fc)
    return out


def _body_needs(conn, task_id: str) -> str:
    from hermes_cli import kanban_host_capabilities as khc
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    lines = [m.group(0).strip() for m in khc.REQUIRES_RE.finditer((row[0] if row else "") or "")]
    return "; ".join(lines) or "no requires: line"


def create_command(capability: str) -> str:
    return (f'hermes kanban create "Pool hosts: install what {capability} work needs so it runs off the Studio" '
            f'--assignee daedalus --idempotency-key studio-capgap:{capability} '
            f'--body "capability-gap: {capability}\\nInstall <tools/models/checkout> on <pool host>; prove one card '
            f'of this kind runs there. Skill: fleet-host-gap-handling."')


def _refusal(conn, task_id: str, capability: str, problem: str, hosts: tuple) -> GapRefused:
    return GapRefused(
        f"{problem}\n"
        f"  {task_id} wants the Studio for '{capability}', which is a host GAP (not Studio-only by nature). "
        f"The card says: {_body_needs(conn, task_id)}.\n"
        f"  Pool hosts: {', '.join(hosts) or '(pool file unreadable)'}.\n"
        f"  A gap override needs: --elsewhere \"<which pool host, what to install or move>\" "
        f"--why-not-now \"<why it cannot run there now>\" --effort hours|days; with hours also --fix-card <open card>.\n"
        f"  Create the gap card: {create_command(capability)}")


def check(conn, task_id: str, capability: str, *, elsewhere: Optional[str], why_not_now: Optional[str],
          effort: Optional[str], fix_card: Optional[str], until: Optional[str] = None, allow: Iterable[str] = (),
          root: Path) -> dict:
    """``{}`` when the gate is off; ``{"class": "inherent"}`` for an inherent capability; for a gap the recorded
    answers. Raises :class:`GapRefused` with the operator screen."""
    if disabled(root):
        return {}
    cls = classify(capability, allow=allow, studio_only=studio_only_caps(root))
    if cls == INHERENT:
        return {"class": INHERENT}
    hosts = pool_hosts(root)
    closed = _closed_fix_cards(conn, task_id)
    if closed:
        raise GapRefused(f"the gap card {', '.join(closed)} for {task_id} is done: the gap is closed, run the card on "
                         f"a pool host (pool hosts: {', '.join(hosts) or '(pool file unreadable)'})")
    ew, wn, eff = (elsewhere or "").strip(), (why_not_now or "").strip(), (effort or "").strip().lower()
    if len(ew) < MIN_TEXT:
        raise _refusal(conn, task_id, capability, f"--elsewhere is required ({MIN_TEXT}+ characters)", hosts)
    if hosts and not _names_host(ew, hosts):
        raise _refusal(conn, task_id, capability, "--elsewhere must name the pool host that would run it "
                       "(it only restates the gap)", hosts)
    if len(wn) < MIN_TEXT:
        raise _refusal(conn, task_id, capability, f"--why-not-now is required ({MIN_TEXT}+ characters)", hosts)
    if eff not in EFFORTS:
        raise _refusal(conn, task_id, capability, "--effort is required: hours or days", hosts)
    fc = (fix_card or "").strip() or None
    if fc:
        status = _card_status(conn, fc)
        if status is None:
            raise _refusal(conn, task_id, capability, f"--fix-card {fc}: no such card", hosts)
        if status in _CLOSED:
            raise GapRefused(f"--fix-card {fc} is {status}: the gap is closed, run {task_id} on a pool host "
                             f"(pool hosts: {', '.join(hosts) or '(pool file unreadable)'})")
        if fc == task_id:
            raise _refusal(conn, task_id, capability, "--fix-card must be the card that closes the gap, not this card",
                           hosts)
    if eff == "hours" and not fc:
        raise _refusal(conn, task_id, capability, "--effort hours: setting it up elsewhere is the default, so name the "
                       "open gap card with --fix-card (this override then covers this run only)", hosts)
    if eff == "hours" and until:
        raise GapRefused(f"--effort hours covers ONE run: drop --until {until} (override again for the next run "
                         f"while {fc} is open)")
    return {"class": GAP, "elsewhere": ew, "why_not_now": wn, "effort": eff, "fix_card_given": fc}

