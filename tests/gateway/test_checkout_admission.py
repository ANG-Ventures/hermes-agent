"""Shared-checkout admission hold (t_e8017c37).

Core contract tests drive the real HoldStore/AdmissionGate against a temp
directory; modality tests drive the real ingress seams (gateway messaging
``_handle_message``, API ``_admit_api_agent_request``, cron ``tick``, serve
``handle_request`` incl. a real long-lived WebSocket through
``tui_gateway.ws.handle_ws``).
"""
from __future__ import annotations
from pathlib import Path

import asyncio
import json
import os
import socket
import threading
from types import SimpleNamespace
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
        if sig == 0:  # liveness probe (psutil.pid_exists on POSIX), not a signal
            return real_kill(pid, 0)
        killed.append((pid, sig))

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
    """Legacy (pre-lease) record: liveness is still the pid."""
    _held_and_acked(store)
    rec = store.read_consumer(GW)
    rec.pop("token")
    store.write_consumer(GW, rec)
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
    store.consumer_path(GW).write_text("{not json", encoding="utf-8")
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
    store.hold_path.write_text("garbage", encoding="utf-8")
    with pytest.raises(AdmissionRefused):
        g.admit("x", internal=True)
    assert g.snapshot()["hold_error"]
    assert ca.evaluate(store)["verdict"] == UNKNOWN
    with pytest.raises(HoldUnreadable):
        store.release("op")  # cannot open what it cannot read
    store.hold("op", [GW], mode="freeze", recover_unreadable=True)  # operator re-holds: still closed
    with pytest.raises(AdmissionRefused):
        g.admit("x")


# ------------------------------------------ persistence across restarts
def test_hold_persists_across_consumer_restart_with_no_gap(store):
    store.hold("op", [GW], mode="freeze")
    old = _gate(store, GW)
    old.publish()
    old.stop_lease()  # the old process exits cleanly (publish_forever's finally)
    new = _gate(store, GW)  # restarted process, never published yet
    with pytest.raises(AdmissionRefused):
        new.admit("first-message-after-boot")
    # Until the new instance acknowledges, the verdict tracks the new pid.
    new.publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == QUIESCENT
    assert rep["consumers"][0]["instance"] == new.instance


def test_unclean_restart_defers_for_at_most_three_leases(store):
    """A predecessor that died without releasing (SIGKILL) is indistinguishable
    from a live peer until its lease expires: UNKNOWN for 3*lease_s, then clear."""
    store.hold("op", [GW], mode="freeze")
    _gate(store, GW).publish()  # killed: no stop_lease
    mono = _Mono()
    new = AdmissionGate(store, GW, active_work=Work(), monotonic=mono)
    assert new.publish().get("shared_with")
    mono.t += ca.LEASE_EXPIRY_FACTOR * ca.DEFAULT_LEASE_S
    assert "shared_with" not in new.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


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
        "  consumers:\n    gateway: gateway:default\n",
        encoding="utf-8",
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
        # Upstream room-grant auth: /v1/runs with a grant token takes _check_run_auth; a plain
        # chat request (no token) keeps the bearer check this test exercises.
        _room_grant_token = staticmethod(lambda request: None)
        _gateway_is_draining = staticmethod(lambda: False)
        _checkout_hold_refusal = staticmethod(api.APIServerAdapter._checkout_hold_refusal)
        _draining_response = api.APIServerAdapter._draining_response

    seen = []

    @api._admit_api_agent_request
    async def handler(self, request):
        seen.append(self._pending_agent_requests)
        return "ok"

    stub = Stub()
    request = SimpleNamespace(path="/v1/chat/completions")
    assert asyncio.run(handler(stub, request)) == "ok" and seen == [1]
    store.hold("op", [GW])
    resp = asyncio.run(handler(stub, request))
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
    # Upstream wire contracts: the dispatcher validates params/results of every registered method
    # (strict under HERMES_TEST_ISOLATION), so the double is driven with contract-valid params.
    server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "prompt.background",
                           "params": {"session_id": "s-bg", "text": "work"}})
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


