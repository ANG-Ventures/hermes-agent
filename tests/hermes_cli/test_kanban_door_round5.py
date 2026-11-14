"""P4 round 5 (t_0c5ef02e): the four dispatcher-door invariants, through the REAL dispatch_once and the real
alloc_client. allocd is a loopback stand-in whose /demand/publish runs the vendored admission_demand.store_door, so
the door's own rule (one live unit id per workload, a second host's enqueue is 409 invalid) is the real one.

A. every start boundary goes through the door (review loop included); the publisher waits a card where the
   dispatcher starts it, and a host move withdraws the old unit first: no self-inflicted latch.
B. the published-unit record survives a real process restart.
C. ASAP is one state in two stores, operator-only, both directions.
D. a latch clears when the credential that actually failed is repaired.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_door_dispatch import TABLE, fleet  # noqa: F401  (fixture)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_priority_faults as faults
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli.fleet_placement import admission_demand, alloc_client

POOL = "ace-ai"
PORTABLE = "Refactor the parser and add tests."
OP_TOKEN = "op-token-for-tests"


class DoorAllocd:
    """Stateful allocd stand-in: /demand/publish -> store_door (the vendored, canonical merge); authorize grants
    a unit waiting on that host unless ``hold`` is set; lookup reads the same store."""

    def __init__(self, root):
        self.root, self.calls, self.hold, self.n = root, [], False, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append((self.path, body))
                code, doc = outer.route(self.path, body)
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def store(self):
        try:
            return json.loads((self.root / admission_demand.DEMAND_REL / "kanban.json").read_text())
        except OSError:
            return {}

    def waiting(self, host):
        return {u["id"]: u for u in self.store().get("by_host", {}).get(host, {}).get("waiting", [])}

    def route(self, path, body):
        base = {"schema": 1, "workload": body.get("workload"), "host": body.get("host")}
        if path == "/demand/publish":
            try:
                state = admission_demand.store_door(body, owner=f"{body['workload']}@{body['host']}", root=self.root)
            except admission_demand.DoorConflict as exc:
                return (403 if exc.reason == "auth" else 409), {"error": "door refused", "reason": exc.reason}
            return 200, {**base, "ok": True, "door": state}
        if path == "/demand/authorize":
            if body["id"] not in self.waiting(body["host"]):
                return 409, {"reason": "not_enqueued"}
            if self.hold:
                return 409, {"reason": "queued_behind"}
            self.n += 1
            return 200, {**base, "id": body["id"], "reservation_id": f"rid-{self.n}", "until": time.time() + 90,
                         "reused": False}
        if path == "/demand/lookup":
            unit = self.waiting(body["host"]).get(body["id"])
            return 200, {**base, "id": body["id"], "state": "waiting" if unit else "unknown"}
        if path in ("/release", "/renew"):
            return 200, {"ok": True, "released": True, "renewed": True}
        return 404, {}


def _pool_table(fleet):
    doc = json.loads(json.dumps(TABLE))
    doc["hosts"][POOL] = {**TABLE["hosts"]["mac-studio"], "aliases": []}
    (fleet.root / "config" / "fleet-hosts.json").write_text(json.dumps(doc), encoding="utf-8")
    d = fleet.root / alloc_client.TOKENS_REL
    d.mkdir(parents=True, exist_ok=True)
    for name in doc["hosts"]:
        (d / f"kanban@{name}").write_text("pair-token\n", encoding="utf-8")
    (d / "kanban").write_text("capacity-owner-token\n", encoding="utf-8")


def _plan(*, remote=True):
    host = kwp.PoolHost(name=POOL, ssh_host=POOL, ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                        absence="required", profiles=("default",), state="active", enabled=True, priority=0)
    cfg = kwp.PoolConfig(hosts=(POOL,), profiles=("default",), pool_hosts=(host,))
    p = kwp.plan([host], {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason, p.default_remote = "admitting", None, remote
    return p


@pytest.fixture
def door_fleet(fleet, monkeypatch):
    fleet.registry("fcfs")
    _pool_table(fleet)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    allocd = DoorAllocd(fleet.root)
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    fleet.allocd = allocd
    return fleet


def _tick(spawned, plan=None, *, local_full=False):
    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "local"))
        return 424242
    kw = dict(spawn_limit=0, spawn_paused="load gate: paused") if local_full else {}
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=False, default_assignee="default", max_spawn=64,
                                 spillover=plan, reconcile_orphans=False, **kw)


def _card(body=PORTABLE):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="c", assignee="default", body=body)


def _uid(tid, offset=0):
    with kbc.connect_closing() as conn:
        return door.unit_id(conn, tid, offset=offset)


# -- A: placement agreement -------------------------------------------------------------------------------------

def test_portable_card_with_local_full_and_default_remote_starts_once_on_the_pool_host(door_fleet):
    """Prism a51c53f3bd02 + 23ed3fd0945c. RED on 1f9b456d: the publisher enqueued the unit on mac-studio
    (local-first), the start enqueued the same uid on ace-ai, store_door said 409 invalid, the whole door latched."""
    tid, spawned = _card(), []
    _tick(spawned, _plan(), local_full=True)
    assert faults.latched() is None, faults.latched()
    assert spawned == [(tid, POOL)]
    assert not any(u.startswith("kb-") for u in door_fleet.allocd.waiting("mac-studio")), "zero live ids on local"
    auth = [b for p, b in door_fleet.allocd.calls if p == "/demand/authorize"]
    assert [(b["host"], b["id"]) for b in auth] == [(POOL, _uid(tid, -1))]


def test_a_door_conflict_on_our_own_unit_is_reconciled_never_latched(door_fleet):
    """The owner record is gone (a different board file, an operator wipe): the start still finds the stale
    old-host unit by lookup, withdraws it and enqueues on the start host. RED on 1f9b456d: latched."""
    tid = _card()
    uid = _uid(tid)
    alloc_client.enqueue("kanban", "mac-studio", uid, 1.0, 3.0)        # a stale unit no record names
    spawned = []
    _tick(spawned, _plan(), local_full=True)
    assert faults.latched() is None
    assert spawned == [(tid, POOL)]
    assert uid not in door_fleet.allocd.waiting("mac-studio")


def test_review_loop_start_goes_through_the_door(door_fleet):
    """Prism 7851057bf109: the live review loop called _spawn directly. RED on 1f9b456d: spawned with no
    authorize call while the door held the unit."""
    tid = _card("review me")
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    door_fleet.allocd.hold = True
    spawned = []
    _tick(spawned)
    assert spawned == []
    # a held run keeps its arrival id (door_held runs are not counted), so the next attempt reuses it
    assert [b["id"] for p, b in door_fleet.allocd.calls if p == "/demand/authorize"] == [_uid(tid)]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"            # held, back to its lane, no penalty
    door_fleet.allocd.hold = False
    _tick(spawned)
    assert spawned == [(tid, "local")]
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                            (tid, door.EVENT)).fetchone()[0] == 1


# -- B: ownership survives a restart; every destination validated ----------------------------------------------

_HELD_TICK = """
from hermes_cli import kanban_db_connect as kbc, kanban_db_dispatch as kbd
def spawn(task, workspace, *a, **k):
    raise SystemExit("must not spawn: allocd holds the unit")
