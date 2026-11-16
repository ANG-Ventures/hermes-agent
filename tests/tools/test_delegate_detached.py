"""Detached background delegation survives a gateway restart (docs/dev/delegate-detached-background.md).

Behaviour contracts:
- a detached unit's child outlives its gateway: shutdown hands it off instead of interrupting it, and the
  next boot ADOPTS it (same attempt, no redispatch) and delivers its result;
- a dead child with no result falls back to the normal re-dispatch;
- a cancelled record's still-live child is killed at adoption;
- the outgoing supervisor never finalizes a handed-off unit;
- the heartbeat drives the stall monitor's progress token.

Child processes here are real OS processes (``python -c`` writing the job's result.json); only the
agent run inside the child is replaced.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import async_delegation as ad
from tools import delegate_detached as dd
from tools.process_registry import process_registry

DEAD_BOOT = "2147483646:1.0"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    _drain()
    spawned: list = []
    yield spawned
    for proc in spawned:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    ad._reset_for_tests()
    _drain()


def _drain() -> list:
    out = []
    while not process_registry.completion_queue.empty():
        out.append(process_registry.completion_queue.get_nowait())
    return out


def _record(delegation_id: str, *, owner: str, state: str = "running", detached: bool = True) -> dict:
    now = time.time()
    return {
        "delegation_id": delegation_id, "state": state, "created_at": now, "updated_at": now,
        "profile": "default",
        "source": {"kind": "single", "shared_context": None,
                   "tasks": [{"goal": "port the module", "context": "", "role": "leaf", "inherit_context": False}]},
        "execution": {"model": "test-model", "provider": "test-provider", "toolsets": ["file"],
                      "max_iterations": 50, "parent_depth": 0, "workspace_hint": "/tmp",
                      "credential_ref": {"source": "provider", "provider": "test-provider"},
                      "detached": detached},
        "route": {"session_key": "agent:main:telegram:dm:123", "parent_session_id": "parent-1",
                  "origin_ui_session_id": "", "platform": "telegram", "chat_type": "dm", "chat_id": "123",
                  "user_id": "u1", "user_name": "Ace", "profile": "default"},
        "attempt": {"attempt_id": f"{delegation_id}:g0:a", "generation": 0, "redispatch_count": 0,
                    "owner_boot_id": owner, "started_at": now, "submitted_at": now,
                    "last_interrupted_at": None, "last_error": None},
        "terminal": None, "outbox": [],
    }


def _write(*records: dict) -> None:
    ad._write_registry_for_tests({"schema_version": 1, "updated_at": time.time(),
                                  "records": {r["delegation_id"]: r for r in records}})


def _load(delegation_id: str) -> dict:
    return json.loads(ad._registry_path().read_text(encoding="utf-8"))["records"][delegation_id]


def _fake_child(spawned: list, job_dir: Path, *, seconds: float, summary: str = "child done",
                write_result: bool = True) -> subprocess.Popen:
    """A real detached process standing in for ``python -m tools.delegate_detached``."""
    job_dir.mkdir(parents=True, exist_ok=True)
    result = json.dumps({"schema_version": dd.SCHEMA_VERSION, "status": "completed", "summary": summary,
                         "api_calls": 3, "duration_seconds": seconds})
    code = (f"import time,pathlib,os; time.sleep({seconds});"
            + (f"p=pathlib.Path({str(job_dir)!r}); t=p/'.r'; t.write_text({result!r}); os.replace(t, p/'result.json')"
               if write_result else ""))
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    spawned.append(proc)
    return proc


def _detached_block(proc: subprocess.Popen, job_dir: Path, attempt_id: str) -> dict:
    return {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid), "job_dir": str(job_dir),
            "attempt_id": attempt_id, "spawned_at": time.time()}


def _wait_for_event(delegation_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in _drain():
            if evt.get("delegation_id") == delegation_id and evt.get("type") == "async_delegation":
                return evt
        time.sleep(0.1)
    raise AssertionError(f"no completion for {delegation_id}")


def _refuse_redispatch(record, continuation):
    raise AssertionError("a live detached child must be adopted, never re-dispatched")


def test_restart_adopts_live_child_and_delivers_its_result_without_redispatch(_isolated, tmp_path):
    """The red-on-base contract: the unit's work survives the gateway that started it.

    Base code re-dispatches a running record whose owner boot is dead (generation 1, work lost).
    Here the child keeps running, the new boot adopts it at generation 0 and delivers ITS result."""
    record = _record("deleg_live", owner=DEAD_BOOT)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    child = _fake_child(_isolated, job_dir, seconds=8, summary="work from the original child")
    record["detached"] = _detached_block(child, job_dir, record["attempt"]["attempt_id"])
    _write(record)

    summary = ad.recover_async_delegations(current_boot_id="new-boot:1.0", runner_factory=_refuse_redispatch,
                                           profile_home=tmp_path)

    assert summary["claimed"] == 0
    assert child.poll() is None, "adoption must not touch a live child"
    evt = _wait_for_event("deleg_live", timeout=60)
    assert evt["summary"] == "work from the original child"
    assert evt["attempt_generation"] == 0
    stored = _load("deleg_live")
    assert stored["state"] == "done"
    assert stored["attempt"]["owner_boot_id"] == "new-boot:1.0"
    assert stored["attempt"]["redispatch_count"] == 0


def test_shutdown_hands_off_a_live_detached_unit_and_the_next_boot_delivers(_isolated, tmp_path, monkeypatch):
    """Gateway A dispatches a detached unit, shuts down mid-run (recoverable), boot B adopts it.

    A's shutdown must not signal the child and A's supervisor must not finalize it."""
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
    holder: dict = {}

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        proc = _fake_child(_isolated, job_dir, seconds=10, summary="survived the restart")
        holder["proc"] = proc
        return dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)

    monkeypatch.setattr(dd, "spawn_child", fake_spawn)
    spec = {k: v for k, v in _record("x", owner="boot-a:1.0").items() if k in {"profile", "source", "execution", "route"}}
    handle = ad.dispatch_async_delegation_batch(
        goals=["port the module"], context=None, toolsets=None, role="leaf", model="test-model",
        session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=lambda: pytest.fail("a detached unit must not run in-process"),
        durable_spec=spec, current_boot_id="boot-a:1.0", detached={"live_delegation_id": None},
    )
    assert handle["status"] == "dispatched"
    delegation_id = handle["delegation_id"]
    deadline = time.time() + 10
    while "detached" not in _load(delegation_id) and time.time() < deadline:
        time.sleep(0.05)
    assert _load(delegation_id)["detached"]["pid"] == holder["proc"].pid

    ad.interrupt_all(reason="gateway shutdown (restart)", recoverable=True)

    assert holder["proc"].poll() is None, "shutdown must not signal the detached child"
    assert _load(delegation_id)["state"] == "recoverable"
    time.sleep(1.2)  # A's supervisor notices the hand-off and leaves the unit alone
    summary = ad.recover_async_delegations(current_boot_id="boot-b:1.0", runner_factory=_refuse_redispatch,
                                           profile_home=tmp_path)

    assert summary["claimed"] == 0, json.dumps(_load(delegation_id), indent=1, default=str)[:4000]
    evt = _wait_for_event(delegation_id, timeout=60)
    assert evt["summary"] == "survived the restart"
    assert _load(delegation_id)["attempt"]["owner_boot_id"] == "boot-b:1.0"
    assert not _drain(), "exactly one completion: the adopter's"


def test_dead_child_without_result_is_redispatched(_isolated, tmp_path, monkeypatch):
    record = _record("deleg_dead", owner=DEAD_BOOT)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    child = _fake_child(_isolated, job_dir, seconds=0, write_result=False)
    child.wait(timeout=10)
    record["detached"] = _detached_block(child, job_dir, record["attempt"]["attempt_id"])
    record["detached"]["create_time"] = 1.0  # the pid is gone (or recycled)
    _write(record)
    monkeypatch.setattr(ad, "_detached_enabled", lambda: True)  # the contract, independent of the default
    respawned = []
    monkeypatch.setattr(dd, "spawn_child", lambda **kw: respawned.append(kw["attempt_id"]))  # -> in-process
    calls = []

    def factory(rec, continuation):
        calls.append(rec["attempt"]["generation"])
        return lambda: {"status": "completed", "summary": "re-run"}

    summary = ad.recover_async_delegations(current_boot_id="new-boot:1.0", runner_factory=factory,
                                           profile_home=tmp_path)

    assert summary["claimed"] == 1
    assert calls == [1]
    assert _wait_for_event("deleg_dead", timeout=60)["summary"] == "re-run"
    assert len(respawned) == 1 and respawned[0].startswith("deleg_dead:g1:"), "re-dispatch spawns a new child"


def test_spawn_window_child_found_by_argv_is_adopted(_isolated, tmp_path):
    """Gateway died after Popen, before pid.json / the registry block: only spawn.json exists."""
    record = _record("deleg_window", owner=DEAD_BOOT)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    (job_dir / "spawn.json").write_text("{}")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--job", str(job_dir)],
                            start_new_session=True)
    _isolated.append(proc)
    _write(record)

    adopted, kill = ad._store.claim_detached_adoptions(
        current_boot_id="new-boot:1.0", profile_home=tmp_path, probe=dd.probe_job,
        identity=dd.job_identity, job_dir_for=dd.job_dir_for)

    assert [r["delegation_id"] for r in adopted] == ["deleg_window"]
    assert adopted[0]["detached"]["pid"] == proc.pid
    assert kill == []


def test_cancelled_record_with_live_child_is_killed_at_adoption(_isolated, tmp_path):
    record = _record("deleg_cancel", owner=DEAD_BOOT, state="cancelled")
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    child = _fake_child(_isolated, job_dir, seconds=30)
    record["detached"] = _detached_block(child, job_dir, record["attempt"]["attempt_id"])
    _write(record)

    ad.recover_async_delegations(current_boot_id="new-boot:1.0", runner_factory=_refuse_redispatch,
                                 profile_home=tmp_path)

    assert child.wait(timeout=10) == -signal.SIGTERM
    assert not _drain()


def test_spawn_fallback_runs_in_process_and_is_no_longer_detached(_isolated, tmp_path, monkeypatch):
    """degraded scope / failed spawn: the unit runs as a thread and shutdown treats it as one."""
    monkeypatch.setattr(dd, "spawn_child", lambda **_: None)
    spec = {k: v for k, v in _record("x", owner="boot-a:1.0").items() if k in {"profile", "source", "execution", "route"}}
    handle = ad.dispatch_async_delegation_batch(
        goals=["port the module"], context=None, toolsets=None, role="leaf", model="test-model",
        session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=lambda: {"results": [{"task_index": 0, "status": "completed", "summary": "thread ran"}]},
        durable_spec=spec, current_boot_id="boot-a:1.0", detached={"live_delegation_id": None},
    )
    evt = _wait_for_event(handle["delegation_id"], timeout=60)
    assert evt["results"][0]["summary"] == "thread ran"
    assert _load(handle["delegation_id"])["execution"]["detached"] is False


def test_heartbeat_token_drives_stall_progress(tmp_path):
    progress = dd.heartbeat_progress_fn(lambda: tmp_path)
    assert progress() == (None, False)
    beat = {"schema_version": dd.SCHEMA_VERSION, "token": [["sa-1", 2, None, 5.0]], "in_tool": False, "ts": 1}
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat))
    first = progress()
    assert progress() == first, "a frozen child reads as no progress"
    beat.update(token=[["sa-1", 3, "terminal", 9.0]], in_tool=True)
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat))
    advanced = progress()
    assert advanced[0] != first[0] and advanced[1] is True
    beat["schema_version"] = dd.SCHEMA_VERSION + 1
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat))
    assert progress() == (None, False), "an unknown protocol never reads as progress"


def test_terminate_group_escalates_to_sigkill_without_blocking(_isolated):
    proc = subprocess.Popen([sys.executable, "-c",
                             "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                            start_new_session=True)
    _isolated.append(proc)
    time.sleep(0.5)
    timers = []

    class _Timer:
        def __init__(self, delay, fn, args):
            self.delay, self.fn, self.args, self.daemon = delay, fn, args, False

        def start(self):
            timers.append(self)

    assert dd.terminate_group(proc.pid, dd.process_create_time(proc.pid), timer_factory=_Timer) is True
    assert proc.poll() is None and timers[0].delay == dd.SIGKILL_GRACE_SECONDS
    timers[0].fn(*timers[0].args)
    assert proc.wait(timeout=10) == -signal.SIGKILL


def test_first_run_reconstruction_keeps_task_context(monkeypatch):
    from tools import delegate_tool as dt

    seen = {}
    monkeypatch.setattr(dt, "delegate_task", lambda **kw: seen.update(kw) or json.dumps({"results": []}))
    record = _record("deleg_ctx", owner=DEAD_BOOT)
    record["source"]["tasks"][0]["context"] = "the brief"

    dt.build_recovered_delegation_runner(record, None, object(), source="gateway_detached")()

    assert seen["tasks"][0]["context"] == "the brief"


def test_reconstruction_prefers_the_dispatching_parents_toolsets(monkeypatch):
    built = {}

    class _Agent:
        def __init__(self, **kwargs):
            built.update(kwargs)

    import types
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))
    record = _record("deleg_ts", owner=DEAD_BOOT)
    record["execution"].update(toolsets=["file"], parent_toolsets=["file", "terminal", "delegation"],
                               parent_disabled_toolsets=["kanban"])

    dd.build_parent_and_runner(record, None, {}, resolve_default=dict,
                               resolve_for_provider=lambda _p, **_kw: {"provider": "test-provider"})

    assert built["enabled_toolsets"] == ["file", "terminal", "delegation"]
    assert built["disabled_toolsets"] == ["kanban"]


def _dispatch_with_fake_child(_isolated, monkeypatch, *, seconds: float, summary: str = "child done"):
    holder: dict = {}

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        proc = _fake_child(_isolated, job_dir, seconds=seconds, summary=summary)
        holder.update(proc=proc, job_dir=job_dir)
        return dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)

    monkeypatch.setattr(dd, "spawn_child", fake_spawn)
    spec = {k: v for k, v in _record("x", owner="boot-a:1.0").items() if k in {"profile", "source", "execution", "route"}}
    handle = ad.dispatch_async_delegation_batch(
        goals=["port the module"], context=None, toolsets=None, role="leaf", model="test-model",
        session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=lambda: pytest.fail("a detached unit must not run in-process"),
        durable_spec=spec, current_boot_id="boot-a:1.0", detached={"live_delegation_id": None},
    )
    assert handle["status"] == "dispatched"
    return handle["delegation_id"], holder


def test_failed_registry_advertise_terminates_the_spawned_child(_isolated, tmp_path, monkeypatch):
    """A child nobody can supervise or adopt must not keep running (Prism P1 4eb695d627ba)."""
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")

    def _enospc(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ad._store, "set_detached_block", _enospc)
    delegation_id, holder = _dispatch_with_fake_child(_isolated, monkeypatch, seconds=30)

    evt = _wait_for_event(delegation_id, timeout=60)
    assert holder["proc"].wait(timeout=20) == -signal.SIGTERM
    assert evt["status"] != "completed"
    assert not holder["job_dir"].exists(), "an un-advertised job dir must not look adoptable"


def test_outgoing_supervisor_leaves_an_adopters_record_alone(_isolated, tmp_path, monkeypatch):
    """Recovery replaced the live record before the handed-off supervisor noticed: the outgoing
    supervisor must neither finalize the adopter's record nor delete the job dir (Prism P1 dec1da79aa87)."""
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
    delegation_id, holder = _dispatch_with_fake_child(_isolated, monkeypatch, seconds=10)
    deadline = time.time() + 10
    while "detached" not in _load(delegation_id) and time.time() < deadline:
        time.sleep(0.05)
    with ad._records_lock:
        attempt_id = ad._records[delegation_id]["attempt_id"]

    ad.interrupt_all(reason="gateway shutdown (restart)", recoverable=True)
    adopter = {"delegation_id": delegation_id, "status": "running", "attempt_id": attempt_id,
               "registry_profile_home": str(tmp_path), "slot_key": delegation_id}
    with ad._records_lock:
        ad._records[delegation_id] = adopter  # boot B's adoption, same process
    time.sleep(dd.POLL_SECONDS * 2.5)  # A's supervisor observes the hand-off and exits

    assert adopter["status"] == "running", "the outgoing supervisor finalized the adopter's record"
    assert holder["job_dir"].exists(), "the outgoing supervisor deleted the live child's job dir"
    assert holder["proc"].poll() is None
    assert not _drain()