# ------------------------------------ FleetReview #1035 (C4 backfill 2026-09-27)
def test_unreadable_hold_cannot_be_taken_over_without_explicit_recovery(store):
    """#1035 checkout_admission.py:229: an unreadable hold lost its owner; a
    plain re-hold by anyone (who could then release it) must be refused."""
    store.hold("owner-a", [GW])
    store.hold_path.write_text("garbage", encoding="utf-8")
    with pytest.raises(HoldConflict):
        store.hold("intruder", [GW])
    with pytest.raises(HoldUnreadable):
        store.release("intruder")
    state = store.hold("op", [GW], recover_unreadable=True)  # explicit override
    assert store.read_hold().owner == "op" and state.owner == "op"
    assert list(store.directory.glob("hold.unreadable.*.json"))  # kept for audit


def test_recover_unreadable_keeps_hold_closed_if_replacement_write_fails(store, monkeypatch):
    """#1354 checkout_admission.py:242: --recover-unreadable must not move the
    unreadable hold away before its replacement is installed; a failed write
    must leave hold.json in place (admission still refused), not absent."""
    store.hold("owner-a", [GW])
    store.hold_path.write_text("garbage", encoding="utf-8")

    def boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(ca, "_atomic_write_json", boom)
    with pytest.raises(OSError):
        store.hold("op", [GW], recover_unreadable=True)
    assert store.hold_path.read_text(encoding="utf-8") == "garbage"
    with pytest.raises(HoldUnreadable):
        store.read_hold()  # still fail-closed, never None (open)
    assert list(store.directory.glob("hold.unreadable.*.json"))  # audit copy


def test_repeated_unreadable_recoveries_never_overwrite_an_audit_copy(store, monkeypatch):
    """#1364 checkout_admission.py:247: two recoveries in the same second must
    each keep their own audit snapshot; a copy that dies mid-write must not
    truncate or clobber an earlier one."""
    monkeypatch.setattr(ca.time, "time", lambda: 1_000_000.0)  # same second
    store.hold("owner-a", [GW])
    store.hold_path.write_text("garbage-1", encoding="utf-8")
    store.hold("op", [GW], recover_unreadable=True)
    store.hold_path.write_text("garbage-2", encoding="utf-8")
    store.hold("op", [GW], recover_unreadable=True)
    audits = sorted(store.directory.glob("hold.unreadable.*.json"))
    assert sorted(p.read_text(encoding="utf-8") for p in audits) == ["garbage-1", "garbage-2"]

    # Crash during the third copy: earlier snapshots intact, no stray temp.
    store.hold_path.write_text("garbage-3", encoding="utf-8")

    def boom(inp, out, *a, **k):
        out.write(b"partial")
        raise OSError("crash mid-copy")

    monkeypatch.setattr(ca.shutil, "copyfileobj", boom)
    store.hold("op", [GW], recover_unreadable=True)  # audit is best-effort
    after = sorted(store.directory.glob("hold.unreadable.*.json"))
    assert after == audits
    assert sorted(p.read_text(encoding="utf-8") for p in after) == ["garbage-1", "garbage-2"]
    assert not list(store.directory.glob(".hold.json.audit.*"))


def test_unreadable_audit_copy_survives_filesystem_without_hard_links(store, monkeypatch):
    """#1418 checkout_admission.py:142: on a mount where os.link always fails
    (SMB/FUSE), recovery must still keep the unreadable bytes for audit and
    must not overwrite an earlier snapshot."""
    import errno

    def no_link(src, dst, *a, **k):
        raise OSError(errno.ENOTSUP, "hard links not supported")

    monkeypatch.setattr(ca.os, "link", no_link)
    monkeypatch.setattr(ca.time, "time", lambda: 1_000_000.0)
    store.hold("owner-a", [GW])
    store.hold_path.write_text("garbage-1", encoding="utf-8")
    store.hold("op", [GW], recover_unreadable=True)
    store.hold_path.write_text("garbage-2", encoding="utf-8")
    store.hold("op", [GW], recover_unreadable=True)
    audits = sorted(store.directory.glob("hold.unreadable.*.json"))
    assert sorted(p.read_text(encoding="utf-8") for p in audits) == ["garbage-1", "garbage-2"]
    assert not list(store.directory.glob(".hold.json.audit.*"))


