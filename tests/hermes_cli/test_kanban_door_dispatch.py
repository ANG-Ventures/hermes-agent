"""Door fault taxonomy through the REAL dispatch tick (dispatch_once), not the adapters (P4 round 2, t_0c5ef02e).

Apollo ruling 2026-11-05 19:30: class 1 registry absent -> spawn-only and loud (one page); class 2 config fault ->
card blocked (capability), door latched, no door call until token/table/registry change; class 3 transport ->
held on a capped backoff, one page past 10 min; spawn-only is today's dispatcher. Real alloc_client, real host
table and real priority registry in a temp fleet root; allocd itself is a recording fake or a closed port.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli.fleet_placement import alloc_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_priority_faults as faults

TABLE = {
    "schema": 1, "default_first": "mac-studio", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1}},
    "hosts": {"mac-studio": {"cores": 32, "threads": 32, "mem_total_gb": 512, "mem_reserve_gb": 64,
                             "cores_reserve": 4, "consumers": {"kanban": {"slots": 8}}, "clock_mhz": 4000,
                             "aliases": ["studio", "local"]}},
}


class Allocd:
    """A loopback HTTP allocd stand-in: ``answer(path, body) -> (code, doc)``; every request is recorded."""

    def __init__(self):
        self.calls = []
        self.answer = lambda path, body: (500, {})
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append((self.path, body))
                code, doc = outer.answer(self.path, body)
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

    def paths(self):
        return [p for p, _ in self.calls]


def ok_door(clock=time.time):
    """Answers every door verb the kanban client uses, authorizing each unit once."""
    n = [0]

    def answer(path, body):
        base = {"schema": 1, "workload": body.get("workload"), "host": body.get("host")}
        if path == "/demand/authorize":
            n[0] += 1
            return 200, {**base, "id": body["id"], "reservation_id": f"rid-{n[0]}", "until": clock() + 90,
                         "reused": False}
        if path in ("/release", "/renew"):
            return 200, {"ok": True, "released": True, "renewed": True}
        return 200, {**base, "ok": True, "door": {"at": clock()}}
    return answer


def _closed_port() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    root = tmp_path / "fleet-root"
    (root / "fleet").mkdir(parents=True)
    (root / "config").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PLACEMENT_HOME", str(root))
    monkeypatch.setenv("ALLOCD_URL", _closed_port())
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    pages = []
    monkeypatch.setattr(faults, "_page", lambda text: pages.append(text) or True)
    kb.init_db()
    ns = type("Fleet", (), {})()
    ns.root, ns.pages = root, pages

    def registry(mode):
        (root / "fleet" / "admission-priority.json").write_text(
            json.dumps({"schema": 1, "priority_mode": mode}), encoding="utf-8")

    def table():
        (root / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")

    def token():
        d = root / alloc_client.TOKENS_REL
        d.mkdir(parents=True, exist_ok=True)
        (d / "kanban@mac-studio").write_text("pair-token\n", encoding="utf-8")
        # Local capacity accounting is upgraded in place; authorization authenticates
        # the existing row's owner as well as the host/workload pair.
        (d / "kanban").write_text("capacity-owner-token\n", encoding="utf-8")

    ns.registry, ns.table, ns.token = registry, table, token
    return ns


def _tick(spawned, **kw):
    def spawn(task, workspace, *a, **k):
        spawned.append(task.id)
        return 424242
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=False, default_assignee="default", **kw)


def _card(title="c"):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="default")


def _status(tid):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid)


def test_spawn_only_is_todays_dispatcher_with_no_token_and_allocd_down(fleet):
    fleet.registry("spawn-only")
    fleet.table()
    tid = _card()
    spawned = []
    t0 = time.monotonic()
    _tick(spawned)
    assert spawned == [tid]
    assert _status(tid).status == "running"
    assert fleet.pages == []
    assert time.monotonic() - t0 < 30


def test_missing_registry_is_spawn_only_and_pages_once_across_two_ticks(fleet):
    """Class 1 (reader repro: kanban_priority_door.py:35-37): no registry, door config present -> the card still
    spawns, a warning every tick, exactly ONE page; the page clears when the file is back."""
    fleet.table()
    fleet.token()
    a, spawned = _card("a"), []
    _tick(spawned)
    b = _card("b")
    _tick(spawned)
    assert spawned == [a, b]
    assert len(fleet.pages) == 1 and "registry missing" in fleet.pages[0]
    fleet.registry("spawn-only")
    _tick(spawned)
    assert len(fleet.pages) == 2 and "back" in fleet.pages[1]


@pytest.mark.parametrize("arm", ["missing_token", "missing_table", "auth"])
def test_config_fault_blocks_capability_latches_and_is_not_reclaimed(fleet, monkeypatch, arm):
    """Class 2 (reader arms, door.py:132-151, 185-201): the card is blocked kind=capability (not returned to ready
    and reclaimed every tick), the door is latched (no door call on the next tick), ONE page; changing the
    token/table/registry clears the latch."""
    fleet.registry("fcfs")
    if arm != "missing_table":
        fleet.table()
    if arm != "missing_token":
        fleet.token()
    allocd = Allocd()
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    allocd.answer = (lambda path, body: (403, {"error": "pair token mismatch"})) if arm == "auth" else ok_door()
    tid, spawned = _card(), []
    _tick(spawned)
    task = _status(tid)
    assert spawned == []
    assert task.status == "blocked" and task.block_kind == "capability"
    assert len(fleet.pages) == 1 and "config fault" in fleet.pages[0]
    n = len(allocd.calls)
    other = _card("other")
    _tick(spawned)
    assert spawned == []
    assert _status(tid).status == "blocked" and _status(other).status == "ready"
    assert len(allocd.calls) == n, "a latched door makes no door call"
    assert len(fleet.pages) == 1
    # the operator fixes the config: the latch clears, the next tick starts the other card
    allocd.answer = ok_door()
    fleet.table()
    fleet.token()
    _tick(spawned)
    assert spawned == [other]


def test_transport_fault_holds_same_id_on_capped_backoff_and_pages_after_10_min(fleet, monkeypatch):
    """Class 3: allocd unreachable WITH a token -> held (never blocked), same unit id, door verbs only when the
    backoff elapses (doubling from the tick interval to 300 s), ONE page past 10 min, cleared by the first success."""
    fleet.registry("fcfs")
    fleet.table()
    fleet.token()
    clock = [time.time()]
    monkeypatch.setattr(faults, "_now", lambda: clock[0])
    allocd = Allocd()
    allocd.answer = lambda path, body: (503, {"error": "router off"})
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    tid, spawned = _card(), []
    with kbc.connect_closing() as conn:
        uid = door.unit_id(conn, tid)
    _tick(spawned)
    assert spawned == [] and _status(tid).status == "ready"
    first = len(allocd.calls)
    assert first > 0
    _tick(spawned)                                        # inside the backoff: no door call
    assert len(allocd.calls) == first
    intervals = []
    for _ in range(8):
        t = json.loads((fleet.root / faults.STATE_REL).read_text())["transport"]["publish"]   # full outage: publish fails first
        intervals.append(t["interval"])
        clock[0] = t["next_at"] + 1
        _tick(spawned)
        assert _status(tid).status == "ready"
    assert intervals[:2] == [60.0, 120.0] and max(intervals) == faults.TRANSPORT_MAX_BACKOFF_S
    assert len(fleet.pages) == 1 and "unreachable" in fleet.pages[0]
    assert spawned == []
    with kbc.connect_closing() as conn:
        assert door.unit_id(conn, tid) == uid, "a transport hold keeps the arrival id"
    allocd.answer = ok_door()
    clock[0] += faults.TRANSPORT_MAX_BACKOFF_S + 1
    _tick(spawned)
    assert spawned == [tid]
    assert len(fleet.pages) == 2 and "again" in fleet.pages[1]
    auth = [b for p, b in allocd.calls if p == "/demand/authorize"]
    assert [b["id"] for b in auth] == [uid]


def test_partial_outage_publish_and_lookup_succeed_authorize_503_backs_off_and_pages(fleet, monkeypatch):
    """Round-3 reader repro (kanban_priority.py:181, kanban_priority_door.py:123-124): publish and lookup answer,
    authorize_start returns 503 for 732 s. Transport health is per verb: a successful publish or lookup must NOT
    clear the authorize history, so the backoff doubles to 300 s and exactly one page fires past 600 s; the first
    successful authorize clears it (one clear page)."""
    fleet.registry("fcfs")
    fleet.table()
    fleet.token()
    t0 = time.time()
    clock = [t0]
    monkeypatch.setattr(faults, "_now", lambda: clock[0])
    allocd = Allocd()
    ok = ok_door()
    authorize_up = [False]

    def answer(path, body):
        if path == "/demand/authorize" and not authorize_up[0]:
            return 503, {"error": "authorize down"}
        if path == "/demand/lookup":
            return 200, {"schema": 1, "workload": body["workload"], "host": body["host"], "id": body["id"],
                         "state": "waiting"}
        return ok(path, body)
    allocd.answer = answer
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    tid, spawned = _card(), []
    intervals = []
    while clock[0] - t0 <= 732:
        _tick(spawned)
        t = json.loads((fleet.root / faults.STATE_REL).read_text()).get("transport", {})
        assert "publish" not in t, t
        t = t.get("authorize")
        assert t, "a successful publish/lookup cleared the authorize transport history"
        intervals.append(t["interval"])
        clock[0] = t["next_at"] + 1
    assert "/demand/lookup" in allocd.paths() and "/demand/publish" in allocd.paths()
    assert spawned == [] and _status(tid).status == "ready"
    assert intervals[:3] == [60.0, 120.0, 240.0] and intervals[-1] == faults.TRANSPORT_MAX_BACKOFF_S
    assert len(fleet.pages) == 1 and "unreachable" in fleet.pages[0]
    authorize_up[0] = True
    _tick(spawned)
    assert spawned == [tid]
    state = json.loads((fleet.root / faults.STATE_REL).read_text())
    assert not any(k.startswith("transport") for k in state), state
    assert len(fleet.pages) == 2 and "again" in fleet.pages[1]


def test_publish_only_outage_pages_once_at_first_failed_retry_past_600s(fleet, monkeypatch):
    """Round-4 reader repro (kanban_priority_faults.py:213-216): with only publish 503 across 9 attempts the failing
    verb's record was rebuilt before its `paged` flag was read, so every retry past 10 min re-paged (5 pages). A verb
    pages only on its unpaged -> paged transition. The page fires on the first FAILED retry after 600 s (here ~720 s:
    the check runs only when a retry fails), never on a timer."""
    fleet.registry("fcfs")
    fleet.table()
    fleet.token()
    t0 = time.time()
    clock = [t0]
    monkeypatch.setattr(faults, "_now", lambda: clock[0])
    allocd = Allocd()
    ok = ok_door()
    allocd.answer = lambda path, body: (503, {"error": "publish down"}) if path == "/demand/publish" else ok(path, body)
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    tid, spawned = _card(), []
    page_at = None
    for _ in range(9):
        _tick(spawned)
        t = json.loads((fleet.root / faults.STATE_REL).read_text())["transport"]
        assert set(t) == {"publish"}, t
        if fleet.pages and page_at is None:
            page_at = clock[0] - t0
        clock[0] = t["publish"]["next_at"] + 1
    assert spawned == [] and _status(tid).status == "ready"
    assert len(fleet.pages) == 1, fleet.pages
    assert "publish unreachable" in fleet.pages[0]
    assert faults.TRANSPORT_PAGE_AFTER_S < page_at <= faults.TRANSPORT_PAGE_AFTER_S + faults.TRANSPORT_MAX_BACKOFF_S


_KILLED_TICK = """
import os, signal
from hermes_cli import kanban_db_connect as kbc, kanban_db_dispatch as kbd
def die(task, workspace, *a, **k):
    os.kill(os.getpid(), signal.SIGKILL)   # the gateway dies after the door receipt, before any worker exists