def _hold_registry_lock_in_another_process(_isolated, seconds: float) -> subprocess.Popen:
    """A sibling process (another gateway's reader) takes async-delegations.lock for ``seconds`` and
    stamps itself the way ``locked_registry`` does."""
    lpath = ad._registry_path().with_suffix(".lock")
    lpath.parent.mkdir(parents=True, exist_ok=True)
    code = ("import fcntl,os,sys,time; h=open(sys.argv[1],'a+b'); fcntl.flock(h.fileno(), fcntl.LOCK_EX);"
            " h.truncate(0); h.write(f'pid={os.getpid()} caller=test_reader'.encode()); h.flush();"
            " print('held', flush=True); time.sleep(float(sys.argv[2]))")
    proc = subprocess.Popen([sys.executable, "-c", code, str(lpath), str(seconds)],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    _isolated.append(proc)
    assert proc.stdout.readline().strip() == "held"
    return proc


def _dispatch_with_lock_held_at_spawn(_isolated, monkeypatch, *, hold_seconds: float, child_seconds: float):
    """t_ad0710c9: the lock is taken by another process right after the child spawns, i.e. across
    ``set_detached_block`` -- the 2026-11-16 deleg_5aff89ab timeline (8.06 s read hold)."""
    holder: dict = {}

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        proc = _fake_child(_isolated, job_dir, seconds=child_seconds, summary="ran despite the held lock")
        holder.update(proc=proc, job_dir=job_dir,
                      lock_holder=_hold_registry_lock_in_another_process(_isolated, hold_seconds))
        return dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)

    monkeypatch.setattr(dd, "spawn_child", fake_spawn)
    spec = {k: v for k, v in _record("x", owner="boot-a:1.0").items() if k in {"profile", "source", "execution", "route"}}
    handle = ad.dispatch_async_delegation_batch(
        goals=["port the module"], context=None, toolsets=None, role="leaf", model="test-model",
        session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=lambda: pytest.fail("a detached unit must not run in-process"),
        durable_spec=spec, current_boot_id="boot-a:1.0", detached={"live_delegation_id": None},
    )
    assert handle["status"] == "dispatched"
    return handle["delegation_id"], holder