def test_consumer_name_shared_by_two_live_processes_is_unknown(store):
    """#1035 checkout_admission.py:419: a busy and an idle process publishing
    under ONE consumer name must not let the idle snapshot read QUIESCENT."""
    store.hold("op", [GW], mode="freeze")
    busy = _gate(store, GW)
    busy.pid = os.getppid()  # a second live process on this host
    busy.instance = "other-process"
    busy._tickets["t"] = ca.Ticket("t", "turn", False, time.time(), busy)
    busy.publish()
    idle = _gate(store, GW)
    idle.publish()
    assert ca.evaluate(store)["verdict"] == UNKNOWN
    idle.publish()  # our own back-to-back write must not clear the flag
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN
    assert "shared" in rep["consumers"][0]["why"]


def _foreign_record(store, name, **fields):
    """Publish under ``name`` as another instance whose pid (our parent) is
    alive on this host; ``fields`` override the start-identity fields. A
    field set to ``...`` is dropped (legacy record shape). The record is
    written in the LEGACY (pre-lease, no ``token``) shape: these tests cover
    the pid-identity fallback kept for one release."""
    other = _gate(store, name)
    other.pid = os.getppid()
    other.instance = "other-instance"
    other.start_offset = ca._start_offset(other.pid)  # genuine by default
    other.time_ns = ca._time_ns_id() if other.start_offset is not None else None
    other.create_time = ca._create_time(other.pid)
    rec = other.snapshot()
    rec.update({"token": ..., "lease_s": ...})
    rec.update(fields)
    rec = {k: v for k, v in rec.items() if v is not ...}
    store.write_consumer(name, rec)


@pytest.mark.parametrize("fields", [
    pytest.param({}, id="genuine"),
    pytest.param({"start_offset": float("nan"), "boot_ts": float("nan")}, id="nan-fails-closed"),
    pytest.param({"boot_id": ..., "start_offset": ...}, id="legacy-record-stays-live"),
])
def test_genuine_or_inconclusive_foreign_record_is_shared(store, fields):
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE, **fields)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_reused_pid_started_after_record_is_not_shared(store):
    """t_2882f0c1: a live pid that is a different process than the record's
    publisher (wireplumber reusing serve-clanker's pid) is not shared."""
    kw = {"start_offset": ca._start_offset(os.getppid()) - 60}
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE, **kw)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()
    assert "shared_with" not in me.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


def test_record_from_before_host_boot_is_not_shared(store, monkeypatch):
    """t_2882f0c1: a record written in a previous host boot is never a live
    foreign publisher, whatever now holds its pid."""
    monkeypatch.setattr(ca, "_host_boot_id", lambda: "current-boot")
    kw = {"boot_id": "previous-boot"}
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE, **kw)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


def test_wall_clock_step_keeps_genuine_foreign_publisher_shared(store, monkeypatch):
    """A forward clock step moves psutil's boot_time and create_time together
    (Linux derives both from btime); the boot-relative start offset must
    still identify the genuine foreign publisher."""
    import psutil

    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    real_boot, real_ct = psutil.boot_time, psutil.Process.create_time
    monkeypatch.setattr(psutil, "boot_time", lambda: real_boot() + 3600)
    monkeypatch.setattr(psutil.Process, "create_time", lambda self: real_ct(self) + 3600)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")


def test_clock_step_between_psutil_terms_keeps_genuine_foreign_publisher_shared(store, monkeypatch):
    """Prism bc03ae51308a: older psutil computed create_time from a CACHED btime
    while boot_time() re-read a fresh one, so after a clock step the two terms
    straddled it and the offset was off by the step. The offset must not depend
    on psutil's wall-clock terms at all: step ONLY boot_time (the cache-miss
    shape) and the genuine publisher must stay flagged."""
    import psutil

    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    real_boot = psutil.boot_time
    monkeypatch.setattr(psutil, "boot_time", lambda: real_boot() + 3600)   # create_time NOT stepped
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")


