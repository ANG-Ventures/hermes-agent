"""A review card approved at its PR head with a land in flight is TERMINAL-PENDING (t_26ebe71a).

2026-10-13 07:00-08:30: seven review cards with an APPROVE at the current head and a land queued had their
15-min operator review claim expire while the reviewer waited on CI. The TTL sweep reclaimed each one, scored
it as a worker failure (``gave_up``: failures 2-3 vs effective_limit 2, ``retry_status=review``) and the owner
got ``stuck`` / ``gave_up`` wakes for finished cards. One fixture per rule:

(a) the sweep EXTENDS a terminal-pending review claim instead of reclaiming it;
(b) a review claim that does lapse goes back to ``review`` without touching the retry counter;
(c) the owner wake withholds ``stuck`` on a terminal-pending card with a ``terminal-pending`` receipt.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway import kanban_owner_wake as ow
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_terminal_pending as tp
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

HEAD = "3806e0d8a3f043f550f0d5858e89926f5cd5129b"
OTHER = "fad526c44b7ef3a83fc6586f73b3bc1305634196"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return root


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _approve(conn, task_id, head=HEAD, verdict="APPROVE"):
    rec = {"verdict": verdict, "head_sha": head, "reviewer": "human:apollo",
           "lenses": {name: "done" for name in REQUIRED_REVIEW_LENSES}, "findings": 0, "items": [],
           "review_minutes": 5, "batch_id": "n/a: test"}
    kb.add_comment(conn, task_id, "default", "review_coverage: " + json.dumps(rec))


def _operator_review_claim(conn, title="approved, landing"):
    """The 10-13 shape: ``claim --review`` from a CLI pid that has exited, no worker pid, claim expired."""
    tid = kb.create_task(conn, title=title, assignee="builder")
    impl = kb.claim_task(conn, tid)
    assert impl
    assert kb.request_review(conn, tid, summary="ready", reviewer="human", expected_run_id=impl.current_run_id)
    host = kb._claimer_id().rpartition(":")[0]
    assert kb.claim_review_task(conn, tid, claimer=f"{host}:{_dead_pid()}", operator_claim=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 5, tid))
    return tid


def _land_request(root: Path, card: str, head=HEAD, suffix=".json", done=False, ulid="01TESTLANDREQ"):
    d = root / "var" / "land-requests"
    d.mkdir(parents=True, exist_ok=True)
    req = {"ulid": ulid, "repo": "ANG-Ventures/hermes-home", "pr": 4049, "review_head_sha": head,
           "card": card, "created_at": time.time()}
    (d / f"{ulid}{suffix}").write_text(json.dumps(req))
    if done:
        (d / f"{ulid}.done.json").write_text(json.dumps(req))
        (d / f"{ulid}.result.json").write_text(json.dumps({"ulid": ulid, "status": "FAILED", "rc": 9,
                                                           "finished_at": time.time()}))
    return d


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, task_id=tid)]


# --- (a) the TTL sweep extends a terminal-pending review claim ---------------------------------------------


def test_sweep_extends_approved_review_claim_with_open_land_request(home):
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        _land_request(home, tid)
        assert kb.release_stale_claims(conn, failure_limit=1) == 0
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.claim_expires > time.time()
        ext = [e for e in kb.list_events(conn, task_id=tid) if e.kind == "claim_extended"][-1]
        assert ext.payload["reason"] == "terminal_pending"
        assert ext.payload["land"]["source"] == "land-request"
        assert "reclaimed" not in _kinds(conn, tid) and "gave_up" not in _kinds(conn, tid)


@pytest.mark.parametrize("intent", ["inflight", "parked", "armed", "land-watch", "land-queue"])
def test_every_open_land_intent_counts(home, intent):
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        held = None
        if intent == "inflight":
            _land_request(home, tid, suffix=".inflight")
        elif intent == "parked":
            _land_request(home, tid, suffix=".parked.json")
        elif intent == "armed":
            d = _land_request(home, tid, suffix=".done.json")
            (d / "01TESTLANDREQ.result.json").write_text(json.dumps(
                {"ulid": "01TESTLANDREQ", "status": "ARMED", "rc": 0, "finished_at": time.time()}))
        elif intent == "land-watch":
            d = home / "var" / "land-watch"
            d.mkdir(parents=True)
            (d / "ANG-Ventures_hermes-home-4049.pin.json").write_text(json.dumps({"card": tid, "head": HEAD}))
            held = open(d / "ANG-Ventures_hermes-home-4049.lock", "w")
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            d = home / "state" / "apollo-land-queue"
            d.mkdir(parents=True)
            (d / "1-x-4049.json").write_text(json.dumps({"id": "1-x-4049", "card": tid, "repo": "o/r", "pr": 4049,
                                                         "status": "pending", "merging_sha": HEAD,
                                                         "enqueued_at": time.time()}))
        try:
            assert tp.terminal_pending(conn, tid) is not None
            assert kb.release_stale_claims(conn) == 0
        finally:
            if held:
                held.close()


@pytest.mark.parametrize("shape", ["no-land", "land-finished", "land-other-head", "approve-other-head",
                                   "newest-is-sendback", "land-watch-dead", "land-cancelled"])
def test_not_terminal_pending_is_reclaimed_as_before(home, shape):
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid, head=OTHER if shape == "approve-other-head" else HEAD)
        if shape == "newest-is-sendback":
            _approve(conn, tid, verdict="CHANGES_REQUESTED")
        if shape in ("land-finished",):
            _land_request(home, tid, suffix=".done.json", done=True)
        elif shape == "land-other-head":
            _land_request(home, tid, head=OTHER)
        elif shape == "land-cancelled":
            d = _land_request(home, tid)
            (d / "01TESTLANDREQ.cancel.json").write_text("{}")
        elif shape in ("approve-other-head", "newest-is-sendback"):
            _land_request(home, tid)
        elif shape == "land-watch-dead":
            d = home / "var" / "land-watch"
            d.mkdir(parents=True)
            (d / "o_r-1.pin.json").write_text(json.dumps({"card": tid, "head": HEAD}))
            (d / "o_r-1.lock").write_text("pid=1")
        assert tp.terminal_pending(conn, tid) is None
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "review"
        assert "reclaimed" in _kinds(conn, tid)


@pytest.mark.parametrize("shape", ["request-json", "request-inflight", "request-parked-mtime", "queue-row"])
def test_stale_open_land_intent_ages_out(home, shape):
    """Prism 798c56e8cf8c: a lander / queue process that died and left its file behind must not extend the claim
    forever. Every open intent is bounded by ARMED_HORIZON_S (row timestamp, else file mtime)."""
    old = time.time() - tp.ARMED_HORIZON_S - 60
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        if shape == "queue-row":
            d = home / "state" / "apollo-land-queue"
            d.mkdir(parents=True)
            (d / "q.json").write_text(json.dumps({"id": "q", "card": tid, "status": "pending",
                                                  "merging_sha": HEAD, "enqueued_at": old}))
        else:
            suffix = {"request-json": ".json", "request-inflight": ".inflight",
                      "request-parked-mtime": ".parked.json"}[shape]
            d = _land_request(home, tid, suffix=suffix)
            path = d / f"01TESTLANDREQ{suffix}"
            if shape == "request-parked-mtime":   # no created_at: the file's mtime decides
                req = json.loads(path.read_text())
                req.pop("created_at")
                path.write_text(json.dumps(req))
                os.utime(path, (old, old))
            else:
                req = json.loads(path.read_text())
                req["created_at"] = old
                path.write_text(json.dumps(req))
        assert tp.terminal_pending(conn, tid) is None
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "review"


@pytest.mark.parametrize("value", ["Infinity", "inf", "nan", "-1", 0, -1, 1e18, "1e18"])
@pytest.mark.parametrize("where", ["request", "queue-row", "armed-result"])
def test_non_finite_or_nonpositive_timestamp_is_not_fresh(home, where, value):
    """Prism 3bc827d1f2e5: float('Infinity') parses and ``now - inf`` is always inside the horizon, so an abandoned
    request carrying it extended the claim on every sweep. The file is FRESH by mtime here: the bad value itself
    must be what makes it stale, not a fallback."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        if where == "queue-row":
            d = home / "state" / "apollo-land-queue"
            d.mkdir(parents=True)
            (d / "q.json").write_text(json.dumps({"id": "q", "card": tid, "status": "pending",
                                                  "merging_sha": HEAD, "enqueued_at": value}))
        elif where == "request":
            path = _land_request(home, tid) / "01TESTLANDREQ.json"
            req = json.loads(path.read_text())
            req["created_at"] = value
            path.write_text(json.dumps(req))
        else:
            d = _land_request(home, tid, suffix=".done.json")
            (d / "01TESTLANDREQ.result.json").write_text(json.dumps(
                {"ulid": "01TESTLANDREQ", "status": "ARMED", "rc": 0, "finished_at": value}))
        assert tp.terminal_pending(conn, tid) is None
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "review"


