"""Exercise the real spawn boundary against the pinned shared-client contract."""
from pathlib import Path
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_headroom as headroom
from hermes_cli.fleet_placement import placement
from tests.hermes_cli.test_kanban_door_dispatch import TABLE


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # the door state root (var/kanban, var/alloc) resolves PLACEMENT_HOME first; an ambient value pointed it at the
    # real ~/.hermes on the reviewer's host
    monkeypatch.setenv("ALLOCATION_HOME", str(tmp_path / "fleet-root"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


class Client:
    """The pinned alloc_client surface (hermes-home 01524a8d), recording calls."""
    class Refused(Exception):
        pass

    class Unreachable(Exception):
        pass

    class TokenUnreadable(Refused):
        pass

    class AuthorizeRefused(Refused):
        def __init__(self, reason, detail=""):
            super().__init__(f"{reason}: {detail}")
            self.reason, self.detail = reason, detail

    def __init__(self):
        self.calls = []
        self.reused = False
        self.authorized = True
        self.raise_on_authorize = None
        self.lookup = {"state": "unknown"}

    def lookup_unit(self, *args):
        self.calls.append(("lookup", args))
        return self.lookup

    def enqueue(self, *args):
        self.calls.append(("enqueue", args))

    def withdraw(self, *args):
        self.calls.append(("withdraw", args))

    def publish_demand(self, *args):
        self.calls.append(("verdict", args))

    def mark_asap(self, *args):
        self.calls.append(("mark_asap", args))

    def authorize_start(self, *args, **kwargs):
        self.calls.append(("authorize", args, kwargs))
        if self.raise_on_authorize is not None:
            raise self.raise_on_authorize
        if not self.authorized:
            return {"authorized": False, "reason": "queued_behind", "detail": ""}
        return {"authorized": True, "reused": self.reused,
                "reservation_id": "same-row", "until": 9999999999}

    def release(self, rid, **kwargs):
        self.calls.append(("release", rid, kwargs))
        return True

    def renew(self, rid, **kwargs):
        self.calls.append(("renew", rid, kwargs))
        return True


@pytest.mark.parametrize("route,entry,expected", [
    ("portable", "dispatch", "dispatch"), ("local", "dispatch", "local"),
    ("pin", "dispatch", "pinned"), ("local", "spawn_now", "forced")])
def test_actual_pid_event_correlates_authorization(board, monkeypatch, route, entry, expected):
    import json
    import subprocess
    import sys
    import time
    client = Client()
    tid, uid, task = _claimed(board, monkeypatch, "evidence")
    original = door.start
    monkeypatch.setattr(door, "start", lambda *a, **kw: original(*a, **kw, client=client))
    monkeypatch.setattr(dispatch, "_book_local_start", lambda *a: None)
    processes = []
    def spawn(*args):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        processes.append(proc)
        return proc.pid
    try:
        state = dispatch._LaunchState()
        assert dispatch._launch_claimed(board, task, "/scratch", board=None,
            spawn=spawn, spawn_kwargs={}, host="ace-ai", pool=None,
            placement_record=lambda: {"route_class": route, "entry": entry}, state=state) == dispatch._LAUNCH_OK
        grant = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                        (tid, door.EVENT)).fetchone()[0])
        event = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spawned'",
                                        (tid,)).fetchone()[0])
        assert event["unit_id"] == uid == grant["unit_id"]
        assert event["reservation_id"] == grant["reservation_id"]
        assert event["host"] == "ace-ai" and event["consumer"] == "kanban"
        assert event["process_id"] == processes[0].pid and event["boundary"] == expected
        assert grant["at"] <= event["at"] <= time.time() < grant["until"]
        assert event["source_ref"].startswith("sha256:")
    finally:
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=5)


def test_local_launch_passes_booked_lease_to_shared_door(board, monkeypatch):
    client = Client()
    tid, uid, task = _claimed(board, monkeypatch, "upgrade-local")
    original = door.start
    monkeypatch.setattr(door, "start", lambda *a, **kw: original(*a, **kw, client=client))
    booked = []
    monkeypatch.setattr(dispatch, "_book_local_start", lambda *a: booked.append(tid) or "local-lease")
    dispatch._launch_claimed(board, task, "/scratch", board=None,
        spawn=lambda *a: None, spawn_kwargs={}, host=None, pool=None,
        placement_record=lambda: {}, state=dispatch._LaunchState())
    assert booked == [tid]
    authorize = next(c for c in client.calls if c[0] == "authorize")
    assert authorize[2]["existing_reservation_id"] == "local-lease"
    assert authorize[2]["existing_consumer"] == "kanban"
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='spawned'", (tid,)).fetchone()