def test_start_offset_reads_the_kernel_boot_relative_starttime(tmp_path, monkeypatch):
    """On Linux the offset comes straight from /proc/<pid>/stat field 22 (ticks
    since boot), with a comm that contains spaces and parens."""
    if not Path("/proc").exists():
        pytest.skip("no procfs")
    pid = os.getpid()
    off = ca._start_offset(pid)
    assert off > 0
    import psutil
    assert abs(off - (psutil.Process(pid).create_time() - psutil.boot_time())) < 1


def test_no_procfs_fallback_identity_is_clock_step_safe(store, monkeypatch):
    """Prism (round 3 on #2075): without procfs the identity is psutil's stored
    absolute create_time, compared against itself; boot_time() never enters.
    Step boot_time by an hour with procfs hidden: the genuine foreign publisher
    stays flagged, and a reused pid (create_time off by an hour) still clears."""
    import psutil

    monkeypatch.setattr(ca, "_start_offset", lambda pid: None)  # no procfs
    monkeypatch.setattr(ca, "_create_time_is_stable", lambda: True)  # the no-procfs hosts (macOS/Windows) store it
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    real_boot = psutil.boot_time
    monkeypatch.setattr(psutil, "boot_time", lambda: real_boot() + 3600)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")  # genuine, flagged
    # the recorded process was replaced by one born an hour later: not the same
    rec = store.read_consumer(SERVE)
    assert rec.get("start_offset") is None and ca._finite(rec.get("create_time"))
    _foreign_record(store, SERVE, create_time=rec["create_time"] - 3600)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()


def test_fallback_offset_never_mixes_boot_time(monkeypatch):
    """The no-procfs path must not compute create_time - boot_time: with
    boot_time patched to raise, the identity still resolves."""
    import psutil

    monkeypatch.setattr(ca, "_start_offset", lambda pid: None)
    monkeypatch.setattr(psutil, "boot_time", lambda: (_ for _ in ()).throw(AssertionError("boot_time read")))
    assert ca._finite(ca._create_time(os.getpid()))


def test_empty_recorded_boot_id_is_missing_evidence_not_another_boot(store, monkeypatch):
    """Prism f-6a6148c60a0851c2: boot_id "" on a live, busy foreign record must not clear the flag."""
    monkeypatch.setattr(ca, "_host_boot_id", lambda: "current-boot")
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE, boot_id="")
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")


def test_offset_from_another_time_namespace_is_inconclusive(store, monkeypatch):
    """Prism f-4c7533074d58f2a7: a recorded start_offset taken in a different time namespace is not
    comparable; the absolute create_time decides, and a genuine publisher stays flagged even when the
    offsets disagree by an hour."""
    monkeypatch.setattr(ca, "_create_time_is_stable", lambda: True)   # macOS/Windows rule: create_time is kernel-stored
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    rec = store.read_consumer(SERVE)
    if rec.get("start_offset") is None:
        pytest.skip("no procfs offset on this host")
    _foreign_record(store, SERVE, start_offset=rec["start_offset"] + 3600, time_ns="time:[1]")  # other namespace
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")  # genuine: create_time matches
    # and a reused pid in that situation still clears (create_time off by an hour)
    _foreign_record(store, SERVE, start_offset=rec["start_offset"] + 3600, time_ns="time:[1]",
                    create_time=rec["create_time"] - 3600)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()


def test_linux_incomparable_offset_never_clears_on_create_time(store, monkeypatch):
    """Prism f-0d05a4e1ac34ca32 / f-78f5d20661de8262: on Linux create_time is btime + starttime/CLK_TCK
    (clock-step sensitive), so with no comparable offset (other / missing time namespace) a create_time
    mismatch of an hour must NOT read as a reused pid; the live foreign publisher stays flagged."""
    monkeypatch.setattr(ca.sys, "platform", "linux")   # the Linux rule, exercised through the real predicate on every host
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    rec = store.read_consumer(SERVE)
    if rec.get("start_offset") is None:
        pytest.skip("no procfs offset on this host")
    _foreign_record(store, SERVE, start_offset=rec["start_offset"] + 3600, time_ns="time:[1]",
                    create_time=rec["create_time"] - 3600)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")
    # and with NO time namespace on either side (pre-5.6 kernel) the same holds
    _foreign_record(store, SERVE, start_offset=rec["start_offset"] + 3600, time_ns=None,
                    create_time=rec["create_time"] - 3600)
    monkeypatch.setattr(ca, "_time_ns_id", lambda: None)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")


