"""Shared-checkout admission hold (t_e8017c37).

Core contract tests drive the real HoldStore/AdmissionGate against a temp
directory; modality tests drive the real ingress seams (gateway messaging
``_handle_message``, API ``_admit_api_agent_request``, cron ``tick``, serve
``handle_request`` incl. a real long-lived WebSocket through
``tui_gateway.ws.handle_ws``).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time

import pytest

import gateway.checkout_admission as ca
from gateway.checkout_admission import (
    BUSY, DEFER, DRAINED, NOT_HELD, QUIESCENT, UNKNOWN,
    AdmissionGate, AdmissionRefused, HoldConflict, HoldStore, HoldUnreadable,
)

GW = "gateway:default"
GW2 = "gateway:clanker"
SERVE = "serve:clanker"


@pytest.fixture
def store(tmp_path):
    return HoldStore(tmp_path / "adm")


@pytest.fixture(autouse=True)
def _reset_process_gates():
    ca._reset_process_gates_for_tests()
    yield
    ca._reset_process_gates_for_tests()


class Work:
    """Stand-in for a consumer's authoritative in-process busy state."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        return {"agents": self.n}


def _gate(store, name, work=None):
    return AdmissionGate(store, name, active_work=work or Work())


# ---------------------------------------------------------------- basics
def test_open_by_default_admits_and_counts(store):
    g = _gate(store, GW)
    t = g.admit("m1")
    rec = g.publish()
    assert rec["hold_epoch"] is None and len(rec["tickets"]) == 1
    t.release()
    t.release()  # idempotent
    assert g.snapshot()["tickets"] == []
    assert ca.evaluate(store)["verdict"] == NOT_HELD


def test_hold_refuses_external_immediately_without_any_poll(store):
    g = _gate(store, GW)
    store.hold("op", [GW])
    with pytest.raises(AdmissionRefused) as exc:
        g.admit("late")
    assert "held" in exc.value.reason and exc.value.epoch


def test_drain_admits_and_counts_internal_freeze_refuses_everything(store):
    g = _gate(store, GW)
    store.hold("op", [GW], mode="drain")
    t = g.admit("replay", internal=True)
    g.publish()
    assert ca.evaluate(store)["verdict"] == BUSY
    t.release()
    g.publish()
    assert ca.evaluate(store)["verdict"] == DRAINED  # not mutation authority
    store.hold("op", [GW], mode="freeze")
    with pytest.raises(AdmissionRefused):
        g.admit("replay2", internal=True)
    assert ca.evaluate(store)["verdict"] == UNKNOWN  # new epoch not acked yet
    g.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


# ------------------------------------------------ late arrival, two arms
def _operator_cutover(store, cutover):
    """Operator policy under test: mutate ONLY on QUIESCENT."""
    rep = ca.evaluate(store)
    if rep["verdict"] == QUIESCENT:
        cutover()
    return rep


def test_late_arrival_after_last_idle_poll_held_arm(store):
    work = Work()
    g = _gate(store, GW, work)
    store.hold("op", [GW], mode="freeze")
    g.publish()  # the LAST idle poll: acked, zero work
    # A turn arrives immediately after that poll, before the operator acts.
    with pytest.raises(AdmissionRefused):
        g.admit("late-turn")
    cut = []
    rep = _operator_cutover(store, lambda: cut.append(1))
    # The late turn never started, so cutting over disturbs nothing.
    assert rep["verdict"] == QUIESCENT and cut == [1]
    assert g.snapshot()["tickets"] == [] and work.n == 0


def test_late_arrival_after_last_idle_poll_control_arm_without_hold(store):
    g = _gate(store, GW)
    g.publish()  # last idle poll: zero work -- the #382 prototype would act here
    t = g.admit("late-turn")  # no hold -> admitted
    rec = g.publish()
    assert len(rec["tickets"]) == 1  # the next observation sees active work
    cut = []
    rep = _operator_cutover(store, lambda: cut.append(1))
    assert rep["verdict"] == NOT_HELD and cut == []  # no hold, no cutover
    t.release()


