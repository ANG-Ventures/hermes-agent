"""Continuation affinity: pessimistic placement, positive state-loss proof.

Host state is independent of browser leases and of spill portability. The
root placement switch is deliberately independent of the capability switch.
"""
from __future__ import annotations

import json
import time
from typing import Optional

STUDIO = "studio"
MODES = frozenset({"off", "warn", "enforce"})


def mode(config: dict) -> str:
    placement = config.get("placement") or {}
    value = placement.get("continuation", "off") if isinstance(placement, dict) else "off"
    return value if isinstance(value, str) and value in MODES else "off"


def backfill_origin_hosts(conn) -> int:
    """One-shot migration; never resurrect an explicitly dropped origin.

    Local legacy runs have no placement event and remain unknown. Remote
    events populate their run even when the card is terminal.
    """
    from hermes_cli import kanban_placement_reasons as _pr
    latest = {}
    for row in conn.execute(
        "SELECT task_id, run_id, payload FROM task_events "
        f"WHERE kind IN ({_pr.kind_marks('worker_placed')}) ORDER BY id", _pr.kind_names("worker_placed")
    ):
        try:
            host = json.loads(row[2] or "{}").get("host")
        except (ValueError, AttributeError, TypeError):
            continue
        if not isinstance(host, str) or not host:
            continue
        latest[row[0]] = host
        if row[1] is not None:
            conn.execute("UPDATE task_runs SET host=? WHERE id=? AND host IS NULL", (host, row[1]))
    count = 0
    for task, host in latest.items():
        count += conn.execute(
            "UPDATE tasks SET origin_host=? WHERE id=? AND origin_host IS NULL "
            "AND status NOT IN ('done','archived')", (host, task)
        ).rowcount
    return count