def test_record_from_another_pid_namespace_is_never_cleared(store, monkeypatch):
    """Prism f-0d05a4e1ac34ca32: a record written in another PID namespace names a pid we cannot inspect; a
    local process at that number with a different start offset is unrelated, so the record stays live."""
    if ca._pid_ns_id() is None:
        pytest.skip("no /proc/self/ns/pid on this host")
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    rec = store.read_consumer(SERVE)
    # same pid, same boot, same time namespace, offset off by an hour: in OUR namespace that is a reused pid...
    _foreign_record(store, SERVE, start_offset=(rec.get("start_offset") or 0) + 3600,
                    create_time=rec["create_time"] - 3600)
    assert "shared_with" not in _gate(store, SERVE).publish()
    # ...but recorded in ANOTHER pid namespace it is inconclusive and the busy publisher stays flagged
    _foreign_record(store, SERVE, start_offset=(rec.get("start_offset") or 0) + 3600,
                    create_time=rec["create_time"] - 3600, pid_ns="pid:[1]")
    assert _gate(store, SERVE).publish().get("shared_with")
    # a legacy record with no pid_ns at all, on a kernel that exposes one, is inconclusive too
    _foreign_record(store, SERVE, start_offset=(rec.get("start_offset") or 0) + 3600,
                    create_time=rec["create_time"] - 3600, pid_ns=...)
    assert _gate(store, SERVE).publish().get("shared_with")


def test_dead_pre_pid_ns_record_clears_on_a_pid_ns_kernel(store, monkeypatch):
    """Prism ff3c2cf86472: the first restart onto pid_ns-aware code reads its dead predecessor's record, which
    has no pid_ns. A dead pid clears it (the pre-#2075 behaviour); the sticky flag must not pin it forever.
    A recorded pid_ns that differs from ours stays inconclusive even with the local pid dead."""
    import subprocess
    import sys as _sys

    monkeypatch.setattr(ca, "_pid_ns_id", lambda: "pid:[4026531836]")  # a kernel that exposes one
    child = subprocess.Popen([_sys.executable, "-c", "pass"])
    child.wait()
    dead = child.pid
    assert not ca._pid_alive(dead)
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE, pid=dead, pid_ns=...)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()
    assert me._foreign is None
    _foreign_record(store, SERVE, pid=dead, pid_ns="pid:[1]")
    assert _gate(store, SERVE).publish().get("shared_with")


def test_android_create_time_is_not_stable(monkeypatch):
    """Prism 1865f7896adc: Android runs the Linux kernel; its create_time is btime-derived, not kernel-stored."""
    monkeypatch.setattr(ca.sys, "platform", "android")
    assert ca._create_time_is_stable() is False
    monkeypatch.setattr(ca.sys, "platform", "darwin")
    assert ca._create_time_is_stable() is True


def test_sticky_shared_flag_clears_when_foreign_pid_is_reused(store):
    """t_2882f0c1: the sticky flag is re-checked every publish, so once the
    recorded process is gone and its pid reused the flag drops."""
    store.hold("op", [SERVE], mode="freeze")
    _foreign_record(store, SERVE)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")  # genuine: flagged
    pid, inst, evidence = me._foreign
    me._foreign = (pid, inst, dict(evidence, start_offset=evidence["start_offset"] - 60))
    assert "shared_with" not in me.publish()


