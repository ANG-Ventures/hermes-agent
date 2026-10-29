"""Fail-fast placement loop (Fleet Work Placement Phase 2, t_ecfd5591).

Spec: Obsidian ``AI/Infrastructure/Fleet Work Allocation -- Host Affinity and
Continuation (spec, 2026-10-11).md`` v2 s3 and s7 Phase 2; review findings F1
and F14 of its MOMUS pass 1.

A placed worker that finds its host lacks a capability the card needs ends
with one ``<TOKEN>_ABSENT cap=<cap> host=<host>`` line. This module decides
what the board does with it, from the card's own ``placement_reroute`` event
ledger and never from ``block_recurrences`` (F1: the first block sets that
counter to 1 and a second same-kind block lands in ``triage``):

* no prior ``placement_reroute`` event: append ``requires: <cap>`` to the
  body (visible; a human undoes it by deleting the line), comment, write
  ``placement_reroute {from, cap, run_id, token}`` and requeue the card
  ``ready``. The next route keeps it on a capable host.
* one or more: the map or the derivation is wrong. Park ``needs_input``
  with both lines quoted. Never ``triage``, never a loop.

An unplaced worker (it already runs on the dispatcher host) has nowhere to
be re-routed to, so it parks ``needs_input`` with the line verbatim, the
#1876 behaviour.

The gates that produce the line:

* Layer 0, worker boot (:func:`boot_check`): before the first model call,
  re-derive the card's HARD requirements from its current body, skills and
  the current map when the route hash the dispatcher stamped no longer
  matches (a stale route), else trust the stamped list. Exit on a miss.
* Layer 0b, dispatch (:func:`attached_skill_gap`): the dispatcher never
  spawns a card on a host lacking a capability its ATTACHED skills need.
* Layer 1, mid-run ``skill_view`` (:func:`skill_gate`): the tool returns the
  token line instead of the skill body. It is applied only in the model's
  tool handler, never at the ``--skills`` preload (F14: a gated preload
  would count as "missing" and ``cli.py`` continues without it); the
  preload's own guard is Layer 0, which exits the worker.

Only ENFORCED capabilities gate (``KANBAN_PLACEMENT_CAPS`` decides per
capability; ``warn``/``off`` are inert here).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

ABSENT_RE = re.compile(r"^([A-Z][A-Z0-9_]*_ABSENT)\b")  # token_for() alphabet: A-Z 0-9 _
REROUTE_EVENT = "placement_reroute"
GATE_REFUSED_EVENT = "placement_gate_refused"
REROUTE_OUTCOME = "rerouted"
ROUTE_HASH_KEY = "route_hash"
REQUIRES_KEY = "requires"

# #1876's spelling for the two rail tokens (spec s3, one token family).
_TOKEN_OF_CAP = {"browser-session": "BROWSER_RAIL_ABSENT", "screen": "SCREEN_RAIL_ABSENT"}
_CAP_OF_TOKEN = {v: k for k, v in _TOKEN_OF_CAP.items()}
_FIELD_RE = re.compile(r"\b(cap|host)=([A-Za-z0-9._-]+)")


# -- the token line -------------------------------------------------------------

def token_for(cap: str) -> str:
    """The token for a capability; always matches :data:`ABSENT_RE` (round-trip tested)."""
    return _TOKEN_OF_CAP.get(cap) or re.sub(r"[^A-Z0-9]", "_", cap.upper()).lstrip("_0123456789") + "_ABSENT"


def absent_line(cap: str, host: str) -> str:
    """The one line a gated worker ends with (spec s3)."""
    return (f"{token_for(cap)} cap={cap} host={host} need=studio: this card needs {cap}, which "
            f"{host} does not have. The pool will re-route it once; to pin by hand add the line "
            f"`requires: {cap}`. Do not retry here.")


def parse_absent(message: Optional[str]) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """``(token, cap, host)`` for a line matching ``^[A-Z_]+_ABSENT``, else None."""
    text = str(message or "").strip()
    m = ABSENT_RE.match(text)
    if not m:
        return None
    token = m.group(1)
    fields = dict(_FIELD_RE.findall(text.split("\n", 1)[0]))
    cap = fields.get("cap") or _CAP_OF_TOKEN.get(token)
    if cap is None:
        cap = token[: -len("_ABSENT")].lower().replace("_", "-") or None
    host = fields.get("host")
    if host is None:
        # #1876's rail line: "BROWSER_RAIL_ABSENT: no browser session on <host>. ..."
        hm = re.search(r"\bon ([A-Za-z0-9._-]+?)\.(?:\s|$)", text)
        host = hm.group(1) if hm else None
    return token, cap, host


# -- the board decision (F1) ----------------------------------------------------

def prior_reroutes(conn, task_id: str) -> int:
    """This card's re-route ledger: its ``placement_reroute`` events."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, REROUTE_EVENT)).fetchone()[0])