def record_placement(conn, task_id: str, run_id: int, host: Optional[str]) -> bool:
    """Fence BEFORE launching: abrupt worker exits must retain the origin.

    A local spawn records its execution host but must not erase a remote
    origin when the continuation switch is off.
    """
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND current_run_id=? AND status='running'",
            (task_id, run_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE task_runs SET host=? WHERE id=?", (host or STUDIO, run_id))
        if host:
            conn.execute("UPDATE tasks SET origin_host=? WHERE id=?", (host, task_id))
    return True


def clear_lost_state(conn, task_id: str, host: str) -> bool:
    """Only call after a successful empty/missing probe. CAS + notice atomic."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        changed = conn.execute(
            "UPDATE tasks SET origin_host=NULL WHERE id=? AND origin_host=? "
            "AND status IN ('ready','review') AND claim_lock IS NULL", (task_id, host),
        ).rowcount
        if not changed:
            return False
        run = conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (task_id,)).fetchone()
        last = run[0] if run else None
        kb._append_event(conn, task_id, "origin_state_lost", {"host": host, "last_run": last})
        kb.add_comment(conn, task_id, "dispatcher",
                       f"state from run {last} on {host} is gone (GC'd or re-imaged); this run starts fresh")
    return True


def origin_hard_refusal(conn, row, pool, host: str, caps=None) -> Optional[str]:
    """Why the card's state on ``host`` cannot run there, or None: registry, assignee, hard capabilities,
    Studio-only access, split brain. Pure over the probe: shared by :func:`origin_route` and the fleet pause
    resume pin (Prism 904f143f5a38), which must keep these checks without letting an empty probe clear the origin."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_hosts as kwh
    tid = row["id"]
    reason = None
    target = next((h for h in pool.pool_hosts if h.name.lower() == host.lower()), None)
    if target is None:
        reason = f"origin {host} is not in the worker registry"
    elif row["assignee"] not in target.profiles:
        reason = f"origin {host} does not serve assignee {row['assignee']}"
    elif caps is not None:
        from hermes_cli import kanban_host_capabilities as khc
        skills = kb._json_or(row["skills"]) or []
        requirements, _none = khc.derive(row["body"], skills, caps.cmap)
        # Hard requirements must hold on origin even when ordinary routing
        # only warns about capabilities.
        missing = [r.cap for r in requirements if r.strength == "hard"
                   and not khc._satisfied(r.cap, caps, host)]
        if missing:
            reason = f"needs {', '.join(sorted(set(missing)))}; state is on {host}"
    if reason is None and caps is None:
        from hermes_cli import kanban_host_capabilities as khc
        words = khc.declared_words(row['body'])
        if any(w != 'none' for w in words):
            reason = f'cannot verify hard requirements on origin {host}: capability map unavailable'
    if reason is None:
        # Access-bound (spec §2.6, Prism bbd09df624d7): the dispatcher returns
        # this route before portable() runs, so the access rule is checked
        # here too, with or without a capability map. A Studio-only access
        # never runs on the origin host.
        from hermes_cli import kanban_worker_pool as kwp
        from hermes_cli import kanban_host_capabilities as khc
        # A served ssh-root (t_79c2259b) was checked above as a hard capability on ``host``.
        access = kwp.access_bound_for(row["body"], pool, served=khc.served_ssh_root(caps))
        if access is not None:
            reason = f"access {access} is Studio-only; state is on {host}"
    if reason is None:
        for other in conn.execute(
            "SELECT t.id,t.origin_host,t.workspace_path FROM task_links l JOIN tasks t "
            "ON t.id=CASE WHEN l.parent_id=? THEN l.child_id ELSE l.parent_id END "
            "WHERE (l.parent_id=? OR l.child_id=?) AND COALESCE(l.kind,'blocks')!='derived-from'",
            (tid, tid, tid),
        ):
            other_host = other["origin_host"]
            if other_host and other_host.lower() != host.lower():
                reason = f"split brain: {tid} state on {host}, {other['id']} state on {other_host}"
                break
            if not other_host and kwh.local_workspace_has_content(other["workspace_path"]):
                reason = f"split brain: {tid} state on {host}, {other['id']} state on studio"
                break
    return reason


def origin_route(conn, row, pool, *, config: dict, states: dict, caps=None,
                 dry_run: bool = False, now: Optional[int] = None):
    """Return origin/wait/refused or None for the ordinary spill ladder.

    states maps (host, workspace) to nonempty/empty/unknown/host_key_mismatch.
    Unknown is sticky. All state-sharing edges are checked, not just the
    first edge portable() reports. Provenance edges never bind placement.
    """
    from hermes_cli import kanban_db as kb
    selected = mode(config)
    if selected == "off":
        return None
    host = kb._row_get(row, "origin_host")
    if not host:
        return None
    tid = row["id"]
    state = states.get((host, row["workspace_path"]), "unknown")
    if selected == "warn":
        if not dry_run:
            # One diagnostic per origin/state, not an append every tick.
            last = kb._latest_event(conn, tid, "continuation_observed")
            payload = {"host": host, "state": state}
            if last is None or kb._json_or(last["payload"]) != payload:
                kb._append_event(conn, tid, "continuation_observed", payload)
        return None
    if state == "empty":
        if not dry_run:
            clear_lost_state(conn, tid, host)
        return None
    target = next((h for h in pool.pool_hosts if h.name.lower() == host.lower()), None)
    reason = "origin host re-imaged? host-key mismatch" if state == "host_key_mismatch" else \
        origin_hard_refusal(conn, row, pool, host, caps)
    now = int(time.time()) if now is None else now
    if reason is None and state == "unknown":
        from hermes_cli import kanban_placement_reasons as _pr
        last = conn.execute(
            f"SELECT payload FROM task_events WHERE task_id=? AND kind IN ({_pr.kind_marks('placement_wait')}) "
            "ORDER BY id DESC LIMIT 1",
            (tid, *_pr.kind_names("placement_wait")),
        ).fetchone()
        prev = kb._json_or(last[0]) if last else {}
        prev = prev if isinstance(prev, dict) else {}
        since = prev.get("since") if prev.get("reason") == "origin_host_absent" and prev.get("host") == host else now
        placement = config.get("placement") or {}
        limit = placement.get("origin_wait_s", 7200 if target.absence == "optional" else 1800)
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            limit = 7200 if target.absence == "optional" else 1800
        if now - int(since) >= limit:
            reason = f"origin {host} absent for {now-int(since)}s; wait longer or start fresh"
        else:
            return "wait", "origin_host_absent"
    if reason:
        message = f"{reason}; requeue --drop-origin starts fresh (files stay behind); workspace migrate is not yet available"
        if not dry_run:
            if kb.block_task(conn, tid, kind="needs_input", reason=message):
                kb.add_comment(conn, tid, "dispatcher", message)
        return "wait", message
    return "origin", host


def remote_cleanup_host(conn, task_id):
    """Terminal origin is cleared; execution history still owns remote GC."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_pool as kwp
    row = conn.execute("SELECT host FROM task_runs WHERE task_id=? AND host IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if row is None or row[0] == STUDIO:
        return None
    pool = kwp.read_pool(kb.kanban_home() / 'fleet', kanban_cfg=kb._board_home_kanban_cfg())
    return next((h for h in pool.pool_hosts if h.name == row[0]), None)


def remote_run_host(conn, task_id: str) -> Optional[str]:
    """The pool host holding the card's scratch tree, or None.

    Read from ``tasks.origin_host``, the field enforce-mode placement routes
    on (:func:`origin_route`), so a restored placeholder and the next
    placement name the same host (Prism c544a7721690). The dispatcher's copy
    of the path is only the placeholder ``resolve_workspace`` made, so a
    missing local dir there is not a lost mount (t_8721e331). None when the
    card has no remote origin, or when it ran on the Studio at its current
    path: that dir held real work and its loss stays a stranded refusal
    (Prism 029eb26d973c).
    """
    row = conn.execute("SELECT origin_host FROM tasks WHERE id=?", (task_id,)).fetchone()
    host = row[0] if row else None
    if not host or host.lower() == STUDIO or _ran_on_studio_at_current_path(conn, task_id):
        return None
    return host


def _ran_on_studio_at_current_path(conn, task_id: str) -> bool:
    """True when any card that held the current path ran on the Studio while holding it.

    The path is held from the card's latest ``origin_dropped`` (the only
    in-place path change) to now, and back through the ``--continues`` chain:
    each source held it up to its ``workspace_transferred`` event (Prism
    e641269822db). Run order against those events is by event id, so a run
    in the same second as the event is placed exactly (Prism c920880fe89d);
    a run with no events falls back to ``started_at`` and a tie counts
    (fail closed: the stranded refusal stays). Runs with a NULL host never
    spawned (or predate host recording) and are not counted.
    """
    card, upper, seen = task_id, None, set()
    while card and card not in seen and len(seen) < 32:
        seen.add(card)
        lower = conn.execute(
            "SELECT id, created_at FROM task_events WHERE task_id=? AND kind='origin_dropped'"
            " AND id<? ORDER BY id DESC LIMIT 1",
            (card, upper[0] if upper else 2**62),
        ).fetchone()
        for run_id, started in conn.execute(
            "SELECT id, COALESCE(started_at, 0) FROM task_runs WHERE task_id=? AND LOWER(host)=?",
            (card, STUDIO),
        ).fetchall():
            first = conn.execute(
                "SELECT MIN(id) FROM task_events WHERE task_id=? AND run_id=?", (card, run_id),
            ).fetchone()[0]
            after = lower is None or (first > lower[0] if first is not None else started >= lower[1])
            before = upper is None or (first < upper[0] if first is not None else started <= upper[1])
            if after and before:
                return True
        if lower is not None:
            return False  # the path began at that drop; earlier holders used another dir
        cont = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='continues' ORDER BY id DESC LIMIT 1",
            (card,),
        ).fetchone()
        try:
            parent = json.loads(cont[0]).get("parent") if cont else None
        except (TypeError, ValueError, AttributeError):
            parent = None
        if not parent:
            return False
        upper = conn.execute(
            "SELECT id, created_at FROM task_events WHERE task_id=? AND kind='workspace_transferred'"
            " AND json_extract(payload, '$.to')=? ORDER BY id DESC LIMIT 1",
            (parent, card),
        ).fetchone()
        if upper is None:
            return True  # a chain we cannot place: keep the refusal
        card = parent
    return False


def restore_remote_placeholder(conn, task) -> Optional[str]:
    """Re-create the local placeholder of a scratch card whose tree lives on a pool host.

    Returns that host when the placeholder now exists, else None. The path is
    unchanged, so the card's state on its origin host stays bound to it (an
    enforce-mode origin route sends it back there; elsewhere it starts in an
    empty dir, as any re-placed scratch card does). Mount checks stay in
    :func:`_materialize_fresh`: an unmounted root raises and nothing is made.
    """
    import os
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    if (task.workspace_kind or "scratch") != "scratch" or not task.workspace_path:
        return None
    host = remote_run_host(conn, task.id)
    if host is None:
        return None
    try:
        _materialize_fresh(conn, task.workspace_path)
    except (WorkspaceUnavailable, OSError):
        return None
    return host if os.path.isdir(os.path.expanduser(task.workspace_path)) else None


def _fresh_path(path: str, tag: str) -> str:
    """A sibling path no card owns: a NULL path is no fresh start, because a
    scratch card's NULL path re-resolves to ``<root>/<id>``, the old dir."""
    import os
    return f"{os.path.normpath(path)}.{tag}"