# ------------------------------------------------- publisher lease (t_eceadca2)
class _Mono:
    """Injected monotonic clock for a gate."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _age_record(store, name, seconds):
    """Move the record's mtime (the store's lease clock) ``seconds`` into the past."""
    p = store.consumer_path(name)
    st = os.stat(p)
    os.utime(p, (st.st_atime - seconds, st.st_mtime - seconds))


def _leased_leftover(store, name, *, age):
    """A leased record whose pid is ALIVE and carries that process's genuine
    pid identity (boot id, namespaces, start offset): everything a /proc
    comparison could see says "same process". Only its lease is old."""
    other = _gate(store, name)
    other.pid = os.getppid()
    other.instance = "previous-boot-instance"
    other.start_offset = ca._start_offset(other.pid)
    other.time_ns = ca._time_ns_id() if other.start_offset is not None else None
    other.create_time = ca._create_time(other.pid)
    store.write_consumer(name, other.snapshot())
    _age_record(store, name, age)
    return other


def test_lease_reused_pid_leftover_is_not_a_second_publisher(store):
    """The wireplumber case: serve:clanker's record from before the reboot
    names a pid that a different, live process now holds. Its lease is long
    expired on the store's clock, so it is neither a sharing peer nor an
    unexpected live consumer -- whatever /proc says about that pid."""
    store.hold("op", [SERVE], mode="freeze")
    _leased_leftover(store, SERVE, age=3600)
    me = _gate(store, SERVE)
    assert "shared_with" not in me.publish()
    assert "shared_with" not in me.publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == QUIESCENT, rep


def test_lease_unlisted_expired_record_on_reused_pid_fails_safe(store):
    """The one remaining pid-trust edge: an UNLISTED consumer's expired lease
    falls back to the pid probe (an expired lease alone does not prove a
    stalled/SIGSTOPped publisher gone), so a reused live pid reads UNKNOWN --
    an extra DEFER, never a mutation under a live consumer."""
    store.hold("op", [SERVE], mode="freeze")
    _leased_leftover(store, GW2, age=3600)
    _gate(store, SERVE).publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN, rep
    assert any(r["consumer"] == GW2 for r in rep["consumers"]), rep


def test_lease_unlisted_expired_record_of_dead_pid_is_ignored(store):
    """Same record whose pid is dead: the fallback ignores it."""
    store.hold("op", [SERVE], mode="freeze")
    left = _leased_leftover(store, GW2, age=3600)
    _gate(store, SERVE).publish()
    rep = ca.evaluate(store, pid_alive=lambda pid: pid != left.pid)
    assert rep["verdict"] == QUIESCENT, rep


def test_lease_unlisted_released_record_of_live_pid_is_ignored(store):
    """A clean stop (released lease) clears an unlisted consumer at once,
    even though its pid (this test process) is still alive."""
    store.hold("op", [SERVE], mode="freeze")
    gone = _gate(store, GW2)
    gone.publish()
    gone.stop_lease()
    _gate(store, SERVE).publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == QUIESCENT, rep


def test_lease_unverifiable_age_is_not_liveness_for_listed(store, monkeypatch):
    """Prism 33713d41b9c3: an unreadable store clock leaves a listed
    consumer's lease unverified -> UNKNOWN, never QUIESCENT."""
    store.hold("op", [SERVE], mode="freeze")
    _gate(store, SERVE).publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT
    monkeypatch.setattr(type(store), "record_age", lambda self, name: None)
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_lease_fresh_foreign_publisher_is_shared(store):
    """Control arm for the reuse test: the same record with a FRESH lease is
    a live second publisher."""
    store.hold("op", [SERVE], mode="freeze")
    _leased_leftover(store, SERVE, age=0)
    me = _gate(store, SERVE)
    assert me.publish().get("shared_with")
    assert ca.evaluate(store)["verdict"] == UNKNOWN


def test_lease_killed_publisher_expires_after_three_leases(store):
    """A peer that stops renewing (kill -9: no release) keeps the flag for
    3*lease_s on the gate's monotonic clock, then the record goes QUIESCENT."""
    store.hold("op", [SERVE], mode="freeze")
    mono = _Mono()
    peer = AdmissionGate(store, SERVE, active_work=Work(), lease_s=30)
    peer.pid = os.getppid()
    me = AdmissionGate(store, SERVE, active_work=Work(), lease_s=30, monotonic=mono)
    peer.publish()
    assert me.publish().get("shared_with")
    peer.publish()  # still alive: renews
    mono.t += 60
    assert me.publish().get("shared_with")
    # peer is killed -9 here: it never writes again
    mono.t += 89
    assert me.publish().get("shared_with")
    mono.t += 2
    assert "shared_with" not in me.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