def test_actual_launch_upgrades_once_and_reconciles_without_second_spawn(board, monkeypatch):
    client = Client()
    monkeypatch.setattr(door, "enabled", lambda: True)
    monkeypatch.setattr(door, "canonical_host", lambda host: "ace-ai")
    from hermes_cli.fleet_placement import placement
    monkeypatch.setattr(placement, "load_hosts", lambda: SimpleNamespace(
        footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3)))
    original = door.start
    monkeypatch.setattr(door, "start", lambda *args, **kwargs: original(*args, **kwargs, client=client))
    tid = kb.create_task(board, title="start", assignee=None)
    before = door.unit_id(board, tid)
    task = kb.claim_task(board, tid)
    assert task is not None
    monkeypatch.setattr("hermes_cli.kanban_continuation.record_placement", lambda *args: True)
    launches = []
    args = dict(board=None, spawn=lambda *args: launches.append(tid), spawn_kwargs={}, host="ace-ai",
                pool=None, placement_record=lambda: {}, state=dispatch._LaunchState(), reservation_id="same-row")
    assert dispatch._launch_claimed(board, task, "/scratch", **args) == dispatch._LAUNCH_OK
    assert launches == [tid]
    authorize = next(c for c in client.calls if c[0] == "authorize")
    assert authorize[1] == ("kanban", "ace-ai", before)
    assert authorize[2]["existing_reservation_id"] == "same-row"
    assert authorize[2]["existing_consumer"] == "kanban"
    assert not any(c[0] == "mark_asap" for c in client.calls)
    with pytest.raises(door.DoorHeld, match="already authorized"):
        dispatch._launch_claimed(board, task, "/scratch", **args)
    assert launches == [tid]
    door.maintain(board, client=client)
    assert client.calls[-1][0] == "renew"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._end_run(board, tid, outcome="completed")
    door.maintain(board, client=client)
    assert client.calls[-1] == ("release", "same-row", {"consumer": "kanban@ace-ai"})
    count = len(client.calls)
    door.maintain(board, client=client)
    assert len(client.calls) == count
    # Publication is per board and per explicit card, never parent urgency.
    from hermes_cli import kanban_priority as priority
    table = SimpleNamespace(footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3),
                            resolve=lambda host: host)
    monkeypatch.setattr(placement, "load_hosts", lambda: table)
    child = kb.create_task(board, title="child", assignee=None, parents=[tid])
    priority.publish_waiting(board, {"ace-ai": [{"id": child, "asap": False}]}, client=client)
    assert client.calls[-1][0] == "enqueue"
    child_uid = door.unit_id(board, child)
    client.calls.clear()
    priority.publish_waiting(board, {"ace-ai": []}, client=client)
    # withdraw precedes any enqueue, so a card that moved host leaves the old host first
    assert client.calls[0] == ("withdraw", ("kanban", "ace-ai", child_uid))


def test_failed_spawn_releases_and_held_attempt_keeps_arrival_id(board, monkeypatch):
    client = Client()
    monkeypatch.setattr(door, "enabled", lambda: True)
    monkeypatch.setattr(door, "canonical_host", lambda host: "ace-ai")
    from hermes_cli.fleet_placement import placement
    monkeypatch.setattr(placement, "load_hosts", lambda: SimpleNamespace(
        footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3)))
    tid = kb.create_task(board, title="retry", assignee=None)
    uid = door.unit_id(board, tid)
    task = kb.claim_task(board, tid)
    client.authorized = False
    with pytest.raises(door.DoorHeld):
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("held door must not spawn")
    door.defer(board, task)
    assert door.unit_id(board, tid) == uid
    task = kb.claim_task(board, tid)
    client.authorized = True
    with pytest.raises(RuntimeError, match="spawn failed"):
        with door.start(board, task, "ace-ai", client=client):
            raise RuntimeError("spawn failed")
    assert client.calls[-1] == ("release", "same-row", {"consumer": "kanban@ace-ai"})
    monkeypatch.setattr(door, "enabled", lambda: False)
    client.calls.clear()
    with door.start(board, task, "ace-ai", client=client):
        pass
    assert client.calls == []