with kbc.connect_closing() as conn:
    kbd.dispatch_once(conn, spawn_fn=die, dry_run=False, default_assignee="default")
"""


def test_fcfs_start_consumes_one_authorization_and_survives_restart(fleet, monkeypatch):
    """An fcfs tick authorizes ONE unit at the start boundary before it spawns. Restart drill: a real dispatcher
    process is SIGKILLed after the durable door receipt and before the spawn; the restarted dispatcher (this
    process) never spawns that run, releases its lease, and spawns the card exactly once on a NEW unit id."""
    import os
    import signal
    import subprocess
    import sys
    fleet.registry("fcfs")
    fleet.table()
    fleet.token()
    allocd = Allocd()
    allocd.answer = ok_door()
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    tid = _card()
    # a subprocess has no conftest: hand it this test's provisioned root receipt (hermes-home #9789)
    from hermes_cli import fleet_placement
    receipt = fleet_placement.admission_demand._receipt_path(Path(os.environ["PLACEMENT_HOME"]).absolute())
    proc = subprocess.run([sys.executable, "-c", _KILLED_TICK], env=dict(os.environ, PLACEMENT_ROOT_RECEIPT=str(receipt)), timeout=120,
                          capture_output=True, text=True)
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]
    auth = [b for p, b in allocd.calls if p == "/demand/authorize"]
    assert len(auth) == 1
    first_uid = auth[0]["id"]
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                            (tid, door.EVENT)).fetchone()[0] == 1
        assert kb.get_task(conn, tid).status == "running"
    spawned = []
    _tick(spawned)
    # Inside the dead-claimer launch bound an unstamped orphan may still exist: the restarted dispatcher neither
    # respawns the run nor drops its lease (the lifecycle sweep renews it).
    assert spawned == [] and "/release" not in allocd.paths()
    with kbc.connect_closing() as conn:
        # Age the dead process's claim and run past the claim TTL and the launch bound (what a real restart waits).
        old = int(time.time()) - kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS - 60
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (old, tid))
            conn.execute("UPDATE task_runs SET started_at=?, claim_expires=? WHERE task_id=?", (old, old, tid))
    for _ in range(3):
        r = _tick(spawned)
    with kbc.connect_closing() as conn:
        dbg = (kb.get_task(conn, tid).status, [(e.kind, e.payload) for e in kb.list_events(conn, tid)][-3:],
               r.placement_waits, allocd.paths()[-8:])
    assert spawned == [tid], f"exactly one spawn after the restart {dbg}"
    auth = [b for p, b in allocd.calls if p == "/demand/authorize"]
    assert len(auth) == 2 and auth[1]["id"] != first_uid
    assert "/release" in allocd.paths(), "the never-spawned run's lease is released"


def test_spawn_only_aggregate_publish_is_fire_and_forget(fleet, monkeypatch):
    """Spawn-only advisory publish (kanban_load_gate.py publish_ready_demand): a wedged allocd (accepts, never answers)
    costs the tick at most the 1 s budget over EVERY host, never raises, and is counted."""
    from hermes_cli import kanban_load_gate as klg
    fleet.registry("spawn-only")
    doc = json.loads(json.dumps(TABLE))
    for name in ("ci-box", "ace-ai", "ace-media"):
        doc["hosts"][name] = {**TABLE["hosts"]["mac-studio"], "aliases": []}
    (fleet.root / "config" / "fleet-hosts.json").write_text(json.dumps(doc), encoding="utf-8")
    d = fleet.root / alloc_client.TOKENS_REL
    d.mkdir(parents=True, exist_ok=True)
    for name in doc["hosts"]:
        (d / f"kanban@{name}").write_text("pair-token\n", encoding="utf-8")
    wedged = socket.socket()
    wedged.bind(("127.0.0.1", 0))
    wedged.listen(16)                                     # connects succeed; no reply ever comes
    monkeypatch.setenv("ALLOCD_URL", f"http://127.0.0.1:{wedged.getsockname()[1]}")
    before = dict(klg.PUBLISH_STATS)
    try:
        t0 = time.monotonic()
        err = klg.publish_ready_demand(3)
        took = time.monotonic() - t0
    finally:
        wedged.close()
    assert took < klg.PUBLISH_BUDGET_S + 0.5, took
    assert err                                            # reported to the caller, not raised
    counted = {k: klg.PUBLISH_STATS[k] - before[k] for k in before}
    assert counted["ok"] == 0 and counted["failed"] + counted["skipped"] == len(doc["hosts"])


def _spawn_only_fleet(fleet, monkeypatch, hosts, tokened):
    """A spawn-only fleet table with ``hosts`` (sorted publish order) and a pair token for each name in ``tokened``."""
    fleet.registry("spawn-only")
    doc = json.loads(json.dumps(TABLE))
    doc["hosts"] = {name: {**TABLE["hosts"]["mac-studio"], "aliases": []} for name in hosts}
    doc["default_first"] = hosts[-1]
    (fleet.root / "config" / "fleet-hosts.json").write_text(json.dumps(doc), encoding="utf-8")
    d = fleet.root / alloc_client.TOKENS_REL
    d.mkdir(parents=True, exist_ok=True)
    for name in tokened:
        (d / f"kanban@{name}").write_text("pair-token\n", encoding="utf-8")
    allocd = Allocd()
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    return allocd


def _queue_ok(slow=(), delay=1.5):
    def answer(path, body):
        if body.get("host") in slow:
            time.sleep(delay)                             # the local door's write outlasts the pass budget
        return 200, {"schema": 1, "workload": body.get("workload"), "host": body.get("host"), "ok": True}
    return answer


def test_spawn_only_publish_tokenless_host_is_not_a_failure_and_is_asked_again_once_tokened(fleet, monkeypatch,
                                                                                            caplog):
    """A host with no local publisher token next to a tokened one: every pass returns None, one log line names it,
    and it is still checked every pass, so a token written between pass 2 and pass 3 is published on pass 3 with
    no restart and no reset of module state."""
    import logging
    from hermes_cli import kanban_load_gate as klg
    allocd = _spawn_only_fleet(fleet, monkeypatch, ["mbp-arm", "studio"], tokened=["studio"])
    allocd.answer = _queue_ok()
    errs = []
    with caplog.at_level(logging.INFO, logger=klg.__name__):
        for _ in range(3):
            errs.append(klg.publish_ready_demand(1))
    assert errs == [None, None, None], errs
    named = [r for r in caplog.records if "mbp-arm" in r.getMessage()]
    assert len(named) == 1, [r.getMessage() for r in caplog.records]
    # provision it now: token file + protected marker, as alloc_client._token requires
    (fleet.root / alloc_client.TOKENS_REL / "kanban@mbp-arm").write_text("pair-token\n", encoding="utf-8")
    marker = fleet.root / alloc_client.PROTECTED_REL / "kanban@mbp-arm"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    assert klg.publish_ready_demand(1) is None
    published = [b["host"] for p, b in allocd.calls if p == "/demand/publish"]
    assert published.count("mbp-arm") == 1 and published.count("studio") == 4, published


def test_spawn_only_publish_slow_door_text_is_unchanged(fleet, monkeypatch):
    """The first write outlasts the 1.0 s budget: the hosts after it read "skipped (publish budget 1.0 s spent)",
    exactly as on main (the publish stays sequential on one shared budget)."""
    from hermes_cli import kanban_load_gate as klg
    hosts = ["a-slow", "h2", "h3"]
    allocd = _spawn_only_fleet(fleet, monkeypatch, hosts, tokened=hosts)
    allocd.answer = _queue_ok(slow={"a-slow"})
    err = klg.publish_ready_demand(2)
    assert err and err.startswith("a-slow: TimedOut"), err   # alloc_client.TimedOut(Unreachable), hermes-home main
    for h in ("h2", "h3"):
        assert f"{h}: skipped (publish budget {klg.PUBLISH_BUDGET_S} s spent)" in err, err


def test_spawn_only_publish_logs_no_failed_line_when_every_host_answers(fleet, monkeypatch, caplog):
    """All reachable and tokened: zero "failed" lines over several passes, and None (the gate tick's caller,
    gateway/kanban_gate_tick.py publish_demand, logs "failed" only for a non-None return)."""
    import logging
    from hermes_cli import kanban_load_gate as klg
    hosts = ["ace-ai", "mac-studio", "nas"]
    allocd = _spawn_only_fleet(fleet, monkeypatch, hosts, tokened=hosts)
    allocd.answer = _queue_ok()
    monkeypatch.setattr(klg, "unified_admit", lambda env=None: True)
    errs = []
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            errs.append(klg.publish_ready_demand(4, root=fleet.root))
    assert errs == [None, None, None], errs
    assert not [r for r in caplog.records if "failed" in r.getMessage()], [r.getMessage() for r in caplog.records]
    assert sorted(b["host"] for p, b in allocd.calls if p == "/demand/publish") == sorted(hosts * 3)