with kbc.connect_closing() as conn:
    kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=False, default_assignee="default")
"""


def test_a_card_that_moves_host_after_a_real_restart_is_withdrawn_from_the_old_host(door_fleet):
    """Prism 39f15c5b2c09. A real dispatcher process publishes the card on mac-studio and exits; this process (the
    restart) places it on the pool. RED on 1f9b456d: the process-local record was empty, nothing was withdrawn,
    the pool enqueue got 409 invalid and the door latched."""
    tid = _card()
    door_fleet.allocd.hold = True
    # a subprocess has no conftest: hand it this test's provisioned root receipt (hermes-home #9789)
    from hermes_cli import fleet_placement
    receipt = fleet_placement.admission_demand._receipt_path(Path(os.environ["PLACEMENT_HOME"]).absolute())
    proc = subprocess.run([sys.executable, "-c", _HELD_TICK], env=dict(os.environ, PLACEMENT_ROOT_RECEIPT=str(receipt)), timeout=120,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _uid(tid) in door_fleet.allocd.waiting("mac-studio")
    door_fleet.allocd.hold = False
    n = len(door_fleet.allocd.calls)
    spawned = []
    _tick(spawned, _plan(), local_full=True)
    assert faults.latched() is None, faults.latched()
    assert spawned == [(tid, POOL)]
    assert _uid(tid, -1) not in door_fleet.allocd.waiting("mac-studio")
    assert "/demand/lookup" not in [p for p, _ in door_fleet.allocd.calls[n:]], "the durable record, not a search"


def test_a_waiting_host_the_table_does_not_name_is_a_config_latch_not_a_silent_success(door_fleet):
    """Prism ba4c7620e475. RED on 1f9b456d: the host was skipped, publish reported success, no latch."""
    from hermes_cli import kanban_priority as priority
    tid = _card()
    with kbc.connect_closing() as conn:
        errors = priority.publish_waiting(conn, {"nowhere-box": [{"id": tid, "asap": False}]})
    assert errors and faults.latched() and "nowhere-box" in faults.latched()
    assert not any(p == "/demand/publish" for p, _ in door_fleet.allocd.calls)


# -- C: ASAP, both stores, operator-only ------------------------------------------------------------------------

def _set_asap(conn, tid, flag):
    """Signature-agnostic, so the red-first run on the round-4 head fails on BEHAVIOUR: there set_asap took a
    caller-supplied ``operator`` string, and the caller here claims ``apollo``."""
    import inspect
    from hermes_cli.kanban_priority import set_asap
    kw = {"operator": "apollo"} if "operator" in inspect.signature(set_asap).parameters else {}
    return set_asap(conn, tid, flag, **kw)


@pytest.fixture
def operator(door_fleet, monkeypatch):
    (kb.kanban_home() / "kanban").mkdir(parents=True, exist_ok=True)
    kb.operator_token_path().write_text(OP_TOKEN + "\n", encoding="utf-8")
    (door_fleet.root / alloc_client.TOKENS_REL / alloc_client.ASAP_OPERATOR).write_text("asap\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, OP_TOKEN)
    return door_fleet


def test_clearing_asap_clears_the_door_unit_in_the_same_operation(operator):
    """Prism 2d873aa15a6e + 82278e729600. RED on 1f9b456d: --no-asap cleared SQLite only and the unit kept
    asap=True at the door (store_door preserves it on refresh)."""
    tid = _card()
    operator.allocd.hold = True
    with kbc.connect_closing() as conn:
        assert _set_asap(conn, tid, True)
    _tick([])
    assert operator.allocd.waiting("mac-studio")[_uid(tid)]["asap"] is True
    with kbc.connect_closing() as conn:
        assert _set_asap(conn, tid, False)
    assert operator.allocd.waiting("mac-studio")[_uid(tid)]["asap"] is False     # no tick in between
    # publication reconciles the other direction too: a door unit marked urgent behind the card's back is cleared
    alloc_client.mark_asap("kanban", "mac-studio", _uid(tid))
    _tick([])
    assert operator.allocd.waiting("mac-studio")[_uid(tid)]["asap"] is False


def test_a_worker_ancestry_process_cannot_set_asap_even_with_its_task_env_unset(operator, monkeypatch):
    """Prism b37716b1f3e9. RED on 1f9b456d: any nonempty operator string was accepted, so a worker that unset
    HERMES_KANBAN_TASK marked its own card."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    tid = _card()
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    with kbc.connect_closing() as conn:
        conn.execute("INSERT INTO task_runs (task_id, profile, status, worker_pid, worker_started_at, started_at) "
                     "VALUES (?, 'daedalus', 'running', ?, ?, 0)", (tid, os.getpid(), _process_fingerprint(os.getpid())))
        conn.commit()
        with pytest.raises(PermissionError, match="daedalus"):
            _set_asap(conn, tid, True)
        assert kb.get_task(conn, tid).asap is False