def test_turn_that_won_the_race_is_counted_by_the_ack_and_defers(store):
    g = _gate(store, GW)
    t = g.admit("won-race")  # admitted a hair before the hold is written
    store.hold("op", [GW], mode="freeze")
    g.publish()
    cut = []
    assert _operator_cutover(store, lambda: cut.append(1))["verdict"] == BUSY
    assert cut == []
    t.release()


def test_concurrent_admissions_never_escape_the_acknowledged_snapshot(store):
    """Stress: many threads admit while the operator holds; after an ack of
    the hold epoch reports N tickets, no admission beyond those N exists."""
    g = _gate(store, GW)
    admitted, refused = [], []
    stop = threading.Event()

    def spam():
        while not stop.is_set():
            try:
                admitted.append(g.admit("x"))
            except AdmissionRefused:
                refused.append(1)

    threads = [threading.Thread(target=spam) for _ in range(8)]
    for th in threads:
        th.start()
    time.sleep(0.05)
    store.hold("op", [GW], mode="freeze")
    rec = g.publish()
    n_at_ack = len(rec["tickets"])
    time.sleep(0.05)
    stop.set()
    for th in threads:
        th.join()
    assert rec["hold_epoch"] is not None
    assert len(admitted) == n_at_ack  # nothing admitted after the ack
    assert refused


def test_admission_read_and_register_is_atomic_against_the_ack(store):
    """Deterministic interleaving: an admission has READ 'open' but not yet
    registered when the operator holds and the consumer acknowledges. The ack
    must wait for (and count) that admission, never report zero under it."""
    read_open = threading.Event()
    proceed = threading.Event()
    orig = store.read_hold

    def slow_read():
        state = orig()
        if threading.current_thread().name == "late-turn":
            read_open.set()
            proceed.wait(0.5)
        return state

    store.read_hold = slow_read
    g = _gate(store, GW)
    tickets = []
    th = threading.Thread(target=lambda: tickets.append(g.admit("late")), name="late-turn")
    th.start()
    assert read_open.wait(5)
    store.hold("op", [GW], mode="freeze")
    acked = {}
    pub = threading.Thread(target=lambda: acked.update(g.publish()))
    pub.start()
    time.sleep(0.1)
    proceed.set()
    th.join(5)
    pub.join(5)
    assert tickets, "the admission that read 'open' completes"
    assert acked["hold_epoch"] and len(acked["tickets"]) == 1
    assert ca.evaluate(store)["verdict"] == BUSY
    tickets[0].release()


# ------------------------------------------- bounded wait / no signalling
def test_wait_times_out_to_defer_without_signalling(store, monkeypatch):
    killed = []
    real_kill = os.kill

    def spy(pid, sig):
        if sig != 0:
            killed.append((pid, sig))
        return real_kill(pid, 0)

    monkeypatch.setattr(os, "kill", spy)
    g = _gate(store, GW)
    t = g.admit("long-turn")
    store.hold("op", [GW], mode="freeze")
    g.publish()
    clock = [0.0]
    rep = ca.wait_for_quiescence(
        store, 5.0, poll=1.0,
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        monotonic=lambda: clock[0],
    )
    assert rep["verdict"] == DEFER and rep["last"] == BUSY
    assert killed == []
    assert g.snapshot()["tickets"]  # work untouched
    t.release()


def test_wait_returns_when_work_finishes(store):
    work = Work()
    work.n = 1
    g = _gate(store, GW, work)
    store.hold("op", [GW], mode="freeze")
    g.publish()

    def finish(_):
        work.n = 0
        g.publish()

    assert ca.wait_for_quiescence(store, 10, poll=0.01, sleep=finish)["verdict"] == QUIESCENT


# ---------------------------------------------- absent / failed evidence
def _held_and_acked(store, names=(GW,)):
    store.hold("op", list(names), mode="freeze")
    gates = [_gate(store, n) for n in names]
    for gg in gates:
        gg.publish()
    return gates


