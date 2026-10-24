"""Background delegate_task units on the kanban runtime: dispatch, delivery, cancel (option C, P2).

Spec: docs/dev/delegate-background-on-kanban-runtime.md §5 and §9 P2 *Unit* (card t_da065db9).
Real store + real kanban DB under the test's sandboxed ``HERMES_HOME``; the unit's worker is a
``sleep`` subprocess (I8: cleanup holds the Popen handle, never a bare pid).
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools import async_delegation_kanban as akb
from tools import async_delegation_store as store
from tools.process_registry import process_registry

BOOT = "100:1.0"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    akb.set_dispatcher_active(False)
    yield root
    akb.set_dispatcher_active(False)
    ad._reset_for_tests()


class Spawner:
    """``spawn_fn`` for ``spawn_now``: a real sleeping worker, its Popen handle kept (I8)."""

    def __init__(self, fail: bool = False):
        self.fail, self.procs = fail, []

    def __call__(self, task, workspace, *, on_popen=None, **kw):
        if self.fail:
            raise OSError("spawn refused (test)")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        self.procs.append(proc)
        if on_popen is not None:
            on_popen(proc)
        return proc.pid

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


@pytest.fixture
def spawner():
    s = Spawner()
    yield s
    s.cleanup()


def _spec(cwd, *, runtime="kanban"):
    return {
        "profile": "default",
        "source": {"kind": "single", "tasks": [{"goal": "write the report", "role": "leaf"}],
                   "shared_context": None},
        "execution": {"model": "m", "provider": "p", "toolsets": ["file"], "max_iterations": 5,
                      "workspace_hint": str(cwd), "runtime": runtime,
                      "route": {"requested_provider": "custom:named", "model": "m", "explicit_base_url": None},
                      "credential_ref": {"provider": "p"}},
        "route": {"session_key": "agent:main:telegram:dm:1", "parent_session_id": "parent-1",
                  "origin_ui_session_id": "", "platform": "telegram", "chat_id": "1", "profile": "default"},
    }


def _sync_executor(monkeypatch):
    """Run each dispatch worker on a recorded thread so a test can join it."""
    threads = []

    def submit(fn):
        t = threading.Thread(target=fn, daemon=True)
        threads.append(t)
        t.start()
        return SimpleNamespace(add_done_callback=lambda cb: None)

    monkeypatch.setattr(ad, "_get_executor", lambda *a: SimpleNamespace(submit=submit))
    return threads


def _dispatch(monkeypatch, tmp_path, *, spawn_fn, runner=None, max_children=3, spawned=None):
    threads = _sync_executor(monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    calls = []

    def _runner():
        calls.append(time.time())
        return {"status": "completed", "summary": "in-process result"}

    handle = ad.dispatch_async_delegation_batch(
        goals=["write the report"], context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="parent-1",
        runner=runner or _runner, max_async_children=max_children,
        durable_spec=_spec(cwd), current_boot_id=BOOT,
        kanban={"profile": "default", "cwd": str(cwd), "session_id": "parent-1", "spawn_fn": spawn_fn,
                "on_spawned": spawned})
    for t in threads:
        t.join(timeout=30)
    return handle, calls


def _record(delegation_id):
    with store.locked_registry(write=False) as reg:
        return json.loads(json.dumps(reg["records"][delegation_id]))


def _card(task_id):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board="delegations") as conn:
        return kb.get_task(conn, task_id), [e.kind for e in kb.list_events(conn, task_id)]


def _drain():
    return akb.drain_profile(Path(ad._registry_path().parent.parent), BOOT)


def _queued():
    out = []
    while not process_registry.completion_queue.empty():
        out.append(process_registry.completion_queue.get_nowait())
    return out


# ── §5.1 eligibility (I6) ──────────────────────────────────────────────────


@pytest.fixture
def eligible(monkeypatch, tmp_path):
    from tools import delegate_tool_config as cfgmod

    monkeypatch.setattr(cfgmod, "_get_background_runtime", lambda: "kanban")
    monkeypatch.setattr(akb, "_terminal_backend", lambda: "local")
    monkeypatch.setattr(akb, "dispatcher_serves_here", lambda: True)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return dict(durable_spec={"execution": {}}, children=[SimpleNamespace(_inherit_mode=None)], cwd=str(cwd))


def test_eligible_when_every_condition_holds(eligible):
    assert akb.ineligible_reason(**eligible) is None


@pytest.mark.parametrize("case", ["no_durable_record", "knob_in_process", "fork_child", "docker",
                                  "modal", "backend_unreadable", "no_dispatcher", "relative_cwd",
                                  "missing_cwd"])
def test_each_condition_false_keeps_the_unit_in_process(eligible, monkeypatch, tmp_path, case):
    from tools import delegate_tool_config as cfgmod

    if case == "no_durable_record":
        eligible["durable_spec"] = None
    elif case == "knob_in_process":
        monkeypatch.setattr(cfgmod, "_get_background_runtime", lambda: "in_process")
    elif case == "fork_child":
        eligible["children"] = [SimpleNamespace(_inherit_mode=None), SimpleNamespace(_inherit_mode="fork")]
    elif case in ("docker", "modal"):
        monkeypatch.setattr(akb, "_terminal_backend", lambda: case)
    elif case == "backend_unreadable":
        monkeypatch.setattr(akb, "_terminal_backend", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    elif case == "no_dispatcher":
        monkeypatch.setattr(akb, "dispatcher_serves_here", lambda: False)
    elif case == "relative_cwd":
        eligible["cwd"] = "relative/dir"
    elif case == "missing_cwd":
        eligible["cwd"] = str(tmp_path / "nope")
    assert akb.ineligible_reason(**eligible) is not None


def test_knob_defaults_to_in_process(monkeypatch):
    from tools import delegate_tool_config as cfgmod

    monkeypatch.setattr(cfgmod, "_cfg", lambda: {})
    assert cfgmod._get_background_runtime() == "in_process"
    monkeypatch.setattr(cfgmod, "_cfg", lambda: {"background_runtime": "KANBAN"})
    assert cfgmod._get_background_runtime() == "kanban"
    monkeypatch.setattr(cfgmod, "_cfg", lambda: {"background_runtime": "bogus"})
    assert cfgmod._get_background_runtime() == "in_process"


def test_dispatcher_flag_gates_eligibility():
    assert akb.dispatcher_serves_here() is False
    akb.set_dispatcher_active(True)
    assert akb.dispatcher_serves_here() is True


def test_parent_route_persists_the_configured_provider_name():
    parent = SimpleNamespace(requested_provider="custom:my-endpoint", provider="custom", model="gpt-x")
    route = akb.parent_route(parent, {"base_url": ""})
    assert route == {"requested_provider": "custom:my-endpoint", "model": "gpt-x", "explicit_base_url": None}
    assert akb.parent_route(parent, {"base_url": "http://h/v1"})["explicit_base_url"] == "http://h/v1"


# ── §5.2 dispatch ───────────────────────────────────────────────────────────


def test_dispatch_writes_kanban_block_and_route_and_spawns_one_card(monkeypatch, tmp_path, spawner):
    closed = []
    handle, calls = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner, spawned=lambda: closed.append(1))
    did = handle["delegation_id"]
    rec = _record(did)
    assert rec["execution"]["runtime"] == "kanban"
    assert rec["execution"]["route"]["requested_provider"] == "custom:named"
    task_id = rec["kanban"]["task_id"]
    assert rec["kanban"]["board"] == "delegations"
    card, kinds = _card(task_id)
    assert card.status == "running" and kinds.count("spawned") == 1
    body = akb.parse_card_body(card.body)
    assert body["delegation_id"] == did and body["attempt_id"] == rec["attempt"]["attempt_id"]
    assert calls == [], "the in-process runner must not run for a spawned unit"
    assert closed == [1], "the gateway-built children are torn down"
    live = ad._records[did]
    assert live["kanban_unit"] and live["status"] == "running" and live["progress_fn"] is None
    assert ad.active_count() == 1 and ad.has_live_for_session(parent_session_id="parent-1")
    assert rec["state"] == "running", "no terminal write by the gateway"


def test_spawn_failure_runs_once_in_process_under_the_new_attempt(monkeypatch, tmp_path):
    handle, calls = _dispatch(monkeypatch, tmp_path, spawn_fn=Spawner(fail=True))
    did = handle["delegation_id"]
    rec = _record(did)
    assert len(calls) == 1
    assert rec["state"] == "done"
    assert rec["attempt"]["generation"] == 1 and rec["attempt"]["redispatch_count"] == 0
    assert rec["execution"]["runtime"] == "in_process" and "kanban" not in rec
    assert not any(e.get("type") == "async_delegation_restarted" for e in rec["outbox"])
    terminal = [e for e in rec["outbox"] if e.get("type") == "async_delegation"]
    assert len(terminal) == 1 and terminal[0]["payload"]["attempt_id"] == rec["attempt"]["attempt_id"]
    assert ad._records[did].get("kanban_unit") is None
    assert ad.active_count() == 0
    # The failed card is archived, never left to run.
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board="delegations") as conn:
        statuses = [r["status"] for r in conn.execute("SELECT status FROM tasks")]
    assert statuses == ["archived"]


def test_supersede_attempt_bumps_generation_only():
    did = "deleg_sup"
    rec, _ = store.persist_dispatch(delegation_id=did, durable_spec=_spec("/tmp"), boot_id=BOOT,
                                    dispatched_at=time.time())
    old = rec["attempt"]["attempt_id"]
    store.set_kanban_block(did, old, {"board": "delegations", "task_id": "t_x"})
    new = store.supersede_attempt(did, old)
    after = _record(did)
    assert new and new != old and after["attempt"]["attempt_id"] == new
    assert after["attempt"]["generation"] == 1 and after["attempt"]["redispatch_count"] == 0
    assert after["state"] == "running" and "kanban" not in after
    assert after["outbox"] == []
    assert store.supersede_attempt(did, old) is None
    assert store.append_terminal(did, old, {"status": "completed"}, "completed") is None


# ── §5.4 delivery ───────────────────────────────────────────────────────────


def _runner_writes(did, status="completed", summary="kanban result"):
    rec = _record(did)
    return store.append_terminal(did, rec["attempt"]["attempt_id"], {"status": status, "summary": summary},
                                 status, queued_boot_id=None)


def _end_card(task_id, how):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board="delegations") as conn:
        if how == "done":
            assert kb.complete_task(conn, task_id, summary="completed", force=True)
        elif how == "archived":
            assert kb.archive_task(conn, task_id)


def _deliver(payloads):
    for p in payloads:
        store.acknowledge(p["event_id"], outcome="delivered")


def test_card_end_drains_once_and_settles(monkeypatch, tmp_path, spawner):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    payload = _runner_writes(did)
    assert payload is not None
    pending = [e for e in _record(did)["outbox"] if e["type"] == "async_delegation"]
    assert pending[0]["queued_boot_id"] is None
    assert _drain()["queued"] == 0, "a running card is not drained"
    _end_card(task_id, "done")
    counts = _drain()
    queued = _queued()
    assert counts["queued"] == 1 and [q["delegation_id"] for q in queued] == [did]
    assert ad._records[did]["status"] == "completed" and ad.active_count() == 0
    assert not ad.has_live_for_session(parent_session_id="parent-1")
    _deliver(queued)
    _drain()
    card, kinds = _card(task_id)
    assert kinds.count(akb.DRAINED_EVENT) == 1 and card.status == "archived"
    assert _drain() == {"drained": 0, "queued": 0, "failed": 0, "lost": 0}
    assert _queued() == []


def test_targeted_drain_touches_only_its_record():
    rec_a, _ = store.persist_dispatch(delegation_id="deleg_a", durable_spec=_spec("/tmp"), boot_id=BOOT,
                                      dispatched_at=time.time())
    rec_b, _ = store.persist_dispatch(delegation_id="deleg_b", durable_spec=_spec("/tmp"), boot_id="other:9",
                                      dispatched_at=time.time())
    store.append_terminal("deleg_a", rec_a["attempt"]["attempt_id"], {"status": "completed"}, "completed",
                          queued_boot_id=None)
    store.append_terminal("deleg_b", rec_b["attempt"]["attempt_id"], {"status": "completed"}, "completed")
    first = store.enqueue_record_outbox("deleg_a", BOOT)
    assert [p["delegation_id"] for p in first] == ["deleg_a"]
    assert store.enqueue_record_outbox("deleg_a", BOOT) == [], "exactly once per boot"
    b_event = [e for e in _record("deleg_b")["outbox"] if e["type"] == "async_delegation"][0]
    assert b_event["queued_boot_id"] == "other:9" and b_event["state"] == "pending"


def test_card_end_watcher_vs_late_runner_write(monkeypatch, tmp_path, spawner):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    attempt = _record(did)["attempt"]["attempt_id"]
    _end_card(task_id, "archived")  # the worker died: archived with no result yet
    counts = _drain()
    assert counts["failed"] == 1
    queued = _queued()
    assert len(queued) == 1 and queued[0]["status"] == "error"
    assert "unit worker ended without a result" in queued[0]["error"]
    # The late runner write is refused by the attempt guard (I2).
    assert store.append_terminal(did, attempt, {"status": "completed"}, "completed", queued_boot_id=None) is None
    assert len([e for e in _record(did)["outbox"] if e["type"] == "async_delegation"]) == 1


@pytest.mark.parametrize("state,expect", [("completed", "completed"), ("error", "error"),
                                          ("cancelled", "cancelled")])
def test_settle_moves_the_live_record_out_of_active(monkeypatch, tmp_path, spawner, state, expect):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    if state == "cancelled":
        store.cancel_matching(parent_session_id="parent-1", reason="/stop")
    else:
        _runner_writes(did, status=state)
    _end_card(task_id, "done" if state != "cancelled" else "archived")
    _drain()
    assert ad._records[did]["status"] == expect
    assert ad.active_count() == 0
    assert "running" not in ad._ACTIVE_STATES or ad._records[did]["status"] not in ad._ACTIVE_STATES


def test_settle_refuses_non_terminal_state(monkeypatch, tmp_path, spawner):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    assert ad._settle_kanban_record(did, "running") is False
    assert ad._settle_kanban_record(did, "recoverable") is False
    assert ad._records[did]["status"] == "running"


def test_stop_archiving_a_card_whose_runner_already_wrote_done(monkeypatch, tmp_path, spawner):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    _runner_writes(did)
    # /stop arrives after the runner's write: cancel_matching skips the terminal record, the card is archived.
    assert ad.interrupt_for_session(parent_session_id="parent-1", reason="/stop") == 1
    deadline = time.time() + 15
    while _card(task_id)[0].status != "archived" and time.time() < deadline:
        time.sleep(0.1)
    assert _card(task_id)[0].status == "archived"
    _drain()
    queued = _queued()
    assert len(queued) == 1 and queued[0]["status"] == "completed"
    _deliver(queued)
    _drain()
    assert _card(task_id)[1].count(akb.DRAINED_EVENT) == 1
    assert _queued() == []


def test_claim_recoveries_skips_kanban_records():
    rec, _ = store.persist_dispatch(delegation_id="deleg_k", durable_spec=_spec("/tmp"), boot_id="dead:1",
                                    dispatched_at=time.time())
    claimed, summary = store.claim_recoveries(current_boot_id=BOOT, resume_enabled=True,
                                              owner_alive=lambda _b: False)
    assert claimed == [] and _record("deleg_k")["attempt"]["generation"] == 0
    in_proc, _ = store.persist_dispatch(delegation_id="deleg_i", durable_spec=_spec("/tmp", runtime="in_process"),
                                        boot_id="dead:1", dispatched_at=time.time())
    claimed, _ = store.claim_recoveries(current_boot_id=BOOT, resume_enabled=True, owner_alive=lambda _b: False)
    assert [c["delegation_id"] for c in claimed] == ["deleg_i"]


def test_recover_abandoned_emits_no_owner_loss_for_a_running_kanban_record(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    record = {"delegation_id": "deleg_m", "goal": "g", "session_key": "s", "origin_ui_session_id": "",
              "origin_session_id": "", "parent_session_id": "p", "dispatched_at": time.time(),
              "routing_origin": None, "role": "leaf", "model": "m", "context": None, "toolsets": None}
    store.persist_dispatch(delegation_id="deleg_m", durable_spec=_spec("/tmp"), boot_id="dead:1",
                           dispatched_at=time.time())
    ad._persist_dispatch(record)
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=?, owner_started_at=? WHERE delegation_id=?",
                     (2 ** 22 + 12345, 1.0, "deleg_m"))
    q = queue.Queue()
    assert ad.recover_abandoned_delegations() == 0
    ad.restore_undelivered_completions(q)
    assert q.empty()


@pytest.mark.parametrize("owner", ["other-live:1", ""])
def test_all_active_cancel_skips_kanban_and_cancels_in_process(monkeypatch, owner):
    monkeypatch.setattr(store, "is_boot_id_alive", lambda _b: False)
    store.persist_dispatch(delegation_id="deleg_k", durable_spec=_spec("/tmp"), boot_id="x:1",
                           dispatched_at=time.time())
    store.persist_dispatch(delegation_id="deleg_i", durable_spec=_spec("/tmp", runtime="in_process"),
                           boot_id="x:1", dispatched_at=time.time())
    n = store.cancel_matching(all_active=True, owner_boot_id=owner, reason="CLI shutdown")
    assert n == 1
    assert _record("deleg_k")["state"] == "running" and _record("deleg_i")["state"] == "cancelled"
    assert store.cancel_matching(all_active=True, owner_boot_id=owner, include_kanban=True) == 1
    assert _record("deleg_k")["state"] == "cancelled"


def test_session_scoped_cancel_still_matches_kanban():
    store.persist_dispatch(delegation_id="deleg_k", durable_spec=_spec("/tmp"), boot_id="x:1",
                           dispatched_at=time.time())
    assert store.cancel_matching(parent_session_id="parent-1", reason="/stop") == 1
    assert _record("deleg_k")["state"] == "cancelled"


@pytest.mark.parametrize("recoverable", [True, False])
def test_interrupt_all_leaves_kanban_records_alone(monkeypatch, tmp_path, spawner, recoverable):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    gate = threading.Event()
    other = ad.dispatch_async_delegation_batch(
        goals=["in-process"], context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:2", parent_session_id="parent-2",
        runner=lambda: (gate.wait(5), {"status": "completed"})[1], max_async_children=3,
        durable_spec=_spec(tmp_path, runtime="in_process"), current_boot_id=BOOT,
        interrupt_fn=gate.set)
    other_id = other["delegation_id"]
    deadline = time.time() + 5
    while ad._records[other_id].get("_started") is not True and time.time() < deadline:
        time.sleep(0.05)
    count = ad.interrupt_all(reason="shutdown", recoverable=recoverable, boot_id=BOOT)
    assert count == 1, "only the in-process record is signalled"
    assert _record(did)["state"] == "running"
    assert not ad._records[did].get("recover_on_shutdown")
    time.sleep(0.5)
    assert _card(task_id)[0].status == "running"
    assert _record(other_id)["state"] in ({"recoverable"} if recoverable else {"cancelled"})


def test_scale_to_zero_probe_excludes_kanban_units(monkeypatch, tmp_path, spawner):
    _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    assert ad.active_count() == 1 and ad.kanban_active_count() == 1
    assert ad.active_count() - ad.kanban_active_count() == 0


# ── §5.4 boot + lost card ───────────────────────────────────────────────────


def _kanban_record(did="deleg_b1", *, task_id="t_lost00001", owner="dead:1"):
    rec, _ = store.persist_dispatch(delegation_id=did, durable_spec=_spec("/tmp"), boot_id=owner,
                                    dispatched_at=time.time())
    store.set_kanban_block(did, rec["attempt"]["attempt_id"], {"board": "delegations", "task_id": task_id})
    return rec


def test_boot_observer_record_counts_and_stop_archives(monkeypatch, tmp_path, spawner):
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    ad._reset_for_tests()  # a restart: the new boot has no live record
    assert ad.active_count() == 0
    profile_home = Path(ad._registry_path().parent.parent)
    assert akb.observe_running(profile_home, "200:2") == 1
    assert ad.active_count() == 1 and ad.has_live_for_session(parent_session_id="parent-1")
    assert ad.interrupt_for_session(parent_session_id="parent-1", reason="/stop") == 1
    deadline = time.time() + 15
    while _card(task_id)[0].status != "archived" and time.time() < deadline:
        time.sleep(0.1)
    assert _card(task_id)[0].status == "archived" and _record(did)["state"] == "cancelled"
    akb.drain_profile(profile_home, "200:2")
    assert ad.active_count() == 0 and _queued() == []


def test_boot_fails_a_unit_whose_card_was_never_created(monkeypatch):
    monkeypatch.setattr(store, "is_boot_id_alive", lambda _b: False)
    store.persist_dispatch(delegation_id="deleg_nc", durable_spec=_spec("/tmp"), boot_id="dead:1",
                           dispatched_at=time.time())
    profile_home = Path(ad._registry_path().parent.parent)
    assert akb.observe_running(profile_home, BOOT) == 0
    assert _record("deleg_nc")["state"] == "failed"
    assert [q["delegation_id"] for q in _queued()] == ["deleg_nc"]


def _observe(did):
    profile_home = Path(ad._registry_path().parent.parent)
    from hermes_cli import kanban_db as kb

    kb.create_board("delegations")
    akb.observe_running(profile_home, BOOT)
    return profile_home


def test_lost_card_read_error_leaves_the_record_running(monkeypatch):
    _kanban_record()
    profile_home = _observe("deleg_b1")
    real = akb._kb

    class Boom:
        def __getattr__(self, name):
            if name == "connect_closing":
                raise RuntimeError("database is locked")
            return getattr(real(), name)

    monkeypatch.setattr(akb, "_kb", lambda: Boom())
    with pytest.raises(RuntimeError):
        akb.drain_profile(profile_home, BOOT, scan=lambda t: [])
    monkeypatch.setattr(akb, "_kb", real)
    assert _record("deleg_b1")["state"] == "running"
    # The runner's later terminal write is still accepted and delivered.
    attempt = _record("deleg_b1")["attempt"]["attempt_id"]
    assert store.append_terminal("deleg_b1", attempt, {"status": "completed"}, "completed",
                                 queued_boot_id=None) is not None


def test_lost_card_with_a_live_runner_is_not_failed():
    _kanban_record()
    profile_home = _observe("deleg_b1")
    runner = SimpleNamespace(cmdline=lambda: ["hermes", "-p", "default", "delegation-unit"])
    assert akb.drain_profile(profile_home, BOOT, scan=lambda t: [runner])["lost"] == 0
    assert _record("deleg_b1")["state"] == "running"


def test_lost_card_scan_unknown_is_not_dead():
    _kanban_record()
    profile_home = _observe("deleg_b1")
    assert akb.drain_profile(profile_home, BOOT, scan=lambda t: None)["lost"] == 0
    assert _record("deleg_b1")["state"] == "running"


def test_lost_card_reaps_a_tagged_non_runner_and_delivers_one_error():
    _kanban_record()
    profile_home = _observe("deleg_b1")
    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        import psutil

        handle = psutil.Process(leftover.pid)
        counts = akb.drain_profile(profile_home, BOOT, scan=lambda t: [handle])
        assert counts["lost"] == 1
        leftover.wait(timeout=15)
        assert leftover.returncode is not None
        assert _record("deleg_b1")["state"] == "failed"
        queued = _queued()
        assert len(queued) == 1 and queued[0]["error"] == "unit card lost"
        assert ad.active_count() == 0
        assert akb.drain_profile(profile_home, BOOT, scan=lambda t: [])["lost"] == 0
        assert _queued() == []
    finally:
        if leftover.poll() is None:
            leftover.kill()
            leftover.wait(timeout=10)


def test_lineage_scan_finds_a_tagged_process():
    import os

    env = dict(os.environ, HERMES_KANBAN_LINEAGE="t_scan0001:7")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env,
                            start_new_session=True)
    try:
        deadline = time.time() + 10
        found = None
        while time.time() < deadline:
            found = akb.lineage_processes("t_scan0001")
            if found:
                break
            time.sleep(0.1)
        assert found is not None and [p.pid for p in found] == [proc.pid]
        assert akb.lineage_processes("t_other0001") == []
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ── file_state across processes (spec §8 rows 10/29) ────────────────────────


def test_kanban_knob_turns_on_the_cross_process_file_layer_under_the_pinned_kanban_root(home, monkeypatch,
                                                                                      tmp_path):
    """A kanban unit is a sibling in another process: the knob (gateway side) and the runner flag (runner
    side) both turn the cross-process layer on, and its lock directory is ``kanban_home()``, which the
    dispatcher pins via ``HERMES_KANBAN_HOME`` so a runner under another profile home agrees (row 10)."""
    import tools.delegate_tool_config as dtc
    from tools import file_state

    monkeypatch.setattr(dtc, "_get_detached_background", lambda: False)
    monkeypatch.setattr(file_state, "FORCE_CROSS_PROCESS", False)
    monkeypatch.setattr(dtc, "_get_background_runtime", lambda: "in_process")
    assert file_state._lock_file_for(str(tmp_path / "f.txt")) is None
    monkeypatch.setattr(dtc, "_get_background_runtime", lambda: "kanban")
    pinned = tmp_path / "gateway-kanban-root"
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # the sandbox ignores the pin (spec AT-10)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(pinned))
    lock = file_state._lock_file_for(str(tmp_path / "f.txt"))
    assert lock is not None and lock.parent == pinned / "state" / "file-locks"
    monkeypatch.setattr(dtc, "_get_background_runtime", lambda: "in_process")
    monkeypatch.setattr(file_state, "FORCE_CROSS_PROCESS", True)  # what the runner sets on itself
    assert file_state._lock_file_for(str(tmp_path / "f.txt")).parent == pinned / "state" / "file-locks"


def test_a_sibling_write_from_another_process_refuses_a_stale_edit_under_the_kanban_knob(home, monkeypatch,
                                                                                         tmp_path):
    """Row 29: another (live) process published a write after this agent's read -> the edit is refused."""
    import tools.delegate_tool_config as dtc
    from tools import file_state

    monkeypatch.setattr(dtc, "_get_detached_background", lambda: False)
    monkeypatch.setattr(dtc, "_get_background_runtime", lambda: "kanban")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kroot"))
    target = tmp_path / "shared.txt"
    target.write_text("a\n")
    reg = file_state.FileStateRegistry()
    reg.record_read("unit-b", str(target))
    time.sleep(0.01)
    code = ("import sys\nfrom tools import file_state\n"
            "file_state.FORCE_CROSS_PROCESS = True\n"
            "open(sys.argv[1], 'w').write('b\\n')\n"
            "file_state.note_write('unit-a', sys.argv[1])\n"
            "print('written', flush=True)\n"
            "sys.stdin.readline()\n")
    env = {**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    proc = subprocess.Popen([sys.executable, "-c", code, str(target)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == "written"
        refusal = reg.sibling_write_conflict("unit-b", str(target))
        assert refusal and "sibling subagent 'unit-a'" in refusal
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)



# ── Prism round 1 on #2252 (one regression per finding) ───────────────────


def test_lost_card_after_the_runner_wrote_its_result_still_delivers():
    """a66f5b9cdaf5: terminal written, card row gone before the drain -> the result is queued once."""
    _kanban_record()
    profile_home = _observe("deleg_b1")
    _runner_writes("deleg_b1")
    counts = akb.drain_profile(profile_home, BOOT, scan=lambda t: [])
    queued = _queued()
    assert counts["queued"] == 1 and [q["delegation_id"] for q in queued] == ["deleg_b1"]
    assert ad.active_count() == 0
    assert akb.drain_profile(profile_home, BOOT, scan=lambda t: []) == {"drained": 0, "queued": 0,
                                                                        "failed": 0, "lost": 0}


def test_missing_board_still_fails_a_unit_whose_runner_died(monkeypatch):
    """6ad353d2c1ab: board removed + worker dead -> the unit fails and its error is delivered."""
    _kanban_record()
    profile_home = _observe("deleg_b1")
    monkeypatch.setattr(akb, "_board_state", lambda: False)
    counts = akb.drain_profile(profile_home, BOOT, scan=lambda t: [])
    assert counts["lost"] == 1 and _record("deleg_b1")["state"] == "failed"
    assert [q["error"] for q in _queued()] == ["unit card lost"] and ad.active_count() == 0


def test_board_check_error_is_not_board_absence(monkeypatch):
    _kanban_record()
    profile_home = _observe("deleg_b1")
    monkeypatch.setattr(akb, "_board_state", lambda: None)
    assert akb.drain_profile(profile_home, BOOT, scan=lambda t: [])["lost"] == 0
    assert _record("deleg_b1")["state"] == "running"


def test_cardless_observer_is_failed_once_its_launching_owner_dies(monkeypatch):
    """ee62b1f9e8d6: observed while the launcher lived; the launcher then dies -> a drain fails it."""
    rec, _ = store.persist_dispatch(delegation_id="deleg_nc2", durable_spec=_spec("/tmp"), boot_id="launcher:1",
                                    dispatched_at=time.time())
    alive = {"launcher:1": True}
    monkeypatch.setattr(akb, "_boot_alive", lambda b: alive.get(b, False))
    profile_home = _observe("deleg_nc2")
    assert ad._records["deleg_nc2"]["kanban_task_id"] in (None, "")
    akb.drain_profile(profile_home, BOOT, scan=lambda t: [])
    assert _record("deleg_nc2")["state"] == "running", "launcher alive: not failed"
    alive["launcher:1"] = False
    akb.drain_profile(profile_home, BOOT, scan=lambda t: [])
    assert _record("deleg_nc2")["state"] == "failed" and ad.active_count() == 0
    assert [q["delegation_id"] for q in _queued()] == ["deleg_nc2"]


def test_cardless_observer_adopts_the_card_once_recorded(monkeypatch):
    rec, _ = store.persist_dispatch(delegation_id="deleg_nc3", durable_spec=_spec("/tmp"), boot_id="launcher:1",
                                    dispatched_at=time.time())
    monkeypatch.setattr(akb, "_boot_alive", lambda b: True)
    profile_home = _observe("deleg_nc3")
    store.set_kanban_block("deleg_nc3", rec["attempt"]["attempt_id"], {"board": "delegations", "task_id": "t_nc3"})
    akb.drain_profile(profile_home, BOOT, scan=lambda t: [SimpleNamespace(cmdline=lambda: ["delegation-unit"])])
    assert ad._records["deleg_nc3"]["kanban_task_id"] == "t_nc3"


def test_stale_card_cleanup_leaves_a_pending_completion_to_the_drain(monkeypatch, tmp_path, spawner):
    """de44f712febf: a 49 h card whose record turned done with a pending result is NOT marked drained."""
    handle, _ = _dispatch(monkeypatch, tmp_path, spawn_fn=spawner)
    did = handle["delegation_id"]
    task_id = _record(did)["kanban"]["task_id"]
    _runner_writes(did)
    monkeypatch.setattr(akb, "STALE_CARD_SECONDS", -10)
    profile_home = Path(ad._registry_path().parent.parent)
    akb._archive_stale_cards(akb._records_of(profile_home), profile_home)
    assert akb.DRAINED_EVENT not in _card(task_id)[1]
    _end_card(task_id, "done")
    assert _drain()["queued"] == 1


def test_titled_worker_is_recognised_as_the_runner():
    """af4825de587f: setproctitle rewrote argv to the canonical worker title."""
    titled = SimpleNamespace(cmdline=lambda: ["hermes kanban-worker t_lost00001"])
    assert akb._is_runner(titled, "t_lost00001")
    assert not akb._is_runner(titled, "t_other0001")
    _kanban_record()
    profile_home = _observe("deleg_b1")
    assert akb.drain_profile(profile_home, BOOT, scan=lambda t: [titled])["lost"] == 0
    assert _record("deleg_b1")["state"] == "running"


def test_cancel_during_card_spawn_archives_the_card(monkeypatch, tmp_path):
    """9cc97d328cfa: a stop accepted while spawn_fn runs still reaches the unit's card."""
    started, release = threading.Event(), threading.Event()
    procs = []

    def slow_spawn(task, workspace, *, on_popen=None, **kw):
        started.set()
        release.wait(30)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        procs.append(proc)
        if on_popen is not None:
            on_popen(proc)
        return proc.pid

    threads = _sync_executor(monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    handle = ad.dispatch_async_delegation_batch(
        goals=["write the report"], context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="parent-1",
        runner=lambda: {"status": "completed"}, max_async_children=3, durable_spec=_spec(cwd),
        current_boot_id=BOOT, kanban={"profile": "default", "cwd": str(cwd), "session_id": "parent-1",
                                       "spawn_fn": slow_spawn})
    try:
        assert started.wait(30)
        ad.interrupt_all("/stop", include_kanban=True)
        release.set()
        for t in threads:
            t.join(timeout=30)
        did = handle["delegation_id"]
        assert _record(did)["state"] == "cancelled"
        task_id = _record(did)["kanban"]["task_id"]
        deadline = time.time() + 10
        while time.time() < deadline and _card(task_id)[0].status != "archived":
            time.sleep(0.1)
        assert _card(task_id)[0].status == "archived"
    finally:
        release.set()
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_file_state_keeps_coordinating_while_a_kanban_unit_survives_a_knob_flip(monkeypatch):
    """f5c4cfd763de: the gateway restarted with in_process still uses the shared lock while a unit lives."""
    from tools import delegate_tool_config as cfgmod
    from tools import file_state

    monkeypatch.setattr(cfgmod, "_get_background_runtime", lambda: "in_process")
    monkeypatch.setattr(cfgmod, "_get_detached_background", lambda: False)
    monkeypatch.setattr(file_state, "FORCE_CROSS_PROCESS", False)
    assert not file_state._cross_process_enabled()
    _kanban_record()
    _observe("deleg_b1")
    assert ad.kanban_active_count() == 1
    assert file_state._cross_process_enabled()
