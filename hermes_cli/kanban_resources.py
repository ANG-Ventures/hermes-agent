"""Resources bound to a kanban run's lifetime (saturation SPEC 4.4, card t_2df204e5).

A card started win11-ai (32 GB), finished, and the VM ran until the box died. Start helpers
(``winvm start``, ``mem0ctl move`` drills, a fleet docker/compose wrapper, ``systemctl start`` of a
GPU unit) now record what they started on the run: ``task_runs.resources`` holds a JSON list of
``{kind, id, host, started_at, state}``. Once the card is ``done`` or ``archived``, the dispatcher
tick (:func:`release_finished`) stops every resource still ``started`` with the stop verb for its
kind, unless the card body carries ``keep-alive: <reason>``. One #logs line per stop.

States: ``started`` (live, ours to stop) -> ``stopped`` | ``kept`` (keep-alive) | ``stop_failed``
(``MAX_STOP_ATTEMPTS`` tries failed; the closeout check refuses it until someone stops it by hand
and re-runs ``resource mark``).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# kind -> argv builder for the stop verb (run on the resource's host). ``stop`` never deletes:
# a container/compose project keeps its volumes, a VM gets an ACPI shutdown, a unit is stopped.
STOP_VERBS: dict[str, Callable[[str], list[str]]] = {
    "vm": lambda rid: ["sudo", "-n", "virsh", "shutdown", rid],
    "container": lambda rid: ["docker", "stop", rid],
    "compose": lambda rid: ["docker", "compose", "-p", rid, "stop"],
    "gpu-resident": lambda rid: (
        ["systemctl", "--user", "stop", rid[len("user:"):]] if rid.startswith("user:")
        else ["sudo", "-n", "systemctl", "stop", rid]
    ),
}
KINDS = tuple(STOP_VERBS)
OPEN_STATES = ("started", "stop_failed")
TERMINAL_CARD = ("done", "archived")
MAX_STOP_ATTEMPTS = 3
STOP_TIMEOUT_S = 90
# A resource id / host goes through a remote shell (ssh), so only shell-inert characters.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
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
    for label, val in (("id", rid), ("host", host)):
        if not _TOKEN_RE.match(val or ""):
            raise ValueError(f"resource {label} {val!r} is not a plain token ([A-Za-z0-9_.:@/-])")
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
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", res["host"], shlex.join(argv)]


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=STOP_TIMEOUT_S, stdin=subprocess.DEVNULL)
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


def release_finished(
    conn, *, board: Optional[str] = None, runner: Callable[[list[str]], tuple[int, str]] = _default_runner,
    notify: Callable[[Optional[str], str], None] = _default_notify, dry_run: bool = False,
) -> list[dict]:
    """Stop (or mark kept) every ``started`` resource whose card is done/archived. Runs once per
    dispatcher tick, outside the dispatch lock; the stop commands run with no DB transaction
    open. Returns one action record per resource touched."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT r.id AS run_id, r.task_id, r.resources, t.body, t.status FROM task_runs r "
        "JOIN tasks t ON t.id = r.task_id "
        "WHERE r.resources LIKE '%\"state\":\"started\"%' AND t.status IN ('done', 'archived')"
    ).fetchall()
    actions: list[dict] = []
    for row in rows:
        keep = keep_alive_reason(row["body"])
        items = _load(row["resources"])
        changed = False
        for res in items:
            if res.get("state") != "started" or res.get("kind") not in STOP_VERBS:
                continue
            act = {"task_id": row["task_id"], "run_id": int(row["run_id"]), "kind": res["kind"],
                   "id": res["id"], "host": res["host"]}
            if keep:
                act.update(action="kept", reason=keep)
                if not dry_run:
                    res.update(state="kept", kept_reason=keep[:200], released_at=int(time.time()))
            else:
                argv = _stop_argv(res)
                act["argv"] = argv
                if dry_run:
                    act["action"] = "would_stop"
                else:
                    rc, tail = runner(argv)
                    res["stop_attempts"] = int(res.get("stop_attempts") or 0) + 1
                    if rc == 0:
                        res.update(state="stopped", released_at=int(time.time()))
                        act["action"] = "stopped"
                    else:
                        res["last_error"] = f"rc={rc} {tail}"[:240]
                        if res["stop_attempts"] >= MAX_STOP_ATTEMPTS:
                            res["state"] = "stop_failed"
                        act.update(action="stop_failed" if res["state"] == "stop_failed" else "retry",
                                   error=res["last_error"])
            actions.append(act)
            changed = changed or not dry_run
        if not changed:
            continue
        with kb.write_txn(conn):
            # Re-read under the write lock: a concurrent registration on the same run must survive.
            cur = _load(conn.execute("SELECT resources FROM task_runs WHERE id = ?", (row["run_id"],)).fetchone()[0])
            ours = {(r.get("kind"), r.get("id"), r.get("host")): r for r in items}
            merged = [ours.get((r.get("kind"), r.get("id"), r.get("host")), r)
                      if r.get("state") == "started" else r for r in cur]
            conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(merged), row["run_id"]))
            for act in actions:
                if act["run_id"] == int(row["run_id"]) and act["action"] != "retry":
                    kb._append_event(conn, row["task_id"], f"resource_{act['action']}",
                                     {k: act[k] for k in ("kind", "id", "host", "reason", "error") if k in act},
                                     run_id=int(row["run_id"]))
    for act in actions:
        if dry_run or act["action"] == "retry":
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
                if (r.get("kind"), r.get("id"), r.get("host")) == (kind, rid, host) and r.get("state") != state:
                    r.update(state=state, released_at=int(time.time()), mark_reason=reason.strip()[:200])
                    hit = True
                    n += 1
            if hit:
                conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (_dump(items), row["id"]))
                kb._append_event(conn, task_id, "resource_marked",
                                 {"kind": kind, "id": rid, "host": host, "state": state, "reason": reason.strip()[:200]},
                                 run_id=int(row["id"]))
    return n