def test_missing_consumer_is_unknown(store):
    _held_and_acked(store, (GW,))
    store.hold("op", [GW, SERVE], mode="freeze")
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN
    row = [r for r in rep["consumers"] if r["consumer"] == SERVE][0]
    assert "no acknowledgment" in row["why"]


def test_stale_record_is_unknown(store):
    _held_and_acked(store)
    assert ca.evaluate(store, now=time.time() + 60, stale_after=20)["verdict"] == UNKNOWN


def test_dead_pid_is_unknown(store):
    _held_and_acked(store)
    assert ca.evaluate(store, pid_alive=lambda pid: False)["verdict"] == UNKNOWN


def test_foreign_host_is_unknown(store):
    _held_and_acked(store)
    assert ca.evaluate(store, host="some-other-box")["verdict"] == UNKNOWN


def test_unacknowledged_epoch_is_unknown(store):
    (g,) = _held_and_acked(store)
    store.hold("op", [GW], mode="freeze")  # re-epoch
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_failing_work_source_is_unknown_not_zero(store):
    def boom():
        raise RuntimeError("registry gone")

    store.hold("op", [GW], mode="freeze")
    AdmissionGate(store, GW, active_work=boom).publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN and "registry gone" in rep["consumers"][0]["why"]


def test_unregistered_work_source_is_unknown(store):
    store.hold("op", [GW], mode="freeze")
    AdmissionGate(store, GW).publish()
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_corrupt_consumer_record_is_unknown(store):
    _held_and_acked(store)
    store.consumer_path(GW).write_text("{not json")
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_unexpected_live_consumer_is_unknown(store):
    _gate(store, GW2).publish()  # live, but the operator forgot it
    _held_and_acked(store, (GW,))
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN
    assert any(r["consumer"] == GW2 for r in rep["consumers"])


def test_corrupt_hold_fails_closed_everywhere(store):
    g = _gate(store, GW)
    store.directory.mkdir(parents=True)
    store.hold_path.write_text("garbage")
    with pytest.raises(AdmissionRefused):
        g.admit("x", internal=True)
    assert g.snapshot()["hold_error"]
    assert ca.evaluate(store)["verdict"] == UNKNOWN
    with pytest.raises(HoldUnreadable):
        store.release("op")  # cannot open what it cannot read
    store.hold("op", [GW], mode="freeze")  # operator re-holds: still closed
    with pytest.raises(AdmissionRefused):
        g.admit("x")


# ------------------------------------------ persistence across restarts
def test_hold_persists_across_consumer_restart_with_no_gap(store):
    store.hold("op", [GW], mode="freeze")
    old = _gate(store, GW)
    old.publish()
    new = _gate(store, GW)  # restarted process, never published yet
    with pytest.raises(AdmissionRefused):
        new.admit("first-message-after-boot")
    # Until the new instance acknowledges, the verdict tracks the new pid.
    new.publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == QUIESCENT
    assert rep["consumers"][0]["instance"] == new.instance


def test_hold_has_no_ttl_and_only_owner_releases(store):
    store.hold("op", [GW], mode="freeze", now=0.0)  # created at the epoch
    g = _gate(store, GW)
    with pytest.raises(AdmissionRefused):
        g.admit("x")
    with pytest.raises(HoldConflict):
        store.release("someone-else")
    with pytest.raises(HoldConflict):
        store.hold("someone-else", [GW])
    store.release("op")
    g.admit("x").release()
    with pytest.raises(HoldConflict):
        store.release("op")