def test_lease_released_publisher_clears_at_once(store):
    """A clean stop writes a released record: no wait for expiry."""
    store.hold("op", [SERVE], mode="freeze")
    peer = _gate(store, SERVE)
    peer.pid = os.getppid()
    me = _gate(store, SERVE)
    peer.publish()
    assert me.publish().get("shared_with")
    peer.publish()
    peer.stop_lease()
    assert store.read_consumer(SERVE)["lease_released"] is True
    assert "shared_with" not in me.publish()
    assert ca.evaluate(store)["verdict"] == QUIESCENT


def test_lease_release_never_overwrites_a_successor(store):
    old = _gate(store, SERVE)
    old.publish()
    new = _gate(store, SERVE)
    new.publish()
    old.stop_lease()
    rec = store.read_consumer(SERVE)
    assert rec["token"] == new.token and not rec.get("lease_released")


def test_lease_live_publisher_under_clock_step_stays_live(store):
    """Neither a wall-clock nor a store-clock step can expire a live peer:
    a known peer is aged on the gate's monotonic clock, and a reader's mtime
    age of a record written after a backward step is negative (fresh)."""
    store.hold("op", [SERVE], mode="freeze")
    mono = _Mono()
    peer = _gate(store, SERVE)
    peer.pid = os.getppid()
    me = AdmissionGate(store, SERVE, active_work=Work(), monotonic=mono)
    peer.publish()
    assert me.publish().get("shared_with")
    peer.publish()
    _age_record(store, SERVE, 3600)  # store clock stepped forward an hour since the write
    assert me.publish().get("shared_with")  # known peer: monotonic, not mtime
    # reader: a live publisher that wrote while the clock ran an hour ahead
    solo = HoldStore(store.directory.parent / "solo")
    solo.hold("op", [SERVE], mode="freeze")
    g = _gate(solo, SERVE)
    g.publish()
    _age_record(solo, SERVE, -3600)
    assert ca.evaluate(solo)["verdict"] == QUIESCENT  # judged by its lease, it is live
    assert ca.probe(solo, SERVE)["ok"] is True


def test_lease_thread_renews_while_serving_loop_is_stalled(store):
    """The lease is renewed off the serving loop: published_at stays put (a
    stalled loop still reads stale) while the record keeps being rewritten."""
    g = AdmissionGate(store, SERVE, active_work=Work(), lease_s=0.05)
    first = g.publish()
    _age_record(store, SERVE, 3600)
    g.start_lease()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (store.record_age(SERVE) or 0) > 60:
            time.sleep(0.02)
        assert store.record_age(SERVE) < 60
        rec = store.read_consumer(SERVE)
        assert rec["token"] == g.token and rec["published_at"] == first["published_at"]
    finally:
        g.stop_lease()


def test_lease_real_publisher_killed_9_goes_quiescent(store, tmp_path):
    """Real process: a second publisher under our name, then SIGKILL. Its
    lease expires after 3*lease_s and the shared flag clears."""
    import signal
    import subprocess
    import sys

    lease_s = 0.3
    store.hold("op", [SERVE], mode="freeze")
    ready = tmp_path / "ready"
    code = (
        "import sys, time, threading\n"
        "from pathlib import Path\n"
        "import gateway.checkout_admission as ca\n"
        f"g = ca.AdmissionGate(ca.HoldStore({str(store.directory)!r}), {SERVE!r}, "
        f"active_work=lambda: 0, lease_s={lease_s})\n"
        "g.start_publisher_thread(interval=0.05)\n"
        "time.sleep(0.3)\n"
        f"Path({str(ready)!r}).write_text('1')\n"
        "time.sleep(600)\n"
    )
    repo = Path(ca.__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(repo),
                            env=dict(os.environ, PYTHONPATH=str(repo)))
    try:
        deadline = time.monotonic() + 120
        while not ready.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "publisher did not start"
            time.sleep(0.05)
        me = AdmissionGate(store, SERVE, active_work=Work(), lease_s=lease_s)
        assert me.publish().get("shared_with"), "live second publisher not flagged"
        assert ca.evaluate(store)["verdict"] == UNKNOWN
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
        killed_at = time.monotonic()
        cleared_at = None
        while time.monotonic() - killed_at < 3 * lease_s * 10:
            if "shared_with" not in me.publish():
                cleared_at = time.monotonic()
                break
            time.sleep(0.05)
        assert cleared_at is not None, "flag never cleared after SIGKILL"
        assert cleared_at - killed_at >= 3 * lease_s - 0.2  # not before the lease could expire
        assert ca.evaluate(store)["verdict"] == QUIESCENT
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def test_unreadable_unexpected_consumer_record_is_unknown(store):
    """#1035 checkout_admission.py:601: a corrupt record for a consumer the
    operator did not list may belong to a live consumer -> UNKNOWN, not skipped."""
    _held_and_acked(store, (GW,))
    assert ca.evaluate(store)["verdict"] == QUIESCENT  # control
    store.consumer_path(GW2).parent.mkdir(parents=True, exist_ok=True)
    store.consumer_path(GW2).write_text("{not json", encoding="utf-8")
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN
    assert any(r["consumer"] == GW2 for r in rep["consumers"])