def _with_requires_line(body: Optional[str], cap: str) -> str:
    from hermes_cli.kanban_host_capabilities import declared_words
    text = body or ""
    if cap in declared_words(text):
        return text
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + f"requires: {cap}\n"


def reroute_or_park(conn, task_id: str, run_id: int, message: str, *,
                    placed_host: Optional[str],
                    placement_raw: Optional[str] = None) -> Optional[str]:
    """Apply the board decision for one ``*_ABSENT`` exit of run ``run_id``.

    A first miss re-routes only when the placed host proves the run's
    workspace (``placement_raw``) empty; otherwise it parks ``needs_input``
    for recovery with host/run/workspace named (``placement_custody_hold``).

    Returns ``"rerouted"``, ``"needs_input"`` or None (not an ABSENT line, or
    the run no longer owns the card: a stale worker never mutates it).
    """
    from hermes_cli import kanban_db as kb
    parsed = parse_absent(message)
    if parsed is None:
        return None
    from hermes_cli import kanban_worker_hosts as kwh
    token, cap, host = parsed
    host = host or placed_host
    if token in _PARK_ONLY_TOKENS or not kwh.is_pool_host(placed_host):
        cap = None          # nothing re-routable was proven missing, or not a pool placement: park
    if cap and prior_reroutes(conn, task_id) == 0:
        # Workspaces are host-local (no sync): a mid-run tool, or an earlier run on this host,
        # may have left work there that a re-route would strand (0e9694bf2944). Re-route only a
        # workspace the host proves empty; anything else parks for recovery, source untouched.
        if kwh.remote_workspace_has_content(placement_raw):
            return _custody_hold(conn, task_id, int(run_id), message, cap=cap, host=host,
                                 token=token, placement_raw=placement_raw)
    if cap:
        decided = None
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT status, current_run_id, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
                return None
            if prior_reroutes(conn, task_id) == 0:
                body = _with_requires_line(row["body"], cap)
                # A reviewer run goes back to the review queue, never to ready (#1963 Prism).
                back_to = kb._retry_status_for_run(conn, task_id, int(run_id))
                conn.execute(
                    "UPDATE tasks SET status = ?, body = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL WHERE id = ? AND current_run_id = ?",
                    (back_to, body, task_id, int(run_id)))
                kb._end_run(conn, task_id, outcome=REROUTE_OUTCOME, status=REROUTE_OUTCOME,
                            summary=message.split("\n", 1)[0])
                kb._append_event(conn, task_id, REROUTE_EVENT,
                                 {"from": host, "cap": cap, "run_id": int(run_id), "token": token,
                                  "to": back_to},
                                 run_id=int(run_id))
                kb.add_comment(conn, task_id, "dispatcher",
                               f're-routed: {cap}, ran on {host}; I added "requires: {cap}" to the body. '
                               f"Delete that line to undo.")
                decided = "rerouted"
        if decided:
            return decided
        first = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id LIMIT 1",
            (task_id, REROUTE_EVENT)).fetchone()
        prev = json.loads(first["payload"] or "{}") if first else {}
        reason = (f"{message.split(chr(10), 1)[0]}\n"
                  f"Second placement miss: the first ({prev.get('token')} cap={prev.get('cap')} "
                  f"host={prev.get('from')}) was re-routed once already, so the capability map or "
                  f"the derivation is wrong. Fix fleet/host-capabilities.json or the skill index, "
                  f"or pin the card by hand.")
    else:
        reason = message
    # The re-route ledger, not block_recurrences, decided this: the loop
    # breaker must not turn a placement miss into triage (F1).
    if kb.block_task(conn, task_id, reason=reason, kind="needs_input",
                     expected_run_id=int(run_id), loop_breaker=False):
        return "needs_input"
    return None


CUSTODY_EVENT = "placement_custody_hold"