def test_registry_lock_held_8s_by_another_process_does_not_lose_the_detached_unit(_isolated, monkeypatch):
    """Red on base: set_detached_block timed out after 5 s, the child was stood down and the parent got
    'stopped before its detached child started work' with zero work done (deleg_5aff89ab)."""
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
    delegation_id, holder = _dispatch_with_lock_held_at_spawn(_isolated, monkeypatch, hold_seconds=8,
                                                              child_seconds=10)

    evt = _wait_for_event(delegation_id, timeout=60)

    assert evt["status"] == "completed", evt.get("error")
    assert evt["summary"] == "ran despite the held lock"
    assert holder["lock_holder"].wait(timeout=5) == 0


def test_registry_lock_held_past_the_budget_fails_fast_naming_the_lock_and_holder(_isolated, monkeypatch):
    """When the hold outlasts the retry budget the parent learns within seconds, and the error names the
    lock and the pid holding it (base: a bare 'stopped before its detached child started work')."""
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
    monkeypatch.setattr(ad._store, "DETACHED_BLOCK_BUDGET_SECONDS", 1.0, raising=False)
    started = time.monotonic()
    # 3 s: past the 1 s advertise budget, inside the stand-down's own lock wait (the 11-16 shape).
    delegation_id, holder = _dispatch_with_lock_held_at_spawn(_isolated, monkeypatch, hold_seconds=3,
                                                              child_seconds=30)

    evt = _wait_for_event(delegation_id, timeout=25)

    assert evt["status"] != "completed"
    assert time.monotonic() - started < 20, "the parent must learn within seconds, not after the hold"
    assert "async-delegations.lock" in (evt.get("error") or ""), evt.get("error")
    assert f"pid={holder['lock_holder'].pid}" in evt["error"], evt["error"]