def test_stdio_server_installs_the_admission_gate(store, monkeypatch):
    """#1035 tui_gateway/server.py:3156: the stdio entrypoint never runs the
    serve lifespan; it must still refuse prompt RPCs under a hold."""
    import io
    import sys

    import tui_gateway.entry as entry
    import tui_gateway.server as server

    gate = _gate(store, SERVE)
    monkeypatch.setattr(ca, "process_gate", lambda kind: gate if kind == "serve" else None)
    monkeypatch.setattr(server, "_checkout_gate_ref", None)
    monkeypatch.setattr(entry, "_install_sidecar_publisher", lambda: None)
    monkeypatch.setattr(server, "_start_backend_heartbeat_refresher", lambda: None)
    monkeypatch.setattr(server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: None)
    monkeypatch.setattr(entry, "resolve_skin", lambda: None)
    monkeypatch.setattr(entry, "handle_spurious_eof", lambda *a: False)
    monkeypatch.setattr("hermes_cli.model_switch.prewarm_picker_cache_async", lambda: None)
    out = []
    monkeypatch.setattr(entry, "write_json", lambda obj: out.append(obj) or True)
    store.hold("op", [SERVE])
    req = {"jsonrpc": "2.0", "id": 7, "method": "prompt.submit", "params": {"session_id": "nope"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(req) + "\n"))

    entry.main()

    (resp,) = [o for o in out if o.get("id") == 7]
    assert resp["error"]["code"] == 5075


def test_cancelling_work_counts_are_unknown_not_quiescent(store):
    """C5: +N/-N entries sum to zero; each entry must be non-negative."""
    store.hold("op", [GW], mode="freeze")
    AdmissionGate(store, GW, active_work=lambda: {"agents": 2, "bogus": -2}).publish()
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN
    assert "negative" in rep["consumers"][0]["why"]


def test_publish_forever_release_survives_shutdown_cancel_on_saturated_executor(store):
    """Prism ad88cba956ad: ``asyncio.run()`` shutdown cancels every pending task,
    including the one ``shield(to_thread(...))`` creates internally. With the
    default executor saturated that cancel drops the still-QUEUED ``stop_lease``
    job and the released record is never written. The release must be a bare
    executor future (not a task) so shutdown cannot cancel it."""
    import concurrent.futures

    gate = AdmissionGate(store, GW, active_work=Work(), lease_s=3600)
    unblock = threading.Event()
    timer = threading.Timer(0.5, unblock.set)

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        pub = asyncio.create_task(gate.publish_forever(interval=3600))
        deadline = time.monotonic() + 5
        while gate._last_record is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert gate._last_record is not None
        loop.run_in_executor(None, unblock.wait)  # saturate the only worker
        pub.cancel()
        for _ in range(5):
            await asyncio.sleep(0)  # the publisher's finally queues the release
        timer.start()  # free the worker while asyncio.run() shuts the executor down
        # return now: asyncio.run() cancels every task still pending

    try:
        asyncio.run(main())
        rec = store.read_consumer(GW)
        assert rec.get("lease_released") is True, rec
    finally:
        unblock.set()
        timer.cancel()
        gate._lease_stop.set()