def _materialize_fresh(conn, path: str) -> None:
    """Create a fresh path under the mount-guarded root before it is persisted.

    Admission refuses a persisted path that is not a directory
    (``stranded_by_mount_loss``), so a never-created fresh path would strand
    the very run it was minted for. A path outside the guarded root is left
    to ``resolve_workspace``, which creates it on spawn.
    """
    from pathlib import Path
    from hermes_cli.kanban_workspace_policy import (
        configured_root, create_scratch, validate_mount,
    )
    root, require_mount = configured_root()
    if root is None or not require_mount:
        return
    target = Path(path).expanduser()
    if not target.is_relative_to(root):
        return
    row = conn.execute("SELECT mount_path FROM workspace_mount_roots WHERE root=?",
                       (str(root),)).fetchone()
    mount = validate_mount(root, expected_mount=Path(row[0]) if row else None)
    create_scratch(root, target, expected_mount=mount)


def transfer_workspace(conn, parent_id: str, child_id: str) -> None:
    """Called inside create's transaction; no sharing and no live-owner theft."""
    from hermes_cli import kanban_db as kb
    parent = conn.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone()
    if parent is None:
        raise ValueError(f"unknown continuation source: {parent_id}")
    if parent['status'] in ('done', 'archived'):
        raise ValueError(f'{parent_id} is terminal; its workspace may already be GC-owned')
    if kb._task_has_live_run(conn, parent_id):
        raise ValueError(f"cannot transfer live workspace from {parent_id}")
    if not parent["workspace_path"]:
        raise ValueError(f"{parent_id} has no workspace to transfer")
    conn.execute(
        "UPDATE tasks SET workspace_path=?,workspace_kind=?,branch_name=?,origin_host=? WHERE id=?",
        (parent["workspace_path"], parent["workspace_kind"], parent["branch_name"], parent["origin_host"], child_id),
    )
    # The source keeps running (it may be ready/blocked/review), so it gets a
    # fresh path, no branch and no origin: it can never re-resolve to the
    # directory it just gave away, nor wait on state it no longer owns.
    source_fresh = _fresh_path(parent["workspace_path"], f"after-{child_id}")
    _materialize_fresh(conn, source_fresh)
    conn.execute("UPDATE tasks SET workspace_path=?,branch_name=NULL,origin_host=NULL WHERE id=?",
                 (source_fresh, parent_id))
    conn.execute("INSERT OR IGNORE INTO task_links VALUES(?,?,'derived-from')", (parent_id, child_id))
    kb._append_event(conn, parent_id, "workspace_transferred", {"to": child_id, "path": parent["workspace_path"]})
    kb._append_event(conn, child_id, "continues", {"parent": parent_id, "host": parent["origin_host"]})