def test_an_unproven_operator_label_cannot_set_asap(operator, monkeypatch):
    tid = _card()
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    with kbc.connect_closing() as conn:
        with pytest.raises(PermissionError, match="'default' holds no operator token"):
            _set_asap(conn, tid, True)
        assert kb.get_task(conn, tid).asap is False


# -- D: the latch fingerprint covers every credential the door reads -------------------------------------------

def test_repairing_the_owner_reservation_token_clears_the_latch(door_fleet):
    """Prism d5a47f67cca8: the reservation upgrade authenticates with the token named exactly ``kanban``. RED on
    1f9b456d: the fingerprint read only kanban@* files, so repairing ``kanban`` never cleared the latch."""
    alloc_client.mark_protected("kanban")                    # a protected consumer whose token is missing
    (door_fleet.root / alloc_client.TOKENS_REL / "kanban").unlink()
    tid = _card()
    with kbc.connect_closing() as conn:
        task = kb.claim_task(conn, tid)
        with pytest.raises(door.DoorFault):
            with door.start(conn, task, None, reservation_id="rid-booked"):
                pytest.fail("a config fault must not spawn")
    assert faults.latched()
    (door_fleet.root / alloc_client.TOKENS_REL / "kanban").write_text("owner-token\n", encoding="utf-8")
    assert faults.latched() is None