def _claimed(board, monkeypatch, title):
    monkeypatch.setattr(door, "enabled", lambda: True)
    monkeypatch.setattr(door, "canonical_host", lambda host: "ace-ai")
    monkeypatch.setattr(door, "need", lambda: (1.0, 3.0))
    tid = kb.create_task(board, title=title, assignee=None)
    uid = door.unit_id(board, tid)
    return tid, uid, kb.claim_task(board, tid)


def test_typed_refusals_follow_the_p1_recovery_rule(board, monkeypatch):
    """hermes-home 9141c8dd AUTHORIZE_REASONS: spent_expired -> new id next run (no spawn, no fault);
    auth/invalid -> config fault, card blocked, never retried blind; not_enqueued -> SAME id re-enqueued."""
    client = Client()
    tid, uid, task = _claimed(board, monkeypatch, "spent")
    client.raise_on_authorize = Client.AuthorizeRefused("spent_expired", "lease gone")
    with pytest.raises(door.DoorCancelled):
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("spent unit must not spawn")
    door.defer(board, task, door.DoorCancelled("x"))
    assert door.unit_id(board, tid) != uid                               # the next attempt is a NEW id
    assert kb.get_task(board, tid).status == "ready"

    tid, uid, task = _claimed(board, monkeypatch, "auth")
    client.raise_on_authorize = Client.AuthorizeRefused("auth", "pair token mismatch")
    with pytest.raises(door.DoorFault) as held:
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("auth fault must not spawn")
    door.defer(board, task, held.value)
    assert kb.get_task(board, tid).status == "blocked"                   # a human fixes config; no blind retry
    from hermes_cli import kanban_priority_faults as faults
    assert faults.latched()                                              # class 2 latches the door
    (faults._root() / faults.STATE_REL).unlink()                         # the operator fixed the config

    tid, uid, task = _claimed(board, monkeypatch, "requeue")
    client.raise_on_authorize = None
    client.calls.clear()
    monkeypatch.setattr(client, "authorize_start", lambda *a, **k: {"authorized": False, "reason": "not_enqueued"})
    with pytest.raises(door.DoorHeld):
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("not enqueued must not spawn")
    assert [c for c in client.calls if c[0] == "enqueue"][-1][1][2] == uid  # the SAME id, twice


def test_lost_reply_is_reconciled_by_lookup_before_any_retry(board, monkeypatch):
    client = Client()
    tid, uid, task = _claimed(board, monkeypatch, "lost")
    client.raise_on_authorize = Client.Unreachable("reply lost")
    client.lookup = {"state": "authorized", "reservation_id": "ghost", "until": 1}
    with pytest.raises(door.DoorCancelled):
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("never spawn on an unreceipted lease")
    assert ("lookup", ("kanban", "ace-ai", uid)) in client.calls
    assert client.calls[-1] == ("release", "ghost", {"consumer": "kanban@ace-ai"})
    client.lookup = {"state": "waiting"}
    client.calls.clear()
    with pytest.raises(door.DoorHeld) as held:
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("unresolved unit must not spawn")
    assert not isinstance(held.value, door.DoorCancelled)                # same id kept for the retry
    assert not any(c[0] == "release" for c in client.calls)


class _OwnerRefusingClient(Client):
    """alloc_client as measured 2026-11-17 02:03:23 / 02:13:33 (t_b18b346e): the pool reservation was booked by this
    process as 'kanban', the door upgraded it to 'kanban@<host>' and lost the reply, and release() then refused on the
    stale process-local owner record before any request: "reservation <id> belongs to 'kanban', not 'kanban@<host>'"."""
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def release(self, rid, **kwargs):
        self.calls.append(("release", rid, kwargs))
        raise self.exc


def _upgrade_start(board, task, client):
    with door.start(board, task, "ace-ai", reservation_id="pool-row", client=client):
        pytest.fail("never spawn on an unreceipted lease")