def interrupt_lost_hosts(conn, states: dict, *, hosts=None, now=None) -> list:
    """Three distinct tick failures interrupt through the fenced timeout path.

    ``states`` is this tick's pool-probe answer (``plan.continuation_states``).
    The counter lives on the run; restart cannot reset it. A state read from
    the host (including an empty dir) proves reachability. ``hosts`` limits
    the count to hosts the probe asked about (None = every host).
    """
    from hermes_cli import kanban_db as kb
    now = int(time.time()) if now is None else int(now)
    lost = []
    rows = conn.execute(
        "SELECT t.*,r.host,r.metadata FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
        "WHERE t.status='running' AND r.host IS NOT NULL AND r.host!='studio'"
    ).fetchall()
    for row in rows:
        if not (row['claim_lock'] or '').startswith(kb._host_prefix()):
            continue
        if hosts is not None and row['host'] not in hosts:
            continue
        metadata = kb._json_dict(row['metadata'])
        last = int(metadata.get('origin_probe_at') or 0)
        if now - last < 45:
            continue
        reachable = states.get((row['host'], row['workspace_path'])) in ('empty', 'nonempty', 'unknown')
        failures = 0 if reachable else int(metadata.get('origin_probe_failures') or 0) + 1
        kb.merge_run_metadata(conn, row['current_run_id'],
                              {'origin_probe_at': now, 'origin_probe_failures': failures})
        if failures < 3 or not row['worker_pid']:
            continue
        termination = kb._terminate_reclaimed_worker(
            row['worker_pid'], row['claim_lock'], started_at=row['worker_started_at'],
            owner_window=kb._worker_owner_window(conn, row['id'], row['worker_pid'], row['current_run_id']),
            conn=conn, task_id=row['id'], run_id=row['current_run_id'],
        )
        if not termination.get('terminated'):
            continue
        with kb.write_txn(conn):
            retry = kb._retry_status_for_run(conn, row['id'])
            changed = conn.execute(
                "UPDATE tasks SET status=?,worker_pid=NULL,worker_started_at=NULL,claim_lock=NULL,claim_expires=NULL "
                "WHERE id=? AND status='running' AND current_run_id=? AND claim_lock IS ?",
                (retry, row['id'], row['current_run_id'], row['claim_lock']),
            ).rowcount
            if changed:
                rid = kb._end_run(conn, row['id'], status='crashed', outcome='host_lost', error='HOST_LOST')
                kb._append_event(conn, row['id'], 'HOST_LOST', {'host': row['host'], 'failures': failures}, run_id=rid)
                lost.append(row['id'])
    return lost