@pytest.mark.parametrize("value", ["", None, "missing"])
def test_empty_or_missing_timestamp_falls_back_to_mtime(home, value):
    """An absent/empty created_at uses the file mtime (old mtime = stale, fresh mtime = fresh); it never reads as
    fresh by itself."""
    old = time.time() - tp.ARMED_HORIZON_S - 60
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        path = _land_request(home, tid) / "01TESTLANDREQ.json"
        req = json.loads(path.read_text())
        if value == "missing":
            req.pop("created_at")
        else:
            req["created_at"] = value
        path.write_text(json.dumps(req))
        assert tp.terminal_pending(conn, tid) is not None
        os.utime(path, (old, old))
        assert tp.terminal_pending(conn, tid) is None
        assert kb.release_stale_claims(conn) == 1


def test_finite_result_beside_a_non_finite_one_still_counts(home):
    """A non-finite finished_at skips that entry only. For the SAME PR it must not win the newest-result sort
    (inf > any real time) and hide the valid ARMED result."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        d = _land_request(home, tid, suffix=".done.json", ulid="01BAD")
        (d / "01BAD.result.json").write_text(json.dumps({"ulid": "01BAD", "status": "ARMED",
                                                         "finished_at": "Infinity"}))
        _land_request(home, tid, suffix=".done.json", ulid="01GOOD")
        (d / "01GOOD.result.json").write_text(json.dumps({"ulid": "01GOOD", "status": "ARMED",
                                                          "finished_at": time.time()}))
        pending = tp.terminal_pending(conn, tid)
        assert pending is not None and pending["land"]["id"] == "01GOOD"


@pytest.mark.parametrize("row", [
    {"status": "pending"},                                    # no verified head yet
    {"status": None, "rc_posted_sha": OTHER},                 # verified, but a different (unapproved) head
])
def test_queue_row_needs_the_verified_approved_head(home, row):
    """Prism 7c8e2b974c90: a queue row waits on whatever head the PR carries; without a verified sha equal to the
    APPROVE's head it is not a land of the approved head."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        d = home / "state" / "apollo-land-queue"
        d.mkdir(parents=True)
        (d / "q.json").write_text(json.dumps(dict(row, id="q", card=tid, enqueued_at=time.time())))
        assert tp.terminal_pending(conn, tid) is None