def _custody_hold(conn, task_id: str, run_id: int, message: str, *, cap: str, host: Optional[str],
                  token: str, placement_raw: Optional[str]) -> Optional[str]:
    """Park a mid-run miss whose workspace may hold work: name host, run and workspace."""
    from hermes_cli import kanban_db as kb
    try:
        workspace = (json.loads(placement_raw or "").get("env") or {}).get("TERMINAL_CWD")
    except (ValueError, AttributeError):
        workspace = None
    reason = (f"{message.split(chr(10), 1)[0]}\n"
              f"Placement custody hold: run {run_id} on {host} may have left work in its workspace "
              f"{workspace or '(unknown path)'} on {host}. Workspaces are host-local (no sync), so the "
              f"pool cannot move that work to another host. Recover or discard it on {host}, add "
              f"`requires: {cap}` to pin the next run, then unblock.")
    if not kb.block_task(conn, task_id, reason=reason, kind="needs_input",
                         expected_run_id=int(run_id), loop_breaker=False):
        return None
    # The park committed (its reason names host/run/workspace); the event is diagnostic. A
    # failure here must not report the committed decision as unhandled (35b39eb7244e).
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, CUSTODY_EVENT,
                             {"host": host, "run_id": int(run_id), "workspace": workspace, "cap": cap,
                              "token": token}, run_id=int(run_id))
    except Exception:
        _log.warning("placement custody hold committed; its %s event was not written", CUSTODY_EVENT,
                     exc_info=True)
    return "needs_input"


def worker_exit_on_absent(message: str) -> Optional[str]:
    """The owning worker's exit hook for an ABSENT line (#1876's fenced path)."""
    try:
        if parse_absent(message) is None:
            return None
        from agent.delegation_context import owned_kanban_task
        task_id = owned_kanban_task()
        if not task_id:
            return None
        try:
            run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
        except ValueError:
            return None
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_worker_hosts as kwh
        placed = kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))
        conn = kb.connect()
        try:
            return reroute_or_park(conn, task_id, run_id, message, placed_host=placed,
                                   placement_raw=os.environ.get(kwh.PLACEMENT_ENV))
        finally:
            conn.close()
    except Exception:
        _log.warning("placement fail-fast exit failed", exc_info=True)
        return None


# -- capability reads shared by the gates -----------------------------------------

