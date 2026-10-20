"""Resources bound to a kanban run's lifetime (saturation SPEC 4.4, card t_2df204e5).

A card started win11-ai (32 GB), finished, and the VM ran until the box died. Start helpers
(``winvm start``, ``mem0ctl move`` drills, a fleet docker/compose wrapper, ``systemctl start`` of a
GPU unit) now record what they started on the run: ``task_runs.resources`` holds a JSON list of
``{kind, id, host, started_at, state}``. Once the card is ``done`` or ``archived``, the dispatcher
tick (:func:`release_finished`) stops every resource still ``started`` with the stop verb for its
kind, unless the card body carries ``keep-alive: <reason>``. One #logs line per stop.

States: ``started`` (live, ours to stop) -> ``stopping`` (claimed by one sweep) -> ``stopped`` | ``kept`` (keep-alive) | ``stop_failed``
(``MAX_STOP_ATTEMPTS`` tries failed; the closeout check refuses it until someone stops it by hand
and re-runs ``resource mark``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# kind -> argv builder for the stop verb (run on the resource's host). ``stop`` never deletes:
# a container/compose project keeps its volumes, a VM gets an ACPI shutdown, a unit is stopped.
# The id comes from a worker-writable row, so it always sits after ``--`` (or inside ``--opt=``):
# it can never be read as an option, whatever the token check below admits.
STOP_VERBS: dict[str, Callable[[str], list[str]]] = {
    "vm": lambda rid: ["sudo", "-n", "virsh", "shutdown", "--", rid],
    "container": lambda rid: ["docker", "stop", "--", rid],
    "compose": lambda rid: ["docker", "compose", f"--project-name={rid}", "stop"],
    "gpu-resident": lambda rid: (
        ["systemctl", "--user", "stop", "--", rid[len("user:"):]] if rid.startswith("user:")
        else ["sudo", "-n", "systemctl", "stop", "--", rid]
    ),
}
KINDS = tuple(STOP_VERBS)
OPEN_STATES = ("started", "stopping", "stop_failed")
TERMINAL_CARD = ("done", "archived")
MAX_STOP_ATTEMPTS = 3
STOP_TIMEOUT_S = 90
# A ``stopping`` claim this old belongs to a sweep that died mid-stop; the next sweep may retake it.
STALE_CLAIM_S = 2 * STOP_TIMEOUT_S + 60
# A resource id goes through a remote shell (ssh), so only shell-inert characters.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
# A host is a bare fleet name (no user@, no path, no option), and must also be in the fleet registry.
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
# Fleet files (under <kanban home>/fleet) whose host names the sweep may ssh to.
_REGISTRY_FILES = ("kanban-reach-matrix.json", "placement-policy.json", "kanban-pool.json")
_KEEP_ALIVE_RE = re.compile(r"(?im)^\s*keep-alive:\s*(\S.*)$")
_LOCAL_HOSTS = {"local", "localhost"}
# Compact JSON so the sweep's LIKE prefilter can match the state literally.
_SEP = (",", ":")


def _dump(items: list[dict]) -> str:
    return json.dumps(items, separators=_SEP, sort_keys=True)


def _load(text: Any) -> list[dict]:
    try:
        val = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return [r for r in val if isinstance(r, dict)] if isinstance(val, list) else []


def keep_alive_reason(body: Optional[str]) -> Optional[str]:
    """The ``keep-alive: <reason>`` text from a card body, else None (an empty reason does not count)."""
    m = _KEEP_ALIVE_RE.search(body or "")
    return m.group(1).strip() if m else None


def _this_host() -> str:
    return socket.gethostname().split(".")[0].lower()


def _is_local(host: str) -> bool:
    return host.lower() in _LOCAL_HOSTS or host.split(".")[0].lower() == _this_host()


def fleet_hosts(fleet_dir: Optional[Path] = None) -> frozenset[str]:
    """Host names the sweep may stop a resource on: the ssh aliases and worker hosts of
    ``kanban-reach-matrix.json`` plus the ``hosts`` of ``placement-policy.json`` and
    ``kanban-pool.json``. Fail closed: an absent or unreadable file contributes nothing, so with no
    registry only this box (``local``) is reachable."""
    if fleet_dir is None:
        from hermes_cli import kanban_db as kb

        fleet_dir = kb.kanban_home() / "fleet"
    names: set[str] = set()
    for fname in _REGISTRY_FILES:
        try:
            doc = json.loads((Path(fleet_dir) / fname).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if isinstance(doc.get("aliases"), list):
            names.update(a for a in doc["aliases"] if isinstance(a, str))
        if isinstance(doc.get("hosts"), dict):
            names.update(h for h in doc["hosts"] if isinstance(h, str))
    return frozenset(n.lower() for n in names if _HOST_RE.match(n.lower()))


def host_refusal(host: Any, registry: Optional[frozenset[str]] = None) -> Optional[str]:
    """Why ``host`` may not carry a resource, else None."""
    if not isinstance(host, str) or not _HOST_RE.match(host.lower()):
        return f"resource host {host!r} is not a bare fleet host name ([a-z0-9.-])"
    if _is_local(host):
        return None
    if host.lower() not in (fleet_hosts() if registry is None else registry):
        return f"resource host {host!r} is not in the fleet registry (fleet/{', fleet/'.join(_REGISTRY_FILES)})"
    return None


def _resolve_run_id(conn, task_id: str, run_id: Optional[int]) -> int:
    if run_id is not None:
        row = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
        if row is None or row["task_id"] != task_id:
            raise ValueError(f"run {run_id} is not a run of {task_id}")
        return int(run_id)
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row["current_run_id"]:
        return int(row["current_run_id"])
    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{task_id} has no run to bind a resource to")
    return int(row["id"])


def kanban_run_resource(
    conn, task_id: str, kind: str, rid: str, host: str, *, run_id: Optional[int] = None,
) -> dict:
    """Record that ``task_id``'s run started ``kind``/``rid`` on ``host``. Idempotent per
    (kind, id, host): a re-start of a recorded resource re-arms it as ``started``.

    Refused on a done/archived card: the sweep would stop the resource on its next tick, so a
    late registration there could only ever be a mistake (or a way to stop someone else's thing).
    Returns ``{"run_id", "resource"}``."""
    from hermes_cli import kanban_db as kb

    if kind not in STOP_VERBS:
        raise ValueError(f"unknown resource kind {kind!r} (one of {', '.join(KINDS)})")
    if not _TOKEN_RE.match(rid or ""):
        raise ValueError(f"resource id {rid!r} is not a plain token ([A-Za-z0-9_.:@/-])")
    why = host_refusal(host)
    if why:
        raise ValueError(why)
    host = host.lower()
    with kb.write_txn(conn):
        trow = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if trow is None:
            raise ValueError(f"unknown task {task_id}")
        if trow["status"] in TERMINAL_CARD:
            raise ValueError(f"{task_id} is {trow['status']}; a finished card cannot own a new resource")
        run = _resolve_run_id(conn, task_id, run_id)
        items = _load(conn.execute("SELECT resources FROM task_runs WHERE id = ?", (run,)).fetchone()[0])
        now = int(time.time())
        rec = {"kind": kind, "id": rid, "host": host, "started_at": now, "state": "started"}
        items = [r for r in items if (r.get("kind"), r.get("id"), r.get("host")) != (kind, rid, host)]
        items.append(rec)
        conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), run))
        kb._append_event(conn, task_id, "resource_started", {"kind": kind, "id": rid, "host": host}, run_id=run)
    return {"run_id": run, "resource": rec}


def list_resources(conn, task_id: str) -> list[dict]:
    """Every recorded resource of every run of ``task_id`` (each tagged with ``run_id``)."""
    out = []
    for row in conn.execute(
        "SELECT id, resources FROM task_runs WHERE task_id = ? ORDER BY started_at, id", (task_id,),
    ):
        out.extend({**r, "run_id": int(row["id"])} for r in _load(row["resources"]))
    return out


def outstanding(conn, task_id: str) -> list[dict]:
    """Resources neither stopped nor kept: what the ang-closeout Resources step refuses."""
    return [r for r in list_resources(conn, task_id) if r.get("state") in OPEN_STATES]


def _stop_argv(res: dict) -> list[str]:
    argv = STOP_VERBS[res["kind"]](res["id"])
    if _is_local(res["host"]):
        return argv
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", res["host"], shlex.join(argv)]


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=STOP_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, f"{type(exc).__name__}: {exc}"
    tail = ((p.stderr or p.stdout or "").strip().splitlines() or [""])[-1]
    return p.returncode, tail[:200]


def _default_notify(board: Optional[str], body: str) -> None:
    """One #logs line (fire-and-forget; a hermetic home without notify.py is a no-op)."""
    try:
        from hermes_cli import kanban_budget as _kbudget

        script = _kbudget._notify_script_path()
        if script is None:
            return
        subprocess.Popen(
            [sys.executable, script, "--channel", "discord", "--target", _kbudget.RECOVERY_TARGET,
             "--send", body],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # best-effort by contract
        _log.warning("kanban resource #logs notify failed (%s: %s)", type(exc).__name__, exc)


def _norm_key(res: dict) -> tuple:
    """(kind, id, host) with this box's own name folded to ``local``, so two cards that recorded
    one resource under either name are seen as sharing it."""
    host = str(res.get("host") or "").lower()
    return (res.get("kind"), res.get("id"), "local" if host and _is_local(host) else host)


def _live_keys(conn) -> set[tuple]:
    """Resources still ``started`` on a card that has not finished: never ours to stop yet."""
    keys: set[tuple] = set()
    for row in conn.execute(
        "SELECT r.resources FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.resources LIKE '%\"state\":\"started\"%' AND t.status NOT IN ('done', 'archived')"
    ):
        keys.update(_norm_key(r) for r in _load(row[0]) if r.get("state") == "started")
    return keys


def why_refused(res: dict, registry: frozenset[str]) -> Optional[str]:
    """Why a recorded row must never be run (planted past registration), else None."""
    if not _TOKEN_RE.match(str(res.get("id") or "")):
        return f"resource id {res.get('id')!r} is not a plain token"
    return host_refusal(res.get("host"), registry)


def _claimed(items: list[dict], token: str) -> Optional[dict]:
    """The record our sweep still owns: same claim token AND still ``stopping``. A record another sweep
    resolved (kept, refused, stopped) is never renewed, stopped or settled by us again."""
    return next((r for r in items if r.get("claim") == token and r.get("state") == "stopping"), None)


def _renew_claims(conn, members: list) -> bool:
    """Refresh ``claimed_at`` on every record in ``members`` still holding our token, so a batch of
    slow stops never lets a queued claim go stale mid-sweep. False when none is ours any more."""
    from hermes_cli import kanban_db as kb

    held = False
    now = int(time.time())
    if not members:
        return False
    with kb.write_txn(conn):
        for run_id, _task_id, token, _act in members:
            items = _load(conn.execute("SELECT resources FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0])
            mine = _claimed(items, token)
            if mine is not None:
                mine["claimed_at"] = now
                conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), run_id))
                held = True
    return held


def release_finished(
    conn, *, board: Optional[str] = None, runner: Callable[[list[str]], tuple[int, str]] = _default_runner,
    notify: Callable[[Optional[str], str], None] = _default_notify, dry_run: bool = False,
) -> list[dict]:
    """Stop (or mark kept) every ``started`` resource whose card is done/archived. Runs once per
    dispatcher tick, outside the dispatch lock. Returns one action record per resource touched.

    Three phases so overlapping sweeps and re-registrations stay safe:
      1. claim (one write txn): each candidate goes ``started`` -> ``stopping`` with a fresh claim
         token, so a second sweep never selects it; a resource another live card still has
         ``started`` is left alone until its last owner finishes; keep-alive/refusals settle here;
      2. stop (no txn open): one stop per resource, however many finished runs recorded it, each
         preceded by renewing the claims still queued and re-checking that this one is still ours;
      3. settle (a write txn right after each stop): a result lands only on a record still holding
         our claim token. A record re-registered meanwhile is a new generation and stays ``started``.
    Each claim is re-checked and renewed just before its own stop, so ``STALE_CLAIM_S`` bounds one
    stop, not the whole batch; a claim older than that (its sweep died mid-stop) is claimable again."""
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    actions: list[dict] = []
    plans: dict[tuple, dict] = {}
    txn = contextlib.nullcontext() if dry_run else kb.write_txn(conn)
    with txn:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, r.resources, t.body FROM task_runs r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE (r.resources LIKE '%\"state\":\"started\"%' OR r.resources LIKE '%\"state\":\"stopping\"%') "
            "AND t.status IN ('done', 'archived')"
        ).fetchall()
        live = _live_keys(conn) if rows else set()
        registry = fleet_hosts() if rows else frozenset()
        for row in rows:
            run_id, keep = int(row["run_id"]), keep_alive_reason(row["body"])
            items = _load(row["resources"])
            settled: list[dict] = []
            changed = False
            for res in items:
                state = res.get("state")
                if res.get("kind") not in STOP_VERBS or state not in ("started", "stopping"):
                    continue
                if state == "stopping" and now - int(res.get("claimed_at") or 0) < STALE_CLAIM_S:
                    continue  # another sweep holds it
                act = {"task_id": row["task_id"], "run_id": run_id, "kind": res["kind"],
                       "id": res.get("id"), "host": res.get("host")}
                key = _norm_key(res)
                if keep or why_refused(res, registry):
                    # Settled here: a stale claim on it is dead, so a paused sweep holding it cannot resume it.
                    res.pop("claim", None)
                    res.pop("claimed_at", None)
                if keep:
                    act.update(action="kept", reason=keep)
                    res.update(state="kept", kept_reason=keep[:200], released_at=now)
                elif why := why_refused(res, registry):
                    # A row planted past registration: never run it, leave it open for closeout.
                    act.update(action="stop_failed", error=f"refused: {why}"[:240])
                    res.update(state="stop_failed", last_error=act["error"])
                elif key in live:
                    _log.info("kanban resource %s kept running: a live card still owns it", key)
                    continue
                else:
                    token = secrets.token_hex(8)
                    res.update(state="stopping", claim=token, claimed_at=now)
                    plan = plans.setdefault(key, {"argv": _stop_argv(res), "members": []})
                    act["argv"] = plan["argv"]
                    plan["members"].append((run_id, row["task_id"], token, act))
                    if dry_run:
                        act["action"] = "would_stop"
                        actions.append(act)
                    changed = True
                    continue
                settled.append(act)
                changed = True
            actions.extend(settled)
            if changed and not dry_run:
                conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), run_id))
                for act in settled:
                    kb._append_event(conn, act["task_id"], f"resource_{act['action']}",
                                     {k: act[k] for k in ("kind", "id", "host", "reason", "error") if k in act},
                                     run_id=run_id)
    if dry_run:
        return actions

    # Stop and settle one resource at a time: renew every claim still queued, stop only if this one is
    # still ours, then write its result at once, so no claim (queued or done) ever ages past STALE_CLAIM_S.
    stopped_acts: list[dict] = []
    pending = list(plans)
    while pending:
        key = pending.pop(0)
        plan = plans[key]
        _renew_claims(conn, [m for k in pending for m in plans[k]["members"]])
        if not _renew_claims(conn, plan["members"]):
            continue  # every claim was retaken before the stop ran: the other sweep owns it now
        rc, tail = runner(plan["argv"])
        with kb.write_txn(conn):
            for run_id, task_id, token, act in plan["members"]:
                items = _load(conn.execute("SELECT resources FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0])
                mine = _claimed(items, token)
                if mine is None:
                    continue  # re-registered while the stop ran: that generation is not ours to settle
                mine.pop("claim", None)
                mine.pop("claimed_at", None)
                mine["stop_attempts"] = int(mine.get("stop_attempts") or 0) + 1
                if rc == 0:
                    mine.update(state="stopped", released_at=int(time.time()))
                    act["action"] = "stopped"
                else:
                    mine["last_error"] = f"rc={rc} {tail}"[:240]
                    mine["state"] = "stop_failed" if mine["stop_attempts"] >= MAX_STOP_ATTEMPTS else "started"
                    act.update(action="stop_failed" if mine["state"] == "stop_failed" else "retry",
                               error=mine["last_error"])
                conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), run_id))
                if act["action"] != "retry":
                    kb._append_event(conn, task_id, f"resource_{act['action']}",
                                     {k: act[k] for k in ("kind", "id", "host", "error") if k in act},
                                     run_id=run_id)
                stopped_acts.append(act)
    actions.extend(stopped_acts)
    for act in actions:
        if act["action"] == "retry":
            continue
        what = f"{act['kind']} `{act['id']}` on {act['host']}"
        line = {
            "stopped": f"🧹 Kanban: stopped {what}, started by {act['task_id']} (card finished).",
            "kept": f"📌 Kanban: kept {what} after {act['task_id']} finished (keep-alive: {act.get('reason', '')[:120]}).",
            "stop_failed": f"⚠️ Kanban: could not stop {what} for finished {act['task_id']} "
                           f"after {MAX_STOP_ATTEMPTS} tries ({act.get('error', '')[:120]}). Stop it by hand.",
        }[act["action"]]
        _log.warning("PHASE=kanban_resource_release %s", line)
        notify(board, line)
    return actions


def mark(conn, task_id: str, kind: str, rid: str, host: str, state: str, *, reason: str) -> int:
    """Operator override: set a recorded resource to ``stopped`` or ``kept`` (e.g. after a
    ``stop_failed`` was stopped by hand). Returns the number of records changed."""
    from hermes_cli import kanban_db as kb

    if state not in ("stopped", "kept"):
        raise ValueError("state must be stopped or kept")
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    n = 0
    with kb.write_txn(conn):
        for row in conn.execute("SELECT id, resources FROM task_runs WHERE task_id = ?", (task_id,)).fetchall():
            items = _load(row["resources"])
            hit = False
            for r in items:
                if (r.get("kind"), r.get("id"), r.get("host")) == (kind, rid, host.lower()) and r.get("state") != state:
                    r.update(state=state, released_at=int(time.time()), mark_reason=reason.strip()[:200])
                    hit = True
                    n += 1
            if hit:
                conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), row["id"]))
                kb._append_event(conn, task_id, "resource_marked",
                                 {"kind": kind, "id": rid, "host": host, "state": state, "reason": reason.strip()[:200]},
                                 run_id=int(row["id"]))
    return n