# ------------------------------------------------------------ probes
def test_probe_is_non_mutating_and_checks_freshness(store):
    g = _gate(store, SERVE)
    g.set_serving(lambda: {"ok": True})
    g.publish()
    before = {p: p.read_bytes() for p in store.directory.rglob("*") if p.is_file()}
    ok = ca.probe(store, SERVE, url="http://x/api/health", http_get=lambda u, t: (200, b"{}"))
    assert ok["ok"] is True
    assert ca.probe(store, SERVE, url="http://x", http_get=lambda u, t: (503, b""))["ok"] is False

    def refused(u, t):
        raise ConnectionRefusedError("down")

    assert ca.probe(store, SERVE, url="http://x", http_get=refused)["ok"] is False
    assert ca.probe(store, SERVE, now=time.time() + 120)["ok"] is False
    assert ca.probe(store, "gateway:nope")["ok"] is False
    after = {p: p.read_bytes() for p in store.directory.rglob("*") if p.is_file()}
    assert before == after


def test_probe_reports_consumer_not_serving(store):
    g = _gate(store, GW)
    g.set_serving(lambda: {"ok": False})
    g.publish()
    assert ca.probe(store, GW)["ok"] is False


# ---------------------------------------------------------- pin contract
class _Run:
    def __init__(self, rcs):
        self.rcs = rcs

    def __call__(self, argv, **kw):
        class R:
            returncode = self.rcs.get(argv[3], 0)
        return R()


GREEN = [{"name": "tests", "status": "completed", "conclusion": "success"},
         {"name": "lint", "status": "completed", "conclusion": "skipped"}]
SHA = "a" * 40


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, None])
def test_pin_rejects_non_40_hex(sha):
    assert ca.check_pin(sha, repo=".", remote_ref="origin/main", check_runs=lambda s: GREEN)["ok"] is False


def test_pin_green_on_main_passes():
    res = ca.check_pin(SHA, repo=".", remote_ref="origin/main", check_runs=lambda s: GREEN, run=_Run({}))
    assert res == {"ok": True, "sha": SHA, "problems": []}


@pytest.mark.parametrize("rcs,runs,needle", [
    ({"merge-base": 1}, GREEN, "not on"),
    ({"cat-file": 1}, GREEN, "not present"),
    ({}, [], "no CI"),
    ({}, [{"name": "t", "status": "completed", "conclusion": "failure"}], "non-green"),
    ({}, [{"name": "t", "status": "in_progress", "conclusion": None}], "non-green"),
    ({}, [{"name": "t", "status": "completed", "conclusion": "skipped"}], "no successful"),
])
def test_pin_rejections(rcs, runs, needle):
    res = ca.check_pin(SHA, repo=".", remote_ref="origin/main", check_runs=lambda s: runs, run=_Run(rcs))
    assert res["ok"] is False and any(needle in p for p in res["problems"])


def test_pin_provenance_failure_is_not_green():
    def boom(sha):
        raise RuntimeError("gh down")

    assert ca.check_pin(SHA, repo=".", remote_ref="origin/main", check_runs=boom, run=_Run({}))["ok"] is False


# ------------------------------------------------------------- CLI/config
def test_cli_exit_codes(store, capsys):
    d = ["--dir", str(store.directory)]
    assert ca.main(d + ["status"]) == 2
    assert ca.main(d + ["hold", "--owner", "op", "--expect", GW, "--mode", "freeze"]) == 0
    assert ca.main(d + ["status"]) == 4  # nobody acked
    _gate(store, GW).publish()
    assert ca.main(d + ["status"]) == 0
    assert ca.main(d + ["wait", "--timeout", "0"]) == 0
    assert ca.main(d + ["probe", GW]) == 0
    assert ca.main(d + ["release", "--owner", "op"]) == 0
    capsys.readouterr()


def test_gate_from_settings(tmp_path):
    assert ca.gate_from_settings("gateway", {}) is None
    assert ca.gate_from_settings("gateway", {"enabled": False, "consumers": {"gateway": GW}}) is None
    assert ca.gate_from_settings("gateway", {"enabled": True, "dir": str(tmp_path)}) is None
    g = ca.gate_from_settings("serve", {"enabled": True, "dir": str(tmp_path), "consumers": {"serve": SERVE}})
    assert g is not None and g.consumer == SERVE and g.store.directory == tmp_path