def test_orphan_recovery_does_not_hold_the_registry_lock_while_state_db_is_busy(_isolated, tmp_path):
    """The untagged 6-8 s write=False holds: recover_abandoned_delegations (every 30 s orphan sweep) held
    async-delegations.lock across its whole state.db pass, so a busy state.db writer stretched the JSON
    lock hold and a dispatch-path writer timed out behind it (t_ad0710c9 measurement)."""
    import sqlite3
    import threading

    record = _record("deleg_done", owner=DEAD_BOOT, state="done", detached=False)
    record["terminal"] = {"status": "completed", "completed_at": time.time(), "error": None, "result": None}
    _write(record)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("INSERT INTO async_delegations (delegation_id, origin_session, state, delivery_state, "
                     "dispatched_at, owner_pid, owner_started_at, task_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("deleg_done", "k", "running", "pending", time.time(), 1, 1.0, "{}", time.time()))
        conn.commit()
    held = threading.Event()

    def busy_state_db_writer():
        c = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(4)
        c.execute("COMMIT")
        c.close()

    threading.Thread(target=busy_state_db_writer, daemon=True).start()
    held.wait()
    sweep = threading.Thread(target=ad.recover_abandoned_delegations, daemon=True)
    sweep.start()
    time.sleep(0.5)  # the sweep is now waiting on state.db

    started = time.monotonic()
    with ad._store.locked_registry(timeout=2.0):
        waited = time.monotonic() - started
    sweep.join(timeout=15)

    assert waited < 1.0, f"a registry writer waited {waited:.1f}s behind the orphan sweep's state.db wait"