@pytest.mark.parametrize("path", ["lost_reply", "reused"])
def test_owner_refusal_on_unreceipted_release_cancels_one_start_never_latches(board, monkeypatch, path):
    from hermes_cli import kanban_priority_faults as faults
    msg = "reservation pool-row belongs to 'kanban', not 'kanban@ace-ai'"
    client = _OwnerRefusingClient(Client.Refused(msg))
    tid, uid, task = _claimed(board, monkeypatch, f"owner-{path}")
    if path == "lost_reply":
        client.raise_on_authorize = Client.Unreachable("TimeoutError: POST /demand/authorize")
        client.lookup = {"state": "authorized", "reservation_id": "pool-row", "until": 1}
    else:
        client.reused = True
    with pytest.raises(door.DoorCancelled) as cancelled:
        _upgrade_start(board, task, client)
    assert not isinstance(cancelled.value, door.DoorFault)
    assert msg in str(cancelled.value)
    assert client.calls[-1][0] == "release" and client.calls[-1][2] == {"consumer": "kanban@ace-ai"}
    assert not faults.latched()                                          # the other hosts keep starting
    door.defer(board, task, cancelled.value)
    assert kb.get_task(board, tid).status == "ready"                     # not blocked for a human
    assert door.unit_id(board, tid) != uid                               # a NEW id next run
    # the next start on the board still reaches the door
    tid2, uid2, task2 = _claimed(board, monkeypatch, f"next-{path}")
    plain = Client()
    with door.start(board, task2, "ace-ai", client=plain):
        pass
    assert any(c[0] == "authorize" for c in plain.calls)


def test_unreadable_credential_on_unreceipted_release_still_latches(board, monkeypatch):
    from hermes_cli import kanban_priority_faults as faults
    client = _OwnerRefusingClient(Client.TokenUnreadable("token file kanban@ace-ai: empty"))
    tid, uid, task = _claimed(board, monkeypatch, "token")
    client.raise_on_authorize = Client.Unreachable("reply lost")
    client.lookup = {"state": "authorized", "reservation_id": "pool-row", "until": 1}
    with pytest.raises(door.DoorFault):
        _upgrade_start(board, task, client)
    assert faults.latched()
    (faults._root() / faults.STATE_REL).unlink()


def test_asap_without_operator_capability_never_blocks_a_start(board, monkeypatch):
    """mark_asap needs the asap-operator token; a pair-only dispatcher gets Refused. The start still proceeds at
    the unit's own arrival: urgency may reorder, never hold."""
    from hermes_cli import kanban_priority
    client = Client()
    tid, uid, task = _claimed(board, monkeypatch, "urgent")
    kanban_priority._set_asap(board, tid, True, operator="apollo")

    def refuse(*args):
        client.calls.append(("mark_asap", args))
        raise Client.Refused("asap operator capability required")

    monkeypatch.setattr(client, "mark_asap", refuse)
    spawned = []
    with door.start(board, task, "ace-ai", client=client):
        spawned.append(tid)
    assert spawned == [tid]
    assert ("mark_asap", ("kanban", "ace-ai", uid)) in client.calls


def _install_review_client(monkeypatch, client):
    original = door.start
    monkeypatch.setattr(door, 'start', lambda *a, **kw: original(*a, **kw, client=client))


def _review_launch(board, task, spawn, *, host=None):
    return dispatch._launch_claimed(board, task, '/scratch', board=None, spawn=spawn,
        spawn_kwargs={}, host=host, pool=None,
        placement_record=lambda: {'route_class': 'local', 'entry': 'dispatch'},
        state=dispatch._LaunchState())


def test_repeated_local_launch_must_not_book_an_unowned_second_lease(board, monkeypatch):
    client = Client()
    _, _, task = _claimed(board, monkeypatch, 'repeat-local')
    _install_review_client(monkeypatch, client)
    booked = []
    def book(*args):
        rid = f'local-{len(booked)+1}'
        booked.append(rid)
        with kb.write_txn(board):
            kb._append_event(board, task.id, headroom.LOCAL_ADMITTED_EVENT,
                {headroom.RESERVATION_KEY: rid, 'host': 'local'}, run_id=task.current_run_id)
        return rid
    monkeypatch.setattr(dispatch, '_book_local_start', book)
    def authorize(*a, **kw):
        client.calls.append(('authorize', a, kw))
        return {'authorized': True, 'reused': False, 'reservation_id': kw['existing_reservation_id'],
                'until': time.time()+90}
    monkeypatch.setattr(client, 'authorize_start', authorize)
    launches=[]
    _review_launch(board, task, lambda *a: launches.append(task.id))
    with pytest.raises(door.DoorHeld):
        _review_launch(board, task, lambda *a: launches.append(task.id))
    assert launches == [task.id]
    assert headroom.run_reservation(board, task.id, task.current_run_id) == (None, None)
    assert booked == ['local-1'], f'leaked non-door-owned local capacity: {booked}'