def test_process_gate_reads_config_yaml_through_production_loader(tmp_path):
    from hermes_constants import get_hermes_home

    cfg = get_hermes_home() / "config.yaml"
    cfg.write_text(
        "checkout_admission:\n  enabled: true\n"
        f"  dir: {tmp_path / 'adm'}\n"
        "  consumers:\n    gateway: gateway:default\n"
    )
    ca._reset_process_gates_for_tests()
    g = ca.process_gate("gateway")
    assert g is not None and g.consumer == "gateway:default"
    assert g.store.directory == tmp_path / "adm"
    assert ca.process_gate("serve") is None  # no consumer named for serve


def test_default_directory_is_shared_git_common_dir():
    d = ca.default_directory()
    assert d is not None and d.name == "checkout-admission"
    assert d.parent.name == ".git" or (d.parent / "HEAD").exists()


# ======================================================= ingress modalities
def _install(kind, gate):
    ca._process_gates[kind] = gate


# ---- gateway messaging (real _handle_message) ----------------------------
def _msg_runner(gate):
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _ = make_restart_runner()
    runner._external_drain_active = False
    runner._checkout_gate_cache = gate
    return runner


def _event(text="hello"):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import make_restart_source

    return MessageEvent(text=text, message_type=MessageType.TEXT,
                        source=make_restart_source(), message_id="m1")


def test_gateway_message_refused_under_hold(store):
    g = _gate(store, GW)
    store.hold("op", [GW])
    runner = _msg_runner(g)
    called = []

    async def agent(*a, **k):
        called.append(1)
        return "ran"

    runner._handle_message_with_agent = agent
    result = asyncio.run(runner._handle_message(_event()))
    assert "maintenance" in (result or "").lower()
    assert called == [] and g.snapshot()["tickets"] == []


def test_gateway_message_control_admitted_and_counted_for_whole_turn(store):
    g = _gate(store, GW)
    runner = _msg_runner(g)
    seen = []

    async def agent(*a, **k):
        seen.append(len(g.snapshot()["tickets"]))
        return "ran"

    runner._handle_message_with_agent = agent
    result = asyncio.run(runner._handle_message(_event()))
    assert result == "ran" and seen == [1]
    assert g.snapshot()["tickets"] == []  # released at turn end


# ---- gateway API server (real decorator) --------------------------------
def test_api_request_refused_under_hold_and_counted_otherwise(store):
    from gateway.platforms import api_server as api

    g = _gate(store, GW)
    _install("gateway", g)

    class Stub:
        _pending_agent_requests = 0
        _check_auth = staticmethod(lambda request: None)
        _gateway_is_draining = staticmethod(lambda: False)
        _checkout_hold_refusal = staticmethod(api.APIServerAdapter._checkout_hold_refusal)
        _draining_response = api.APIServerAdapter._draining_response

    seen = []

    @api._admit_api_agent_request
    async def handler(self, request):
        seen.append(self._pending_agent_requests)
        return "ok"

    stub = Stub()
    assert asyncio.run(handler(stub, None)) == "ok" and seen == [1]
    store.hold("op", [GW])
    resp = asyncio.run(handler(stub, None))
    assert resp.status == 503
    assert json.loads(resp.body)["error"]["code"] == "checkout_held"
    assert seen == [1] and stub._pending_agent_requests == 0


# ---- cron ticker (real tick) --------------------------------------------
def test_cron_tick_refused_under_hold_and_ticket_spans_tick(store, monkeypatch, tmp_path):
    import cron.scheduler as sched

    g = _gate(store, GW)
    released = []

    class Gate:
        def __call__(self):
            return True

        def admit(self):
            try:
                t = g.admit("cron:tick")
            except AdmissionRefused:
                return None
            return lambda: (released.append(len(g.snapshot()["tickets"])), t.release())

    monkeypatch.setattr(sched, "_get_lock_paths", lambda: (tmp_path, tmp_path / "tick.lock"))
    due_calls = []
    monkeypatch.setattr(sched, "get_due_jobs", lambda *a, **k: due_calls.append(1) or [], raising=False)
    store.hold("op", [GW])
    assert sched.tick(verbose=False, can_dispatch=Gate()) == 0
    assert due_calls == [] and released == []
    store.release("op")
    sched.tick(verbose=False, can_dispatch=Gate())
    assert released == [1]  # ticket held through the dispatch window
    assert g.snapshot()["tickets"] == []