def caps_modes(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    """``KANBAN_PLACEMENT_CAPS`` as ``{cap|*: mode}``; ``{}`` when absent or bad."""
    from hermes_cli.kanban_host_capabilities import CAPS_ENV
    raw = (os.environ if environ is None else environ).get(CAPS_ENV)
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def enforced(cap: str, modes: Mapping[str, str]) -> bool:
    from hermes_cli.kanban_host_capabilities import ENFORCE
    return modes.get(cap, modes.get("*")) == ENFORCE


GATE_WARN_EVENT = "placement_gate_warn"


def note_gate_warn(cap: str, message: str, modes: Mapping[str, str]) -> None:
    """A gate that would have refused under ``enforce``: one run-scoped event, no block."""
    try:
        from hermes_cli.kanban_worker_route import _append_run_event
        _append_run_event(GATE_WARN_EVENT, {"cap": cap, "mode": modes.get(cap, modes.get("*")),
                                            "line": str(message).split("\n", 1)[0][:200]})
    except Exception:
        _log.debug("placement_gate_warn event failed", exc_info=True)


def _fleet_dir() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "fleet"


def _fleet_bytes(fleet_dir: Path) -> Tuple[Optional[bytes], Optional[bytes]]:
    from hermes_cli.kanban_host_capabilities import INDEX_FILE, MAP_FILE
    out = []
    for name in (MAP_FILE, INDEX_FILE):
        try:
            out.append((Path(fleet_dir) / name).read_bytes())
        except OSError:
            out.append(None)
    return out[0], out[1]


def route_hash(body: Optional[str], skills: Iterable[str], fleet_dir: Path, *,
               snapshot: Optional[Tuple[Optional[bytes], Optional[bytes]]] = None) -> str:
    """sha256 over (body, skills, map bytes, index bytes). ``snapshot`` = the bytes the
    requirements were derived from, so stamp and hash can never describe different files."""
    h = hashlib.sha256()
    h.update((body or "").encode("utf-8"))
    h.update(b"\0" + json.dumps(sorted(s for s in skills or () if isinstance(s, str))).encode("utf-8"))
    for blob in (snapshot if snapshot is not None else _fleet_bytes(fleet_dir)):
        h.update(b"\0" + (blob if blob is not None else b"-"))
    return h.hexdigest()


def snapshot_map(snapshot: Tuple[Optional[bytes], Optional[bytes]]):
    """The capability map parsed from one captured (map, index) byte pair, never from the
    files again: every gate evaluates exactly the bytes it hashed (23e10f0f6800)."""
    from hermes_cli import kanban_host_capabilities as khc
    return khc.map_from_bytes(*snapshot)


def hard_requirements(cmap, body: Optional[str], skills: Iterable[str]) -> List[str]:
    """Declared + attached-skill capabilities (HARD, s2.2) under ``cmap``. A word the map
    does not define stays ``unknown:<word>`` (F4): an unresolved requirement is still a
    requirement, never read as "needs nothing" (470af3484c95). [] for an absent/invalid map."""
    from hermes_cli import kanban_host_capabilities as khc
    if cmap is None or not cmap.valid:
        return []
    reqs, _none = khc.derive(body, skills, cmap)
    return sorted({r.cap for r in reqs if r.strength == khc.HARD})


def decide(cmap, host: str, requirements: Iterable[str], modes: Mapping[str, str]) -> Optional[str]:
    """THE refusal decision for boot, preload and mid-run (t_ecfd5591 682bd83da2ba): the first
    refusal line for ``requirements`` on ``host`` under ``cmap``, else None. An unresolved
    ``unknown:<word>`` parks before any routable known miss (470af3484c95): a re-route can never
    satisfy a word no host serves. An absent/invalid map gates nothing (F11)."""
    from hermes_cli.kanban_host_capabilities import UNKNOWN_PREFIX
    if cmap is None or not cmap.valid:
        return None
    have = cmap.hosts.get(host.lower(), frozenset())
    for cap in sorted(dict.fromkeys(requirements), key=lambda c: not c.startswith(UNKNOWN_PREFIX)):
        if (line := _refusal(cap, have, host, modes)) is not None:
            return line
    return None


def _skill_key(name: str) -> str:
    from hermes_cli.kanban_host_capabilities import skill_key
    return skill_key(name)


_SNAPSHOT_TRIES = 5
_SNAPSHOT_BACKOFF_S = 0.05          # attempt n sleeps n * this before re-reading (bounded: <= 0.5 s)
INDETERMINATE_TOKEN = "PLACEMENT_INDETERMINATE_ABSENT"
UNRESOLVED_TOKEN = "PLACEMENT_UNRESOLVED_ABSENT"
_PARK_ONLY_TOKENS = frozenset({INDETERMINATE_TOKEN, UNRESOLVED_TOKEN})


def unresolved_line(word: str, host: str) -> str:
    """An enforcing worker's refusal of a hard requirement the map does not define (no host can
    prove it, so it parks ``needs_input``; the dispatcher holds such a card the same way, F4)."""
    return (f"{UNRESOLVED_TOKEN} cap=placement-unresolved host={host} need={word}: this card requires "
            f"\"{word}\", which fleet/host-capabilities.json does not define, so no host can be shown "
            f"to serve it. Fix the word or the map, then unblock.")


def _refusal(cap: str, have, host: str, modes: Mapping[str, str]) -> Optional[str]:
    from hermes_cli.kanban_host_capabilities import UNKNOWN_PREFIX
    if cap.startswith(UNKNOWN_PREFIX):
        return unresolved_line(cap[len(UNKNOWN_PREFIX):], host) if _any_enforced(modes) else None
    if cap not in have and enforced(cap, modes):
        return absent_line(cap, host)
    return None


def indeterminate_line(host: str) -> str:
    """The refusal an ENFORCED gate returns when it could not read one consistent map + index
    (t_ecfd5591 review 62b835e510f0). Matches :data:`ABSENT_RE`; the board parks it
    ``needs_input`` and never re-routes on it (no capability was proven missing)."""
    return (f"{INDETERMINATE_TOKEN} cap=placement-indeterminate host={host}: the fleet capability map "
            f"kept changing across {_SNAPSHOT_TRIES} reads, so this worker could not prove {host} "
            f"serves the card's enforced capabilities. Refusing instead of guessing; re-run once "
            f"fleet/host-capabilities.json and fleet/skill-host-requires.json are settled.")


def _consistent(fleet_dir: Path, check, *, on_exhausted):
    """Run ``check(snapshot)`` on one captured (map, index) byte pair. ``check`` parses only
    those bytes (:func:`snapshot_map`), so no file replaced during the evaluation can reach it
    (23e10f0f6800). The re-read afterwards only detects a pair torn between the two file reads
    or republished meanwhile (#1963 Prism r2); ``--sync`` / ``--set`` replace each file
    atomically, so a retry sees whole files. Not a transaction with other writers. Bounded: after
    ``_SNAPSHOT_TRIES`` changing reads NO attempt's result is trusted, a None (admit) as little
    as an ABSENT; the caller's ``on_exhausted()`` decides (an enforced gate refuses)."""
    import time
    for attempt in range(_SNAPSHOT_TRIES):
        if attempt:
            time.sleep(_SNAPSHOT_BACKOFF_S * attempt)
        before = _fleet_bytes(fleet_dir)
        result = check(before)
        if _fleet_bytes(fleet_dir) == before:
            return result
    _log.warning("placement gate: fleet map kept changing across %d reads; no consistent snapshot",
                 _SNAPSHOT_TRIES)
    return on_exhausted()


def _any_enforced(modes: Mapping[str, str]) -> bool:
    from hermes_cli.kanban_host_capabilities import ENFORCE
    return any(v == ENFORCE for v in modes.values())


# -- Layer 0b: dispatch (F14) -------------------------------------------------------

def attached_skill_gap(skills: Iterable[str], host: str, caps) -> Optional[str]:
    """The first enforced capability an ATTACHED skill needs that ``host`` lacks.

    ``caps`` is the tick's :class:`CapabilityTick` (None = layer off).
    """
    if caps is None or not caps.cmap.valid or caps.cmap.index_error is not None:
        return None
    from hermes_cli import kanban_host_capabilities as khc
    have = caps.cmap.hosts.get(host.lower(), frozenset())
    for s in dict.fromkeys(skills or ()):
        if not isinstance(s, str):
            continue
        for cap in caps.cmap.index.get(_skill_key(s), ()):
            if cap not in have and caps.mode_of(cap) == khc.ENFORCE:
                return cap
    return None


# -- Layer 0: worker boot (F1) --------------------------------------------------------

def placement_record(task, fleet_dir: Path) -> dict:
    """What the dispatcher adds to ``KANBAN_WORKER_PLACEMENT`` for Layer 0:
    the card's HARD requirements and the hash of what they were derived from."""
    skills = tuple(s for s in (getattr(task, "skills", None) or ()) if isinstance(s, str))
    body = getattr(task, "body", None)
    # Requirements and hash come from ONE captured byte pair, so the stamp can never pair
    # requirements with a hash of other files (#1963 Prism, 23e10f0f6800). Files that change
    # after the capture change the hash, and the worker re-derives at boot.
    snapshot = _fleet_bytes(fleet_dir)
    reqs = hard_requirements(snapshot_map(snapshot), body, skills)
    return {REQUIRES_KEY: reqs, ROUTE_HASH_KEY: route_hash(body, skills, fleet_dir, snapshot=snapshot)}


def stamp_placement(env: dict, task, fleet_dir: Path) -> None:
    """Add :func:`placement_record` to an already-applied placement env."""
    from hermes_cli import kanban_worker_hosts as kwh
    try:
        doc = json.loads(env.get(kwh.PLACEMENT_ENV) or "")
    except ValueError:
        return
    if isinstance(doc, dict):
        doc.update(placement_record(task, fleet_dir))
        env[kwh.PLACEMENT_ENV] = json.dumps(doc, sort_keys=True)


def boot_check(task_id: str, environ: Optional[Mapping[str, str]] = None, *,
               conn=None, fleet_dir: Optional[Path] = None,
               preloads: Iterable[str] = ()) -> Optional[str]:
    """The ABSENT line when this placed worker's host lacks an enforced hard
    requirement, else None. Zero model calls. A stale route (hash mismatch)
    is re-derived from the CURRENT body and map, never trusted from env.

    ``preloads``: the skills this process will actually render before the first model call
    (``skills.auto_load`` + ``--skills``), checked within the same snapshot as the card's
    requirements, so a skill that is not on the card cannot slip past (fbadb37df1b5)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_hosts as kwh
    env = os.environ if environ is None else environ
    raw = env.get(kwh.PLACEMENT_ENV)
    try:
        record = json.loads(raw) if raw else None
    except ValueError:
        record = None
    record = record if isinstance(record, dict) else {}
    # The runtime path uses the placement captured at process start, like the mid-run gate and
    # the exit seam; the env record may since have been dropped or rewritten (0c0e383033ed). An
    # explicit ``environ`` is a complete fixture and speaks for itself.
    host = (kwh.BOOT_PLACED_HOST if environ is None else None) or record.get("host")
    if not kwh.is_pool_host(host):
        return None
    if record.get("host") != host:
        record = {}         # a stamp for another (or no) placement is not trusted: re-derive
    modes = caps_modes(env)
    if not modes:
        return None
    fleet_dir = Path(fleet_dir) if fleet_dir is not None else _fleet_dir()
    own = conn is None
    conn = kb.connect() if own else conn
    try:
        row = conn.execute("SELECT body, skills FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        if own:
            conn.close()
    if row is None:
        return None
    try:
        skills = [s for s in (json.loads(row["skills"] or "[]") or []) if isinstance(s, str)]
    except (ValueError, TypeError):
        skills = []
    stamped = record.get(REQUIRES_KEY)
    preloaded = [s for s in dict.fromkeys(preloads or ()) if isinstance(s, str)]

    def check(snapshot):
        cmap = snapshot_map(snapshot)
        if (isinstance(stamped, list) and record.get(ROUTE_HASH_KEY)
                == route_hash(row["body"], skills, fleet_dir, snapshot=snapshot)):
            reqs = [c for c in stamped if isinstance(c, str)]
        else:
            reqs = hard_requirements(cmap, row["body"], skills)
        return decide(cmap, host, [*reqs, *hard_requirements(cmap, "", preloaded)], modes)

    # warn/off everywhere: nothing could have refused, so a torn read refuses nothing either.
    return _consistent(fleet_dir, check,
                       on_exhausted=lambda: indeterminate_line(host) if _any_enforced(modes) else None)


# -- Layer 1: mid-run skill_view (F14) --------------------------------------------------

def skill_gate(name: str) -> Optional[str]:
    """The ABSENT line for a mid-run ``skill_view`` of a skill whose enforced
    capability this placed worker's host lacks; None otherwise (never for
    an unplaced worker, a delegate child, or an interactive session)."""
    return _midrun_gate(lambda cmap: hard_requirements(cmap, "", [name]))


def capability_gate(cap: str) -> Optional[str]:
    """Layer 1b (D1b-undeclared, t_f813a42f): the ABSENT line for a tool call that itself
    needs ``cap`` (``browser-session`` for a browser_* call) on a placed worker whose host
    lacks it, declared on the card or not; None otherwise, under the same scope as
    :func:`skill_gate`."""
    return _midrun_gate(lambda _cmap: [cap])


def _midrun_gate(requirements) -> Optional[str]:
    """The shared mid-run decision: ``requirements(cmap)`` against the placed host's row."""
    try:
        from agent.delegation_context import owned_kanban_task
        if not owned_kanban_task():
            return None
        from hermes_cli import kanban_worker_hosts as kwh
        host = kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))
        if not kwh.is_pool_host(host):
            return None
        modes = caps_modes()
        if not modes:
            return None
        fleet_dir = _fleet_dir()
    except Exception:
        _log.debug("skill_view placement gate failed open", exc_info=True)
        return None

    def check(snapshot):
        # The skill's requirements and the host row come from the captured bytes, through the
        # same normalization and decision as boot (682bd83da2ba). A failure inside one attempt
        # admits THAT attempt only (the layer is off for it).
        try:
            cmap = snapshot_map(snapshot)
            return decide(cmap, host, requirements(cmap), modes)
        except Exception:
            _log.debug("mid-run placement gate attempt failed open", exc_info=True)
        return None

    # Outside every except: the exhaustion refusal can never be swallowed back into None.
    return _consistent(fleet_dir, check,
                       on_exhausted=lambda: indeterminate_line(host) if _any_enforced(modes) else None)


__all__: List[Any] = [
    "ABSENT_RE", "REROUTE_EVENT", "absent_line", "parse_absent", "reroute_or_park",
    "worker_exit_on_absent", "attached_skill_gap", "placement_record", "boot_check", "skill_gate",
    "capability_gate",
]