def test_module_imports_and_fails_open_without_fcntl(home, monkeypatch):
    """Prism 9686b2f83fad: native Windows has no fcntl. The module must import and treat a pin as not live."""
    code = ("import sys; sys.modules['fcntl'] = None\n"
            "from hermes_cli import kanban_terminal_pending as tp\n"
            "import pathlib, tempfile\n"
            "p = pathlib.Path(tempfile.mkdtemp()) / 'x.lock'; p.write_text('')\n"
            "assert tp.fcntl is None and tp._lock_held(p) is False\n"
            "print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[2]))
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# --- (b) a review-claim reclaim never counts toward the implementation retry budget -------------------------


def test_review_claim_lapse_does_not_count_or_give_up(home):
    with kb.connect() as conn:
        tid = _operator_review_claim(conn, "no land yet")
        for _ in range(3):   # 10-13: three lapses tripped gave_up at failures 2-3 vs limit 2
            assert kb.release_stale_claims(conn, failure_limit=1) == 1
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.consecutive_failures == 0
            host = kb._claimer_id().rpartition(":")[0]
            assert kb.claim_review_task(conn, tid, claimer=f"{host}:{_dead_pid()}", operator_claim=True)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 5, tid))
        assert "gave_up" not in _kinds(conn, tid)


def test_dispatcher_reviewer_run_lapse_still_counts(home, monkeypatch):
    """Prism c54bb0d8dbb3 / 035dc68f1c41: a dispatcher-claimed reviewer run (no ``operator_claim``) whose spawn never
    stamped a pid is a launch failure. Its reclaim counts toward the breaker, is never extended, and its owner
    wake is not withheld as a lease lapse."""
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "1")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reviewer launch failed", assignee="builder")
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human", expected_run_id=impl.current_run_id)
        _approve(conn, tid)
        _land_request(home, tid)          # approved + land open: still not an operator lease, so no extension
        host = kb._claimer_id().rpartition(":")[0]
        assert kb.claim_review_task(conn, tid, claimer=f"{host}:{_dead_pid()}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 5, tid))
            conn.execute("UPDATE task_runs SET started_at = started_at - 100 WHERE task_id = ? AND id = "
                         "(SELECT current_run_id FROM tasks WHERE id = ?)", (tid, tid))
        assert kb.release_stale_claims(conn, failure_limit=1) == 1
        kinds = _kinds(conn, tid)
        assert "claim_extended" not in kinds
        assert "gave_up" in kinds
        rec = [e for e in kb.list_events(conn, task_id=tid) if e.kind == "reclaimed"][-1]
        assert rec.payload.get("dead_claimer_release_basis") != "operator_claim_no_worker"
        assert not ow.is_review_lease_lapse({"kind": "reclaimed", "payload": rec.payload})


def test_implementation_claim_lapse_still_counts(home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        assert kb.claim_task(conn, tid)
        from hermes_cli import kanban_db_dispatch as kbd
        kbd._set_worker_pid(conn, tid, 999_998)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 5, tid))
        assert kb.release_stale_claims(conn, failure_limit=1) == 1
        assert "gave_up" in _kinds(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


# --- (c) stuck / gave_up owner wakes on a terminal-pending card become a receipt ----------------------------


def _items(conn, tid):
    return [{"event_id": e.id, "kind": e.kind, "trigger": ow.TRIGGER_STUCK, "payload": e.payload,
             "actor_sid": "", "actor_profile": "", "event_ts": e.created_at}
            for e in kb.list_events(conn, task_id=tid) if e.kind in ("gave_up", "reclaimed")]


def _gave_up_card(conn):
    tid = _operator_review_claim(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "gave_up", {"failures": 2, "effective_limit": 2, "retry_status": "review"})
    return tid


def test_owner_wake_withholds_stuck_on_terminal_pending_with_receipt(home):
    with kb.connect() as conn:
        tid = _gave_up_card(conn)
        _approve(conn, tid)
        _land_request(home, tid)
        items = _items(conn, tid)
        other = {"event_id": 1, "kind": "completed", "trigger": ow.TRIGGER_LAST_BLOCKER}
        card = {"board": kb.DEFAULT_BOARD, "task_id": tid}
        kept = ow.terminal_pending_items(kb, card, items + [other], "review")
        assert kept == [other], "only the stuck items are withheld"
        receipts = [e.payload for e in kb.list_events(conn, task_id=tid) if e.kind == "notify_suppressed"]
        assert [(r["kind"], r["reason"], r["path"]) for r in receipts] == [("gave_up", tp.REASON, "owner-wake")]
        from hermes_cli import kanban_show_suppressed as kss
        rows = kss.suppressed_rows(kb.list_events(conn, task_id=tid))
        assert rows and rows[0]["reason"] == "terminal-pending"


def test_owner_wake_withholds_pidless_review_lease_lapse(home):
    """The real 10-13 07:11 payload: a reclaim of an operator review claim, heartbeat_stale from the old run."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        assert kb.release_stale_claims(conn) == 1
        items = _items(conn, tid)
        assert items and items[0]["payload"]["retry_status"] == "review"
        assert items[0]["payload"]["worker_pid"] is None
        assert ow.terminal_pending_items(kb, {"board": kb.DEFAULT_BOARD, "task_id": tid}, items, "review") == []
        receipts = [e.payload for e in kb.list_events(conn, task_id=tid) if e.kind == "notify_suppressed"]
        assert [(r["kind"], r["reason"]) for r in receipts] == [("reclaimed", tp.REASON_LEASE_LAPSE)]


_OP = {"dead_claimer_release_basis": "operator_claim_no_worker"}


@pytest.mark.parametrize("payload", [
    dict(_OP, retry_status="review", worker_pid=4242),       # a spawned reviewer worker died: stuck is real
    dict(_OP, retry_status="ready", worker_pid=None),        # an implementation claim
    dict(_OP, retry_status="review"),                        # no worker_pid field: unknown, fail open
    {"retry_status": "review", "worker_pid": None},          # NULL pid, no operator provenance: a failed launch
    {"retry_status": "review", "worker_pid": None, "dead_claimer_release_basis": "launch_bound"},
])
def test_lease_lapse_is_only_the_operator_review_claim(payload):
    assert not ow.is_review_lease_lapse({"kind": "reclaimed", "payload": dict(payload, heartbeat_stale=True)})
    assert ow.is_review_lease_lapse({"kind": "reclaimed", "payload": dict(_OP, retry_status="review",
                                                                            worker_pid=None)})
    assert not ow.is_review_lease_lapse({"kind": "crashed", "payload": {"retry_status": "review", "worker_pid": None}})


@pytest.mark.parametrize("shape", ["no-land", "not-approved", "card-blocked"])
def test_owner_wake_keeps_stuck_when_not_terminal_pending(home, shape):
    with kb.connect() as conn:
        tid = _gave_up_card(conn)
        if shape != "not-approved":
            _approve(conn, tid)
        if shape != "no-land":
            _land_request(home, tid)
        items = _items(conn, tid)
        status = "blocked" if shape == "card-blocked" else "review"
        assert ow.terminal_pending_items(kb, {"board": kb.DEFAULT_BOARD, "task_id": tid}, items, status) == items
        assert "notify_suppressed" not in _kinds(conn, tid)


# --- Prism round 3 ------------------------------------------------------------------------------------------


def test_extended_workerless_lease_holds_no_worker_slot(home, monkeypatch, all_assignees_spawnable):
    """Prism 5207920d8aa5: an extended terminal-pending lease stays ``running`` (review ownership kept) but the
    board, host and per-profile worker counters do not charge it; a real running worker still counts."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        _land_request(home, tid)
        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, tid).status == "running"
        assert kbd.count_running_tasks(conn) == 0
        assert kb._count_running_strict(conn) == 0
        assert kb.count_running_tasks_host() == 0
        # Per-profile cap 1 for the lease's assignee: the ready card of that profile still spawns.
        ready = kb.create_task(conn, title="next", assignee=kb.get_task(conn, tid).assignee)
        spawns: list = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawns.append(task.id) or 42,
                                max_in_progress=1, max_in_progress_per_profile=1)
        assert [t for t, *_ in res.spawned] == [ready]
        assert kb.get_task(conn, tid).status == "running", "the lease keeps the card"
        assert kbd.count_running_tasks(conn) == 1, "a real worker still counts"