# ---- serve RPC + a real long-lived WebSocket ----------------------------
@pytest.fixture
def serve_gate(store, monkeypatch):
    import tui_gateway.server as server

    g = _gate(store, SERVE)
    g.set_active_work(server._serve_active_work)
    monkeypatch.setattr(server, "_checkout_gate_ref", g)
    return g


def test_serve_prompt_rpcs_refused_under_hold(store, serve_gate):
    import tui_gateway.server as server

    for m in sorted(server._CHECKOUT_GATED_METHODS):
        resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": m, "params": {"session_id": "nope"}})
        assert resp["error"]["code"] != 5075, m  # control: reached the handler
    store.hold("op", [SERVE])
    for m in sorted(server._CHECKOUT_GATED_METHODS):
        resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": m, "params": {"session_id": "nope"}})
        assert resp["error"]["code"] == 5075, m
    # Non-turn RPCs keep working under the hold (the serving probe path).
    assert "error" not in (server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "commands.catalog", "params": {}}) or {})


def test_serve_background_ticket_spans_worker_thread(store, serve_gate, monkeypatch):
    import tui_gateway.server as server

    gate_open = threading.Event()
    counts = []

    def fake(rid, params):
        def work():
            counts.append(len(serve_gate.snapshot()["tickets"]))
            gate_open.wait(5)

        server._start_counted_thread(work, name="bg-test")
        return server._ok(rid, {"task_id": "bg"})

    monkeypatch.setitem(server._methods, "prompt.background", fake)
    server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "prompt.background", "params": {}})
    deadline = time.time() + 5
    while not counts and time.time() < deadline:
        time.sleep(0.01)
    assert counts == [1]
    assert len(serve_gate.snapshot()["tickets"]) == 1  # RPC returned, work still counted
    gate_open.set()
    deadline = time.time() + 5
    while serve_gate.snapshot()["tickets"] and time.time() < deadline:
        time.sleep(0.01)
    assert serve_gate.snapshot()["tickets"] == []


def test_serve_running_session_counted_and_freeze_defers_continuation(store, serve_gate, monkeypatch):
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock(), "running": True}
    monkeypatch.setitem(server._sessions, "s-test", session)
    assert serve_gate.snapshot()["active_work"] >= 1
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(a))
    store.hold("op", [SERVE], mode="drain")
    # drain: continuation passes the gate (it proceeds into the real body; we
    # only assert it was NOT refused by the checkout gate)
    assert serve_gate.check(internal=True) is None
    store.hold("op", [SERVE], mode="freeze")
    assert server._run_prompt_submit("r", "s-test", session, "continue") is False
    assert session["running"] is False
    assert emitted and "maintenance" in emitted[-1][2]["message"].lower()


def test_real_websocket_existing_connection_is_gated(store, serve_gate):
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from tui_gateway.ws import handle_ws

    async def endpoint(ws):
        await handle_ws(ws)

    app = Starlette(routes=[WebSocketRoute("/ws", endpoint)])

    def call(conn, i):
        conn.send_text(json.dumps({"jsonrpc": "2.0", "id": i, "method": "prompt.btw",
                                   "params": {"session_id": "nope", "text": "hi"}}))
        while True:
            msg = json.loads(conn.receive_text())
            if msg.get("id") == i:
                return msg

    with TestClient(app).websocket_connect("/ws") as conn:
        first = json.loads(conn.receive_text())
        assert first["params"]["type"] == "gateway.ready"
        assert call(conn, 1)["error"]["code"] != 5075  # control: open
        store.hold("op", [SERVE])  # engaged while this connection is live
        assert call(conn, 2)["error"]["code"] == 5075
        store.release("op")
        assert call(conn, 3)["error"]["code"] != 5075