def inventory(conn, task_id: str) -> list:
    """One explicit scan for --drop-origin; failed scans disclose unknown."""
    import shlex
    import subprocess
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_pool as kwp
    row = conn.execute('SELECT origin_host,workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None or not row[0] or not row[1]:
        return []
    pool = kwp.read_pool(kb.kanban_home() / 'fleet', kanban_cfg=kb._board_home_kanban_cfg())
    host = next((h for h in pool.pool_hosts if h.name == row[0]), None)
    if host is None:
        return ['inventory unavailable (origin absent from registry)']
    script = 'import os,json; print(json.dumps(os.listdir(' + repr(row[1]) + ')))'
    try:
        proc = subprocess.run(kwp._ssh_argv(host, 'python3 -c ' + shlex.quote(script)),
                              capture_output=True, text=True, encoding="utf-8", timeout=8, stdin=subprocess.DEVNULL)
        result = json.loads(proc.stdout) if proc.returncode == 0 else None
        if isinstance(result, list) and all(isinstance(p, str) for p in result):
            return result
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ['inventory unavailable (origin unreachable or scan failed)']


def drop_origin(conn, task_id: str, *, actor: str, files: list) -> bool:
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute('SELECT origin_host FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or not row[0]:
            return False
        if kb._task_has_live_run(conn, task_id):
            raise ValueError('cannot drop origin while a worker owns the workspace')
        host = row[0]
        path = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
        # Start fresh means a new directory: the abandoned one stays behind
        # untouched, and a later placement on the same host never reuses it.
        fresh = _fresh_path(path, f"fresh-{int(time.time())}") if path else None
        if fresh:
            _materialize_fresh(conn, fresh)
        conn.execute('UPDATE tasks SET origin_host=NULL,workspace_path=? WHERE id=?', (fresh, task_id))
        kb._append_event(conn, task_id, 'origin_dropped', {'by': actor, 'host': host})
        kb.add_comment(conn, task_id, actor, f'origin {host} dropped; next run uses {fresh}; '
                       f'files left behind in {path}: ' + ', '.join(files))
    return True


def create_hints(conn, task_id: str, body: Optional[str], *, continues=None) -> None:
    import re
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement_reasons as _pr
    for source in sorted(set(re.findall(r"\bt_[0-9a-f]{8}\b", body or ""))):
        if source in (task_id, continues):
            continue
        row = conn.execute("SELECT origin_host FROM tasks WHERE id=?", (source,)).fetchone()
        if row and row[0]:
            message = f"{source} left state on {row[0]}; recreate with --continues {source} to keep it"
            kb.add_comment(conn, task_id, "dispatcher", message)
            kb._append_event(conn, task_id, _pr.PLACEMENT_HINT_EVENT, {"source": source, "host": row[0]})