def test_unextended_pidless_review_claim_still_counts(home):
    """Only a lease this module positively extended is excluded; an unexpired operator review claim counts."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) + 600, tid))
        assert kbd.count_running_tasks(conn) == 1


@pytest.mark.parametrize("kind", ["human", "machine", "default-human"])
def test_operator_sendback_withdraws_the_earlier_approve(home, kind):
    """Prism 43b2117caf62: an operator send-back writes no coverage record. A human one withdraws the APPROVE until
    a newer APPROVE supersedes it; a machine send-back never holds a landing, so it does not."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        _land_request(home, tid)
        assert tp.approve_head(conn, tid) == HEAD
        payload = {"reason": "wrong repo", "operator": "apollo: wrong repo"}
        if kind != "default-human":
            payload["operator_kind"] = kind
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "changes_requested", payload)
        if kind == "machine":
            assert tp.approve_head(conn, tid) == HEAD
            return
        assert tp.approve_head(conn, tid) is None
        assert tp.terminal_pending(conn, tid) is None
        assert kb.release_stale_claims(conn) == 1          # not extended
        time.sleep(1.05)                                   # created_at is whole seconds; a newer APPROVE wins
        _approve(conn, tid)
        assert tp.approve_head(conn, tid) == HEAD


def test_real_request_changes_operator_sendback_withdraws(home, monkeypatch):
    """The same through the real ``request_changes(operator=...)`` path on a card parked in review."""
    from hermes_cli import kanban_identity as _ki

    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(_ki, "_runs_under_operator_gateway", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="approved then bounced", assignee="builder")
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human", expected_run_id=impl.current_run_id)
        _approve(conn, tid)
        _land_request(home, tid)
        assert tp.approve_head(conn, tid) == HEAD
        ok, why = kb.request_changes(conn, tid, reason="rebase first", claimer="apollo",
                                     operator="Ace via Apollo: rebase first")
        assert ok, why
        assert tp.approve_head(conn, tid) is None


def test_one_malformed_result_file_does_not_disable_the_lookup(home):
    """Prism d6f16f45db8f: a non-numeric ``finished_at`` in one result is skipped; another ARMED land still counts."""
    with kb.connect() as conn:
        tid = _operator_review_claim(conn)
        _approve(conn, tid)
        d = _land_request(home, tid, suffix=".done.json", ulid="01ARMED")
        (d / "01ARMED.result.json").write_text(json.dumps(
            {"ulid": "01ARMED", "status": "ARMED", "rc": 0, "finished_at": time.time()}))
        bad = dict(json.loads((d / "01ARMED.done.json").read_text()), ulid="01BAD", pr=9999)
        (d / "01BAD.done.json").write_text(json.dumps(bad))
        (d / "01BAD.result.json").write_text(json.dumps({"ulid": "01BAD", "status": "ARMED", "finished_at": "soon"}))
        assert tp.terminal_pending(conn, tid) is not None