def test_faults_module_imports_without_fcntl(monkeypatch):
    """Prism 1c125b8e5419: an unconditional fcntl import broke importing the dispatcher on native Windows."""
    import importlib
    monkeypatch.setitem(sys.modules, "fcntl", None)
    importlib.reload(faults)


# -- round 6 (Prism at 7e57a9c9) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("token", [False, True])
def test_a_worker_dispatched_as_an_operator_profile_cannot_set_asap(operator, monkeypatch, token):
    """Prism f1dbe6687476. A kanban worker whose profile is ``apollo`` unsets HERMES_KANBAN_TASK: ancestry is checked
    first, by the process walk, so the operator label (and even the token) never admits it. RED on 7e57a9c9 without
    the token: verified_profile_author returned the worker's profile ``apollo``, an operator label, and ASAP was set."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    tid = _card()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if not token:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    with kbc.connect_closing() as conn:
        conn.execute("INSERT INTO task_runs (task_id, profile, status, worker_pid, worker_started_at, started_at) "
                     "VALUES (?, 'apollo', 'running', ?, ?, 0)", (tid, os.getpid(), _process_fingerprint(os.getpid())))
        conn.commit()
        with pytest.raises(PermissionError, match="kanban worker 'apollo'"):
            _set_asap(conn, tid, True)
        assert kb.get_task(conn, tid).asap is False


def test_an_operator_label_without_the_operator_token_cannot_set_asap(operator, monkeypatch):
    """Round 6: the label is necessary, never sufficient; the operator token is required in addition."""
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    monkeypatch.setattr("hermes_cli.kanban_identity._runs_under_operator_gateway", lambda name: True)
    with kbc.connect_closing() as conn:
        with pytest.raises(PermissionError, match="no operator token"):
            _set_asap(conn, tid, True)
        assert kb.get_task(conn, tid).asap is False


@pytest.mark.parametrize("lost", ["host_left_table", "pair_token_gone"])
def test_a_stale_record_entry_on_an_unreachable_host_is_dropped_never_latched(door_fleet, caplog, lost):
    """Prism 7e404c636d6f. The durable record names a unit on a host that left the host table (or whose kanban@<host>
    token is gone). RED on 7e57a9c9: every publish withdrew it, got Refused and latched class 2, forever."""
    import logging
    from hermes_cli import kanban_priority as priority
    tid = _card()
    uid = _uid(tid)
    with kbc.connect_closing() as conn:
        door.save_published(conn, {(POOL, uid)})
    if lost == "host_left_table":
        path = door_fleet.root / "config" / "fleet-hosts.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["hosts"][POOL]
        path.write_text(json.dumps(doc), encoding="utf-8")
    else:
        (door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}").unlink()
    caplog.set_level(logging.WARNING, logger="hermes_cli.kanban_priority")
    with kbc.connect_closing() as conn:
        for _ in range(2):                                   # the second publish must stay clean too
            assert priority.publish_waiting(conn, {}) == {}
            assert faults.latched() is None, faults.latched()
        assert door.load_published(conn) == set()
    message = "dropped published unit" if lost == "host_left_table" else "pending withdrawal of published unit"
    dropped = [r for r in caplog.records if message in r.getMessage()]
    assert len(dropped) == 1 and uid in dropped[0].getMessage()


# -- CI-completion round 2 (Prism at 605a9c86) ---------------------------------------------------------------------

@pytest.mark.platforms("posix")
def test_a_permission_only_token_repair_clears_the_latch(door_fleet):
    """Prism 917208e77049. chmod leaves mtime and size alone. RED on 605a9c86: the fingerprint hashed only those,
    so a pair token repaired from mode 000 to 600 stayed latched."""
    token = door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}"
    token.chmod(0o000)
    try:
        faults.latch("pair token unreadable")
        assert faults.latched()
        token.chmod(0o600)
        assert faults.latched() is None
    finally:
        token.chmod(0o600)


def test_a_root_receipt_repair_clears_the_latch(door_fleet, monkeypatch, tmp_path):
    """Prism f6e8a5c31293: a latch caused by missing root provisioning clears once PLACEMENT_ROOT_RECEIPT is set,
    and again when the receipt file it names is repaired."""
    monkeypatch.delenv("PLACEMENT_ROOT_RECEIPT", raising=False)
    faults.latch("PLACEMENT_ROOT_RECEIPT required")
    assert faults.latched()
    receipt = tmp_path / "placement-root.json"
    monkeypatch.setenv("PLACEMENT_ROOT_RECEIPT", str(receipt))
    assert faults.latched() is None                       # env repaired: a fresh door attempt is allowed
    faults.latch("receipt unreadable")
    assert faults.latched()
    receipt.write_text("{}", encoding="utf-8")
    assert faults.latched() is None                       # receipt file created: cleared again


def test_an_idle_publish_does_not_clear_a_publish_outage(door_fleet):
    """Prism 0056dc604414. RED on 605a9c86: with nothing waiting and nothing recorded, publish_waiting made no allocd
    call yet recorded publish success, so an idle tick after the backoff cleared the outage and paged recovery."""
    from hermes_cli import kanban_priority as priority
    faults.note_transport("demand publication unavailable", "publish")
    with faults._state() as doc:
        doc["transport"]["publish"]["next_at"] = 0           # backoff expired; the next tick may publish
        doc["transport"]["publish"]["paged"] = True
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {}) == {}
    with faults._state() as doc:
        assert "publish" in faults._transport(doc)
    assert not any("reachable again" in p for p in door_fleet.pages)


def test_real_review_spawn_does_not_retry_after_post_launch_typeerror(door_fleet):
    from hermes_cli import kanban_db_dispatch as dispatch
    tid=_card('review me')
    with kbc.connect_closing() as conn,kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?",(tid,))
    import subprocess
    import sys
    starts=[]
    def spawn(task,workspace,*,board=None):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])
        starts.append(process)
        if len(starts)==1:
            raise TypeError('after external process creation')
        return process.pid
    try:
        with kbc.connect_closing() as conn:
            dispatch.dispatch_once(conn,spawn_fn=spawn,dry_run=False,default_assignee='default',
                                   max_spawn=64,reconcile_orphans=False)
        auth=[b for p,b in door_fleet.allocd.calls if p=='/demand/authorize']
        assert len(auth)==1
        assert len(starts)==1, f'one shared authorization started two real children: {[p.pid for p in starts]}'
    finally:
        for process in starts:
            process.terminate()
            process.wait(timeout=5)