def test_expired_authorization_must_not_spawn(board, monkeypatch):
    client = Client()
    _, _, task = _claimed(board, monkeypatch, 'expired')
    _install_review_client(monkeypatch, client)
    # The reply is valid when received. A local receipt/DB stall crosses its
    # lifetime before yield enters the actual process launch.
    monkeypatch.setattr(client, 'authorize_start', lambda *a, **k: {
        'authorized': True, 'reused': False, 'reservation_id': 'expired-lease', 'until': time.time()+0.05})
    receipt = door._receipt
    def slow_receipt(*a):
        receipt(*a)
        time.sleep(0.1)
    monkeypatch.setattr(door, '_receipt', slow_receipt)
    starts=[]
    with pytest.raises(door.DoorCancelled):
        _review_launch(board, task, lambda *a: starts.append(task.id), host='ace-ai')
    assert starts == [], 'started after the authorization lifetime ended'
    assert any(call[0] == 'release' and call[1] == 'expired-lease' for call in client.calls)
    assert board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                         (task.id, door.RELEASED)).fetchone()


def test_lost_claim_actual_start_keeps_authorization_join_fields(board, monkeypatch):
    client=Client()
    _, _, task=_claimed(board, monkeypatch, 'late')
    with door.start(board, task, 'ace-ai', client=client):
        pass
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready',current_run_id=NULL WHERE id=?", (task.id,))
    assert not dispatch._set_worker_pid(board, task.id, 424242, run_id=task.current_run_id,
        placement={'route_class':'local','entry':'dispatch'})
    payload=json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spawned'",
                                   (task.id,)).fetchone()[0])
    assert payload['late_spawn'] and payload['claim_lost']
    assert {'host','unit_id','reservation_id','process_id','at','boundary','source_ref'} <= payload.keys()


def test_receipt_timestamp_honestly_labelled_and_no_pre_spawn_start(board, monkeypatch):
    client=Client()
    _, _, task=_claimed(board, monkeypatch, 'positive')
    _install_review_client(monkeypatch, client)
    def spawn(*a):
        assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='spawned'",(task.id,)).fetchone()
        return 424242
    _review_launch(board, task, spawn, host='ace-ai')
    grant=json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                  (task.id,door.EVENT)).fetchone()[0])
    assert grant['at_source']=='authorization_receipt_received'


def test_actual_local_ledger_does_not_leak_on_repeated_authorized_launch(board, monkeypatch):
    client=Client()
    _, _, task=_claimed(board, monkeypatch, 'real-repeat')
    monkeypatch.setattr(door,'canonical_host',lambda host:'mac-studio')
    root=placement.fleet_root()
    (root/'config').mkdir(parents=True)
    (root/'config'/'fleet-hosts.json').write_text(json.dumps(TABLE))
    original=door.start
    monkeypatch.setattr(door,'start',lambda *a,**kw:original(*a,**kw,client=client))
    def authorize(*a,**kw):
        return {'authorized':True,'reused':False,'reservation_id':kw['existing_reservation_id'], 'until':9999999999}
    monkeypatch.setattr(client,'authorize_start',authorize)
    args=dict(board=None,spawn=lambda *a:None,spawn_kwargs={},host=None,pool=None,
              placement_record=lambda:{},state=dispatch._LaunchState())
    dispatch._launch_claimed(board,task,'/scratch',**args)
    rpath=root/placement.RESERVATIONS_REL
    before=json.loads(rpath.read_text())['rows']
    assert len(before)==1
    with pytest.raises(door.DoorHeld):
        dispatch._launch_claimed(board,task,'/scratch',**args)
    rows=json.loads(rpath.read_text())['rows']
    assert len(rows)==1, f'duplicate real live capacity rows: {rows}'
