"""Detached delegation safety contracts (Prism P1 triage of #2135, t_31528621).

- a process is signalled only under a VERIFIED identity (pid + kernel start time);
- SIGKILL escalation reaches a tool that outlived its SIGTERM'd leader;
- a child already spawned is never orphaned by a failed bookkeeping write;
- a dispatcher standing a child down never kills one another boot already adopted;
- an adopted supervisor queued behind a full pool is not judged stalled;
- a detached infrastructure error is an error, never a completed batch;
- a result published between probe checks is delivered, never re-run;
- the reconstructed parent persists its children to the owning profile's SessionDB;
- an explicit inherit_mode keeps its inherited context across the process boundary;
- an age-capped orphan whose kill was never sent is killed by the next boot;
- the detached worker never overwrites the gateway's process checkpoints.
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

_REAL_SPAWN_CHILD = dd.spawn_child  # captured before the conftest guard patches it per test
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


def _record(delegation_id: str, *, owner: str, state: str = "running", created_at: float | None = None) -> dict:
    now = time.time()
    return {
        "delegation_id": delegation_id, "state": state, "created_at": created_at or now, "updated_at": now,
        "profile": "default",
        "source": {"kind": "single", "shared_context": None,
                   "tasks": [{"goal": "port the module", "context": "", "role": "leaf", "inherit_context": False}]},
        "execution": {"model": "test-model", "provider": "test-provider", "toolsets": ["file"],
                      "max_iterations": 50, "parent_depth": 0, "workspace_hint": "/tmp",
                      "credential_ref": {"source": "provider", "provider": "test-provider"}, "detached": True},
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


def _sleeper(spawned: list, seconds: float = 60, extra_argv: tuple = ()) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})", *extra_argv],
                            start_new_session=True)
    spawned.append(proc)
    time.sleep(0.2)
    return proc


def _wait_for_event(delegation_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in _drain():
            if evt.get("delegation_id") == delegation_id and evt.get("type") == "async_delegation":
                return evt
        time.sleep(0.1)
    raise AssertionError(f"no completion for {delegation_id}")


class _Timer:
    created: list = []

    def __init__(self, delay, fn, args):
        self.delay, self.fn, self.args, self.daemon = delay, fn, args, False

    def start(self):
        _Timer.created.append(self)


def _spec() -> dict:
    return {k: v for k, v in _record("x", owner="boot-a:1.0").items() if k in {"profile", "source", "execution", "route"}}


def _dispatch(monkeypatch, fake_spawn) -> str:
    monkeypatch.setattr(dd, "spawn_child", fake_spawn)
    handle = ad.dispatch_async_delegation_batch(
        goals=["port the module"], context=None, toolsets=None, role="leaf", model="test-model",
        session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=lambda: pytest.fail("a detached unit must not run in-process"),
        durable_spec=_spec(), current_boot_id="boot-a:1.0", detached={"live_delegation_id": None},
    )
    assert handle["status"] == "dispatched"
    return handle["delegation_id"]


# ── 5a34992b5c92 / 0a845db29ae6: identity ──────────────────────────────────
def test_an_identity_without_a_start_time_is_never_signalled(_isolated, tmp_path):
    proc = _sleeper(_isolated)

    assert dd.signal_group(proc.pid, None, signal.SIGTERM) is False
    assert dd.terminate_group(proc.pid, None, timer_factory=_Timer) is False
    time.sleep(0.3)
    assert proc.poll() is None, "a pid with no verified start time was signalled"
    # A recorded pid without a start time is only trusted when its argv names this exact job.
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    assert dd.job_identity(job_dir, {"pid": proc.pid, "create_time": None}) is None
    owned = _sleeper(_isolated, extra_argv=("--job", str(job_dir)))
    ident = dd.job_identity(job_dir, {"pid": owned.pid, "create_time": None})
    assert ident and ident["pid"] == owned.pid and ident["create_time"] is not None


# ── a873e9791964: escalation outlives the leader ───────────────────────────
@pytest.mark.live_system_guard_bypass  # the surviving worker is reparented to init: real delivery is the point
def test_sigkill_escalation_reaches_a_tool_that_outlived_its_leader(_isolated, tmp_path):
    marker = tmp_path / "worker.pid"
    leader_code = (
        "import signal,subprocess,sys,time,os\n"
        "w=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"open({str(marker)!r},'w').write(str(w.pid))\n"
        "signal.signal(signal.SIGTERM, lambda *a: os._exit(0))\n"
        "time.sleep(120)\n")
    leader = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
    _isolated.append(leader)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    worker_pid = int(marker.read_text())
    _Timer.created = []

    assert dd.terminate_group(leader.pid, dd.process_create_time(leader.pid), timer_factory=_Timer) is True
    assert leader.wait(timeout=10) == 0
    assert dd.pid_alive(worker_pid, dd.process_create_time(worker_pid)), "worker ignores SIGTERM"
    _Timer.created[0].fn(*_Timer.created[0].args)  # the grace timer fires after the leader is gone

    deadline = time.time() + 10
    while dd.pid_alive(worker_pid, dd.process_create_time(worker_pid)) and time.time() < deadline:
        time.sleep(0.05)
    assert not dd.pid_alive(worker_pid, dd.process_create_time(worker_pid)), "worker survived SIGKILL escalation"


# ── 2d058dbbdb27: a spawned child is never lost to a pid.json failure ──────
def test_pid_file_failure_after_popen_still_returns_the_live_child(_isolated, tmp_path, monkeypatch):
    real_popen = subprocess.Popen

    def _popen(argv, **kwargs):
        proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        _isolated.append(proc)
        return proc

    real_write = dd._write_json

    def _write_json(path, data):
        if Path(path).name == "pid.json":
            raise OSError(28, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(dd.subprocess, "Popen", _popen)
    monkeypatch.setattr(dd, "_write_json", _write_json)
    record = _record("deleg_pidfile", owner="boot-a:1.0")

    spawned = _REAL_SPAWN_CHILD(record=record, attempt_id=record["attempt"]["attempt_id"], profile_home=tmp_path,
                                continuation=None, live_delegation_id=None)

    assert spawned is not None and spawned.popen.poll() is None, "the caller lost the handle to a running child"


# ── 3f85b1480eee / cfbb2577cd60: stand-down vs an overlapping boot's adoption ──
def test_stand_down_never_kills_a_child_another_boot_already_adopted(_isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
    holder: dict = {}

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        proc = _sleeper(_isolated, 20)
        holder.update(proc=proc, job_dir=job_dir)
        spawned = dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)
        (job_dir / "pid.json").write_text(json.dumps({"pid": proc.pid, "create_time": spawned.create_time}))
        return spawned

    def racing_install(delegation_id, *a, **k):
        # Shutdown flags the unit, boot B adopts it, THEN A's supervision install sees the shutdown.
        ad._store.transition_owned([delegation_id], "recoverable", timeout=5, reason="restart", caller="test")
        adopted, _ = ad._store.claim_detached_adoptions(
            current_boot_id="boot-b:1.0", profile_home=tmp_path, probe=dd.probe_job,
            identity=dd.job_identity, job_dir_for=dd.job_dir_for)
        holder["adopted"] = [r["delegation_id"] for r in adopted]
        return False

    monkeypatch.setattr(ad, "_install_detached_supervision", racing_install)
    delegation_id = _dispatch(monkeypatch, fake_spawn)
    deadline = time.time() + 10
    while "adopted" not in holder and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(1.5)

    assert holder["adopted"] == [delegation_id]
    assert holder["proc"].poll() is None, "boot A killed the child boot B adopted"
    assert holder["job_dir"].exists(), "boot A deleted the adopted child's job dir"
    assert _load(delegation_id)["attempt"]["owner_boot_id"] == "boot-b:1.0"
    assert not _drain(), "boot A must not finalize boot B's unit"


# ── 987614e4d2a0 (#2160 Prism): an unknown stand-down outcome must not kill ──
def _dispatch_with_unreadable_fence(_isolated) -> tuple:
    holder: dict = {"fence_calls": 0, "terminated": []}

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        proc = _sleeper(_isolated, 30)
        holder.update(proc=proc, job_dir=job_dir)
        spawned = dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)
        (job_dir / "pid.json").write_text(json.dumps({"pid": proc.pid, "create_time": spawned.create_time}))
        return spawned

    def broken_fence(*a, **k):
        holder["fence_calls"] += 1
        raise OSError("registry unreadable")

    real_terminate = dd.terminate_group
    with pytest.MonkeyPatch.context() as mp:  # scoped: the next boot reads a healthy registry
        mp.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")
        mp.setattr(ad, "_STAND_DOWN_RETRY_DELAYS", (0.0, 0.0), raising=False)
        mp.setattr(ad, "_install_detached_supervision", lambda *a, **k: False)
        mp.setattr(ad._store, "stand_down_detached", broken_fence)
        mp.setattr(dd, "terminate_group", lambda *a, **k: holder["terminated"].append(a) or real_terminate(*a, **k))
        delegation_id = _dispatch(mp, fake_spawn)
        deadline = time.time() + 10
        while holder["fence_calls"] < 3 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(1.0)
    return delegation_id, holder


def test_an_unfenceable_child_is_left_running_and_adopted_by_the_next_boot(_isolated, tmp_path, monkeypatch):
    delegation_id, holder = _dispatch_with_unreadable_fence(_isolated)

    assert holder["fence_calls"] == 3, "the fence is retried before the outcome is called unknown"
    assert holder["terminated"] == [], "an unknown stand-down outcome signalled the child"
    assert holder["proc"].poll() is None
    assert holder["job_dir"].exists(), "an unknown stand-down outcome removed the job dir"
    assert not _drain(), "an unknown stand-down outcome finalized the unit"

    adopted, kill = ad._store.claim_detached_adoptions(
        current_boot_id="boot-b:1.0", profile_home=tmp_path, owner_alive=lambda boot: False,
        probe=dd.probe_job, identity=dd.job_identity, job_dir_for=dd.job_dir_for,
        stand_down_unknown=dd.stand_down_unknown_dirs)

    assert [r["delegation_id"] for r in adopted] == [delegation_id] and kill == []
    assert adopted[0]["detached"]["pid"] == holder["proc"].pid
    assert dd.stand_down_unknown_dirs(tmp_path) == []


def test_an_unfenceable_child_nobody_owns_is_killed_by_the_next_boot(_isolated, tmp_path, monkeypatch):
    delegation_id, holder = _dispatch_with_unreadable_fence(_isolated)
    assert holder["proc"].poll() is None
    ad._store.cancel_matching(all_active=True, owner_boot_id="", reason="test", caller="test")
    ad._reset_for_tests()

    ad.recover_async_delegations(current_boot_id="boot-b:1.0", profile_home=tmp_path,
                                 runner_factory=lambda *a: pytest.fail("an orphan is never re-run"))

    holder["proc"].wait(timeout=10)
    assert not holder["job_dir"].exists()


def test_a_stood_down_attempt_is_not_adoptable(_isolated, tmp_path):
    record = _record("deleg_fence", owner="boot-a:1.0", state="recoverable")
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 20, extra_argv=("--job", str(job_dir)))
    (job_dir / "spawn.json").write_text("{}")
    _write(record)

    assert ad._store.stand_down_detached("deleg_fence", record["attempt"]["attempt_id"], "boot-a:1.0",
                                         profile_home=tmp_path) is True
    adopted, kill = ad._store.claim_detached_adoptions(
        current_boot_id="boot-b:1.0", profile_home=tmp_path, probe=dd.probe_job,
        identity=dd.job_identity, job_dir_for=dd.job_dir_for)

    # Never adopted; the fenced child itself is killed (its dispatcher may have died before doing so).
    assert adopted == []
    assert [k.get("pid") for k in kill] == [proc.pid] and kill[0].get("fenced") == "deleg_fence"


# ── cea4126db419: adopted supervisors queued behind a full pool ─────────────
def test_an_adopted_unit_queued_behind_a_full_pool_is_not_stalled(_isolated, tmp_path):
    import threading

    record = _record("deleg_queued", owner=DEAD_BOOT)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 30)
    record["detached"] = {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid), "job_dir": str(job_dir),
                          "attempt_id": record["attempt"]["attempt_id"], "spawned_at": time.time()}
    _write(record)
    release = threading.Event()
    ad._get_executor(1).submit(release.wait, 30)  # another profile's long supervisor holds the only worker
    try:
        ad.recover_async_delegations(current_boot_id="new-boot:1.0", profile_home=tmp_path, max_async_children=1,
                                     runner_factory=lambda *a: pytest.fail("adopt, never re-dispatch"))
        with ad._records_lock:
            stalled, _, _ = ad._sweep_stale_locked(time.time() + 10 * 86400)
        assert stalled == [], "a supervisor that never started was judged stalled"
    finally:
        release.set()


# ── 65abe792be6f / 38f0f25f1599: infrastructure errors on initial dispatch ─
def test_a_child_that_exits_without_a_result_is_an_error_not_a_completed_batch(_isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "current_boot_id", lambda: "boot-a:1.0")

    def fake_spawn(*, record, attempt_id, profile_home, continuation, live_delegation_id):
        job_dir = dd.job_dir_for(profile_home, attempt_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"], start_new_session=True)
        _isolated.append(proc)
        return dd.DetachedSpawn(job_dir, proc.pid, dd.process_create_time(proc.pid), proc)

    delegation_id = _dispatch(monkeypatch, fake_spawn)
    evt = _wait_for_event(delegation_id)

    assert evt["status"] == "error", evt
    assert _load(delegation_id)["state"] == "failed"


# ── d5f8557f9417: result published between the two probe checks ───────────
def test_a_result_published_between_probe_checks_is_never_read_as_dead(tmp_path, monkeypatch):
    def _identity(job_dir, block):
        (job_dir / "result.json").write_text("{}")  # the child publishes and exits right here
        return {"pid": 2 ** 22 + 7, "create_time": 1.0}

    monkeypatch.setattr(dd, "job_identity", _identity)

    assert dd.probe_job(tmp_path) == "result"


# ── f5512eeb584d / 1af17e8fecd2: owning SessionDB ──────────────────────────
def test_reconstructed_parent_runs_its_children_on_the_profiles_session_db(monkeypatch):
    import types

    import hermes_state_registry
    from tools import delegate_tool as dt

    class _Agent:
        def __init__(self, **kwargs):
            self._session_db = None

    sentinel, released, seen = object(), [], {}
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda *a, **k: sentinel)
    monkeypatch.setattr(hermes_state_registry, "release_or_close", released.append)
    monkeypatch.setattr(dt, "build_recovered_delegation_runner",
                        lambda record, continuation, parent, source: lambda: seen.update(db=parent._session_db) or {})

    runner, _interrupt, parent = dd.build_parent_and_runner(
        _record("deleg_db", owner=DEAD_BOOT), None, {}, resolve_default=dict,
        resolve_for_provider=lambda _p: {"provider": "test-provider"})
    runner()

    assert seen["db"] is sentinel, "children built from this parent would run unpersisted"
    assert released == [sentinel] and parent._session_db is None


# ── b70baea72b68 / 537d46fef85b / 3b462f1e3bca: inherited context ──────────
def test_explicit_inherit_mode_persists_the_childs_inherited_context(monkeypatch):
    import types

    from gateway import status as gateway_status
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import delegate_tool as dt

    monkeypatch.setattr(gateway_status, "get_current_boot_id", lambda: "boot-a:1.0")
    fold = [{"role": "user", "content": "[inherited] deploy target is ace-media"}]
    child = types.SimpleNamespace(prefill_messages=fold, provider="p", model="m")
    tokens = set_session_vars(platform="telegram", source="gateway", chat_id="123",
                              session_key="agent:main:telegram:dm:123", session_id="parent-1", profile="default")
    try:
        spec, _boot = dt._build_durable_background_spec(
            task_list=[{"goal": "Deploy to the target named in our conversation", "inherit_mode": "summary"}],
            shared_context=None, top_role="leaf", inherit_context=None, cfg={}, creds={"model": "m"},
            parent_agent=types.SimpleNamespace(), session_key="agent:main:telegram:dm:123",
            parent_session_id="parent-1", origin_ui_session_id="", max_iterations=10, children=[child])
    finally:
        clear_session_vars(tokens)

    assert spec["source"]["tasks"][0].get("materialized_prefill_messages") == fold


# ── 5781695a19e8: age-capped orphan whose kill was never sent ──────────────
def test_an_age_capped_orphan_is_killed_by_the_next_boot_too(_isolated, tmp_path):
    record = _record("deleg_old", owner=DEAD_BOOT, created_at=time.time() - 30 * 86400)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 60)
    ident = {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid)}
    record["detached"] = {**ident, "job_dir": str(job_dir), "attempt_id": record["attempt"]["attempt_id"],
                          "spawned_at": time.time()}
    _write(record)
    kw = dict(profile_home=tmp_path, probe=dd.probe_job, identity=dd.job_identity, job_dir_for=dd.job_dir_for)

    _, first = ad._store.claim_detached_adoptions(current_boot_id="boot-b:1.0", **kw)
    # boot B dies after the registry commit, before it signals ``first``
    _, second = ad._store.claim_detached_adoptions(current_boot_id="boot-c:1.0", **kw)

    assert first == [ident]
    assert second == [ident], "the next boot no longer knows about the still-running orphan"
    assert _load("deleg_old")["state"] == "failed"


# ── 4c4528618a53: process checkpoints ──────────────────────────────────────
def test_the_detached_worker_never_writes_the_gateways_process_checkpoint(tmp_path, monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "CHECKPOINT_PATH", pr.CHECKPOINT_PATH)  # restored after the test
    gateway_checkpoint = pr._checkpoint_path()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "spec.json").write_text(json.dumps({"schema_version": -1}))  # child exits right after bootstrap
    monkeypatch.setattr(dd, "HEARTBEAT_SECONDS", 3600.0)

    assert dd._child_main(job_dir) == 1

    assert pr._checkpoint_path() == job_dir / "processes.json"
    assert pr._checkpoint_path() != gateway_checkpoint


# ── c5a334c727d0: Windows has no process-group cancel ──────────────────────
@pytest.mark.platforms("windows")
def test_windows_runs_detached_units_in_process(tmp_path):
    record = _record("deleg_win", owner="boot-a:1.0")

    assert dd.spawn_mode() == "degraded"
    assert _REAL_SPAWN_CHILD(record=record, attempt_id=record["attempt"]["attempt_id"], profile_home=tmp_path,
                             continuation=None, live_delegation_id=None) is None


# ── #2160 Prism round 1 (t_7ccdc21b) ───────────────────────────────────────
def _background_spec_for(monkeypatch, tmp_path, **call) -> dict:
    """Run delegate_task(background=True) in a routable gateway session up to the dispatch and return
    the durable spec it persisted (children are stand-ins; nothing runs)."""
    import types

    from gateway import status as gateway_status
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import delegate_tool as dt

    captured: dict = {}
    monkeypatch.setattr(gateway_status, "get_current_boot_id", lambda: "boot-a:1.0")
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools",
                        lambda **kw: types.SimpleNamespace(_subagent_id=None, provider="p", model="m",
                                                           prefill_messages=None))
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch",
                        lambda **kw: captured.update(kw) or {"status": "dispatched", "delegation_id": "d1"})
    parent = types.SimpleNamespace(model="m", provider="p", base_url=None, api_mode=None, session_id="parent-1",
                                   enabled_toolsets=["file"], _delegate_depth=0, terminal_cwd=str(tmp_path))
    tokens = set_session_vars(platform="telegram", source="gateway", chat_id="123", async_delivery=True,
                              session_key="agent:main:telegram:dm:123", session_id="parent-1", profile="default")
    try:
        out = json.loads(dt.delegate_task(background=True, parent_agent=parent, **call))
    finally:
        clear_session_vars(tokens)
    assert out.get("status") == "dispatched", out
    return captured["durable_spec"]


# 234cda575be4 / 43bfa6936050: call-level inputs reach the reconstructed worker
def test_call_level_images_and_skills_are_persisted_into_the_detached_task(monkeypatch, tmp_path):
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    spec = _background_spec_for(monkeypatch, tmp_path, tasks=[{"goal": "Describe the attached diagram"}],
                                images=[str(image)], skills=["diagram-reading"])

    task = spec["source"]["tasks"][0]
    assert task.get("images") == [str(image)], "the detached worker would run without the image"
    assert task.get("skills") == ["diagram-reading"], "the detached worker would run without the skills"


# 689f06f8643e: a session-isolated sandbox stays in-process
def test_a_session_isolated_sandbox_keeps_the_unit_in_process(monkeypatch, tmp_path):
    from tools import delegate_tool as dt

    monkeypatch.setattr(dt, "_get_detached_background", lambda: True)
    monkeypatch.setattr("tools.delegate_detached.spawn_mode", lambda: "in_process")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")

    spec = _background_spec_for(monkeypatch, tmp_path, tasks=[{"goal": "Read /workspace/session-only.txt"}])

    assert not spec["execution"].get("detached"), "the child would alias 'default', not the session's container"


# ca62d9c4cab7 / be9a89e1a7e1: no handoff that cannot cross the process boundary
def test_the_detached_worker_refuses_a_process_handoff_it_cannot_deliver(tmp_path, monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "CHECKPOINT_PATH", pr.CHECKPOINT_PATH)
    monkeypatch.setattr(pr, "HANDOFF_UNAVAILABLE", getattr(pr, "HANDOFF_UNAVAILABLE", None), raising=False)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "spec.json").write_text(json.dumps({"schema_version": -1}))
    monkeypatch.setattr(dd, "HEARTBEAT_SECONDS", 3600.0)
    assert dd._child_main(job_dir) == 1

    import weakref

    from tools.delegate_tool import _register_subagent, _unregister_subagent

    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", False)
    class _Agent:
        pass

    parent = _Agent()
    parent.session_id, parent._current_task_id = "reconstructed-parent", "parent-turn"
    child = _Agent()
    child._delegate_parent_ref = weakref.ref(parent)
    sid = "sa-0-detachedhandoff"
    _register_subagent({"subagent_id": sid, "parent_id": None, "depth": 0, "goal": "g", "model": "m",
                        "started_at": time.time(), "status": "running", "tool_count": 0, "agent": child})
    proc = pr.process_registry.spawn_local("sleep 30", task_id=sid, owner_task_id=sid)
    try:
        out = json.loads(pr._handle_process({"action": "handoff", "session_id": proc.id, "data": "collect it"},
                                            task_id=sid))
        assert out.get("status") != "handed_off", "a handoff the gateway can never receive was reported done"
        assert "detached" in out.get("error", "")
        assert proc.owner_task_id == sid, "ownership moved to a parent that exits with this worker"
    finally:
        pr.process_registry.kill_process(proc.id)
        _unregister_subagent(sid)


# 65089d8d2a59: the worker runs PM's dependency boot (lease + activate) before its imports
def test_spawned_worker_is_marked_to_boot_pm_dependencies(_isolated, tmp_path, monkeypatch):
    seen: dict = {}
    real_popen = subprocess.Popen

    def _popen(argv, **kwargs):
        seen["env"] = kwargs["env"]
        proc = real_popen([sys.executable, "-c", "pass"])
        _isolated.append(proc)
        return proc

    monkeypatch.setattr(dd.subprocess, "Popen", _popen)
    record = _record("deleg_pm", owner="boot-a:1.0")
    _REAL_SPAWN_CHILD(record=record, attempt_id=record["attempt"]["attempt_id"], profile_home=tmp_path,
                      continuation=None, live_delegation_id=None)
    assert seen["env"].get(dd.WORKER_BOOT_MARKER) == "1"

    booted: list = []
    import pm.environments
    monkeypatch.setattr(pm.environments, "activate_dependencies", booted.append)
    monkeypatch.setattr(dd, "_child_main", lambda job_dir: booted.append(("child_main", job_dir)) or 0)
    monkeypatch.setenv(dd.WORKER_BOOT_MARKER, "1")

    assert dd.main(["--job", str(tmp_path / "j")]) == 0
    assert booted[0] == dd._RUNTIME_ROOT and booted[1][0] == "child_main", "deps must activate before the run"
    assert dd.WORKER_BOOT_MARKER not in os.environ, "the worker's own children must not re-boot"


# 50036c4daf58 / 44539ffc4253: a fenced child is resolved before its unit can run again
def test_a_fenced_child_left_alive_blocks_redispatch_until_the_next_boot_kills_it(_isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_detached_enabled", lambda: False)  # the re-dispatch runs the stub in-process
    record = _record("deleg_fenced", owner="boot-a:1.0", state="recoverable")
    attempt_id = record["attempt"]["attempt_id"]
    job_dir = dd.job_dir_for(tmp_path, attempt_id)
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 60)
    block = {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid), "job_dir": str(job_dir),
             "attempt_id": attempt_id, "spawned_at": time.time()}
    record["detached"] = block
    _write(record)
    # Boot A fences its un-advertised child, then dies before terminate_group.
    assert ad._store.stand_down_detached("deleg_fenced", attempt_id, "boot-a:1.0", profile_home=tmp_path) is True

    claimed, _ = ad._store.claim_recoveries(current_boot_id="boot-b:1.0", resume_enabled=True,
                                            profile_home=tmp_path, owner_alive=lambda b: False)
    assert claimed == [], "generation 1 would run beside the still-live generation 0"

    redispatched: list = []
    ad.recover_async_delegations(current_boot_id="boot-b:1.0", profile_home=tmp_path,
                                 runner_factory=lambda rec, cont: redispatched.append(rec) or (lambda: {}))
    assert proc.wait(timeout=10) == -signal.SIGTERM, "the fenced child was never killed"
    assert [r["attempt"]["generation"] for r in redispatched] == [1], "re-dispatch only after it is dead"
    assert "detached_fenced" not in _load("deleg_fenced")
    _wait_for_event("deleg_fenced")  # the re-dispatched stub finishes inside this test


# 0863b09af7bd: a failed orphan's identity survives every boot that dies before signalling
def test_a_failed_orphan_is_rekilled_by_every_boot_until_it_is_dead(_isolated, tmp_path):
    record = _record("deleg_old2", owner=DEAD_BOOT, created_at=time.time() - 30 * 86400)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 60)
    ident = {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid)}
    record["detached"] = {**ident, "job_dir": str(job_dir), "attempt_id": record["attempt"]["attempt_id"],
                          "spawned_at": time.time()}
    _write(record)
    kw = dict(profile_home=tmp_path, probe=dd.probe_job, identity=dd.job_identity, job_dir_for=dd.job_dir_for)

    _, b = ad._store.claim_detached_adoptions(current_boot_id="boot-b:1.0", **kw)  # dies before signalling
    _, c = ad._store.claim_detached_adoptions(current_boot_id="boot-c:1.0", **kw)  # dies before signalling
    _, d = ad._store.claim_detached_adoptions(current_boot_id="boot-d:1.0", **kw)

    assert b == c == d == [ident], "a later boot lost the identity of a still-running orphan"
    proc.kill()
    proc.wait(timeout=5)
    _, e = ad._store.claim_detached_adoptions(current_boot_id="boot-e:1.0", **kw)
    assert e == [] and "detached" not in _load("deleg_old2"), "a confirmed-dead orphan is released"


# 33b3f07f6558: one file's read-modify-write is serialized across processes
def test_patch_locks_serialize_the_same_file_across_processes(tmp_path):
    target = tmp_path / "shared.txt"
    hold = tmp_path / "held"
    code = (
        "import sys,time,pathlib\n"
        "from tools import file_state\n"
        "with file_state.lock_path(sys.argv[1]):\n"
        "    pathlib.Path(sys.argv[2]).write_text('1')\n"
        "    time.sleep(3)\n")
    env = {**os.environ, "PYTHONPATH": str(Path(dd.__file__).resolve().parent.parent)}
    holder = subprocess.Popen([sys.executable, "-c", code, str(target), str(hold)], env=env)
    try:
        deadline = time.time() + 20
        while not hold.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert hold.exists()
        from tools import file_state

        started = time.monotonic()
        with file_state.lock_path(str(target)):
            waited = time.monotonic() - started
        assert holder.poll() is not None or waited >= 2.0, "a second process entered the same file's lock"
    finally:
        holder.wait(timeout=20)


# ── #2160 post-merge Prism (t_f69309df): an identity lookup that FAILED is unknown, never dead ──
def _psutil_unavailable(monkeypatch):
    """Process inspection fails transiently (``import psutil`` raises), as on a starved or sandboxed host."""
    monkeypatch.setitem(sys.modules, "psutil", None)


# 88eb7f2543d5: a start time unknown at spawn must not make the child uncancellable
def test_a_child_spawned_without_a_start_time_is_still_cancellable(_isolated, tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    proc = _sleeper(_isolated, 60)
    with ad._records_lock:
        ad._records["deleg_ct"] = {"delegation_id": "deleg_ct", "status": "running"}
    # spawn_child's process_create_time() failed: the supervision hook is installed with create_time=None.
    assert ad._install_detached_supervision("deleg_ct", proc.pid, None, job_dir) is True
    # The live child then publishes its own verified identity (what _child_main writes first).
    (job_dir / "child.json").write_text(json.dumps({"pid": proc.pid, "create_time": dd.process_create_time(proc.pid)}))

    with ad._records_lock:
        interrupt = ad._records["deleg_ct"]["interrupt_fn"]
    interrupt()

    assert proc.wait(timeout=10) == -signal.SIGTERM, "cancel was a silent no-op on a verifiable live child"


def test_an_unverifiable_child_is_still_never_signalled_by_cancel(_isolated, tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    proc = _sleeper(_isolated, 60)  # its argv does not name the job: a recycled pid looks exactly like this
    with ad._records_lock:
        ad._records["deleg_ct2"] = {"delegation_id": "deleg_ct2", "status": "running"}
    assert ad._install_detached_supervision("deleg_ct2", proc.pid, None, job_dir) is True
    with ad._records_lock:
        ad._records["deleg_ct2"]["interrupt_fn"]()
    time.sleep(0.3)
    assert proc.poll() is None, "the PID-reuse guard was weakened"


# 2027aadba6a8: an unfenced orphan whose identity cannot be read keeps its dir + marker for a retry
def test_an_unfenced_orphan_with_an_unreadable_identity_is_kept_for_the_next_pass(_isolated, tmp_path, monkeypatch):
    _write()  # a registry exists; no live record owns the orphan's job dir
    job_dir = dd.job_dir_for(tmp_path, "deleg_gone:g0:a")
    job_dir.mkdir(parents=True)
    (job_dir / "spawn.json").write_text("{}")
    dd.mark_stand_down_unknown(job_dir)
    proc = _sleeper(_isolated, 60, extra_argv=("--job", str(job_dir)))
    terminated: list = []
    real_terminate = dd.terminate_group
    monkeypatch.setattr(dd, "terminate_group", lambda *a, **k: terminated.append(a) or real_terminate(*a, **k))

    with pytest.MonkeyPatch.context() as mp:
        _psutil_unavailable(mp)
        ad.recover_async_delegations(current_boot_id="boot-b:1.0", profile_home=tmp_path,
                                     runner_factory=lambda *a: pytest.fail("an orphan is never re-run"))
    assert job_dir.exists() and dd.stand_down_unknown_dirs(tmp_path) == [job_dir], (
        "the orphan's dir and marker were removed while its child may be alive")
    assert proc.poll() is None and terminated == []

    ad.recover_async_delegations(current_boot_id="boot-c:1.0", profile_home=tmp_path,
                                 runner_factory=lambda *a: pytest.fail("an orphan is never re-run"))
    assert proc.wait(timeout=10) == -signal.SIGTERM, "the retry pass did not kill the orphan"
    assert not job_dir.exists()


# Same class: an adoption pass that cannot inspect processes must not call a live child dead
def test_a_detached_child_is_not_lost_when_process_inspection_fails(_isolated, tmp_path, monkeypatch):
    record = _record("deleg_insp", owner=DEAD_BOOT)
    job_dir = dd.job_dir_for(tmp_path, record["attempt"]["attempt_id"])
    job_dir.mkdir(parents=True)
    proc = _sleeper(_isolated, 60)
    block = {"pid": proc.pid, "create_time": dd.process_create_time(proc.pid), "job_dir": str(job_dir),
             "attempt_id": record["attempt"]["attempt_id"], "spawned_at": time.time()}
    record["detached"] = block
    _write(record)
    kw = dict(profile_home=tmp_path, probe=dd.probe_job, identity=dd.job_identity, job_dir_for=dd.job_dir_for)

    with pytest.MonkeyPatch.context() as mp:
        _psutil_unavailable(mp)
        adopted, kill = ad._store.claim_detached_adoptions(current_boot_id="boot-b:1.0", **kw)
        claimed, _ = ad._store.claim_recoveries(current_boot_id="boot-b:1.0", resume_enabled=True,
                                                profile_home=tmp_path, owner_alive=lambda b: False)
    assert (adopted, kill, claimed) == ([], [], []), "a second generation would run beside a live child"
    assert _load("deleg_insp")["detached"]["pid"] == proc.pid

    adopted, _ = ad._store.claim_detached_adoptions(current_boot_id="boot-c:1.0", **kw)
    assert [r["delegation_id"] for r in adopted] == ["deleg_insp"], "the healthy retry pass adopts it"


def test_an_exit_is_never_confirmed_from_a_failed_inspection(_isolated, monkeypatch):
    proc = _sleeper(_isolated, 60)
    ct = dd.process_create_time(proc.pid)
    _psutil_unavailable(monkeypatch)
    assert dd.wait_for_exit(proc.pid, ct, timeout=0.3) is False
    assert dd.wait_for_exit(proc.pid, None, timeout=0.3) is False
