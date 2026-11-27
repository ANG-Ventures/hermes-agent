"""Real inbound boundary with an isolated registry and deterministic I/O barriers."""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_cli import active_sessions
from tests.gateway.test_max_concurrent_sessions import (
    _make_event, _make_runner, _silence_global_gateway_hooks,
)


@pytest.mark.asyncio
async def test_registry_write_does_not_block_inbound_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(1)
    runner._persist_active_agents = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="ok")
    runner._post_turn_hooks_after_delivery = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = active_sessions._write_entries

    def slow_write(*args):
        # Fails immediately on base, rather than deadlocking the event loop.
        assert threading.get_ident() != loop_thread, "registry I/O on event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release registry barrier"
        return real_write(*args)

    monkeypatch.setattr(active_sessions, "_write_entries", slow_write)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        heartbeat = asyncio.Event()
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 2)
    finally:
        release.set()
        await turn
        await runner._drain_active_session_releases()
    assert not active_sessions._read_entries(tmp_path / "runtime" / "active_sessions.json")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(1)
    runner._persist_active_agents = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="ok")
    runner._post_turn_hooks_after_delivery = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    return runner, tmp_path / "runtime" / "active_sessions.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_claim_owns_slot_and_cancellation_releases(rig, monkeypatch, cancel):
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = active_sessions._write_entries
    releases = []
    real_release = active_sessions.ActiveSessionLease.release

    def release(lease):
        releases.append(lease.lease_id)
        return real_release(lease)

    def write(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return real_write(*args)

    monkeypatch.setattr(active_sessions, "_write_entries", write)
    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", release)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Different chat cannot overtake the locally reserved pending slot.
        rejected = await runner._handle_message(_make_event(chat_id="other"))
        assert "(1/1)" in rejected
        assert runner._handle_message_with_agent.await_count == 0
        if cancel:
            turn.cancel()
            await asyncio.sleep(0)
            turn.cancel()  # repeated cancellation must not abandon the worker
    finally:
        proceed.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await turn
    else:
        assert await turn == "ok"
    await runner._drain_active_session_releases()
    assert len(releases) == 1
    assert not active_sessions._read_entries(path)
    assert not runner._running_agents
    assert not runner._active_session_claim_tasks
    assert not runner._active_session_claim_locks


@pytest.mark.asyncio
async def test_cancel_before_dispatch_never_acquires(rig, monkeypatch):
    runner, path = rig
    acquire = MagicMock(wraps=active_sessions.try_acquire_active_session)
    monkeypatch.setattr(active_sessions, "try_acquire_active_session", acquire)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    acquire.assert_not_called()
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_exception_and_duplicate_finalizer_release_once(rig, monkeypatch):
    from gateway.session import build_session_key
    runner, path = rig
    releases = []
    real_release = active_sessions.ActiveSessionLease.release

    def release(lease):
        releases.append(lease.lease_id)
        return real_release(lease)

    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", release)
    runner._handle_message_with_agent.side_effect = RuntimeError("turn failed")
    event = _make_event()
    with pytest.raises(RuntimeError, match="turn failed"):
        await runner._handle_message(event)
    runner._release_running_agent_state(build_session_key(event.source))
    await runner._drain_active_session_releases()
    assert len(releases) == 1
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_cross_runner_claim_honors_registry_cap(rig):
    runner, path = rig
    other = _make_runner(1)
    a, b = await asyncio.gather(
        runner._claim_active_session_slot_off_loop("a", _make_event().source),
        other._claim_active_session_slot_off_loop("b", _make_event().source),
    )
    winners = [lease for lease, _ in (a, b) if lease is not None]
    assert len(winners) == 1
    assert len(active_sessions._read_entries(path)) == 1
    assert sum(message is not None for _, message in (a, b)) == 1
    await runner._release_active_session_lease(winners[0])
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_superseded_acquisition_never_clears_successor(rig, monkeypatch):
    from gateway.session import build_session_key
    runner, path = rig
    event = _make_event()
    key = build_session_key(event.source)
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = runner._claim_active_session_slot

    def delayed(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return acquire(*args)

    monkeypatch.setattr(runner, "_claim_active_session_slot", delayed)
    turn = asyncio.create_task(runner._handle_message(event))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runner._invalidate_session_run_generation(key, reason="test supersession")
        successor = object()
        runner._session_state(key).turn.agent = successor
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert runner._session_state(key).turn.agent is successor
    runner._handle_message_with_agent.assert_not_awaited()
    await runner._drain_active_session_releases()
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_release_barrier_survives_cancellation_and_is_idempotent(rig, monkeypatch):
    runner, path = rig
    lease, _ = await runner._claim_active_session_slot_off_loop("a", _make_event().source)
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    real_release = lease.release
    calls = []

    def release():
        calls.append(1)
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        real_release()

    monkeypatch.setattr(lease, "release", release)
    task = runner._release_active_session_lease(lease)
    assert runner._release_active_session_lease(lease) is task
    waiter = asyncio.create_task(runner._wait_active_session_release(task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await runner._drain_active_session_releases()
    assert calls == [1]
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_shutdown_joins_pending_acquisition_and_cleanup(rig, monkeypatch):
    from types import SimpleNamespace
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = runner._claim_active_session_slot

    def delayed(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return acquire(*args)

    class AfterLeaseDrain(Exception):
        pass

    def after_drain():
        assert not active_sessions._read_entries(path)
        raise AfterLeaseDrain

    monkeypatch.setattr(runner, "_claim_active_session_slot", delayed)
    runner._active_api_run_count = after_drain
    runner._restart_task = None
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop = asyncio.create_task(runner._stop_release_runtime_state(SimpleNamespace()))
        await asyncio.sleep(0)
        assert not stop.done()
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    with pytest.raises(AfterLeaseDrain):
        await stop
    assert not runner._active_session_release_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_error", [False, True])
async def test_release_error_does_not_replace_turn_result(rig, monkeypatch, caplog, turn_error):
    runner, path = rig
    def fail_release(lease):
        raise OSError("release disk failure")
    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", fail_release)
    if turn_error:
        runner._handle_message_with_agent.side_effect = ValueError("original turn failure")
        with pytest.raises(ValueError, match="original turn failure"):
            await runner._handle_message(_make_event())
    else:
        assert await runner._handle_message(_make_event()) == "ok"
    await runner._drain_active_session_releases()
    assert "Failed to release active session slot" in caplog.text
    # Failed durable cleanup is observable, not falsely reported as removed.
    assert len(active_sessions._read_entries(path)) == 1


@pytest.mark.asyncio
async def test_shutdown_release_failure_does_not_abort_remaining_cleanup(rig, monkeypatch):
    from types import SimpleNamespace
    runner, _ = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    lease, _ = await runner._claim_active_session_slot_off_loop("a", _make_event().source)
    def fail():
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        raise OSError("release disk failure")
    monkeypatch.setattr(lease, "release", fail)
    runner._release_active_session_lease(lease)
    await asyncio.wait_for(entered.wait(), 2)
    class AfterDrain(Exception):
        pass
    def after_drain():
        raise AfterDrain
    runner._active_api_run_count = after_drain
    runner._restart_task = None
    stop = asyncio.create_task(runner._stop_release_runtime_state(SimpleNamespace()))
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(AfterDrain):
        await stop


@pytest.mark.asyncio
async def test_registry_refusal_does_not_remove_orphan_fifo_head(rig):
    from gateway.session import build_session_key
    runner, _ = rig
    event = _make_event(text="new")
    key = build_session_key(event.source)
    old = _make_event(text="old")
    runner._session_state(key).conversation.queued_events = [old]
    runner._claim_active_session_slot = MagicMock(return_value=(None, "registry full"))
    assert await runner._handle_message(event) == "registry full"
    assert runner._overflow_queue(key) == [old]
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_spools_pending_claim_before_cancelling_cleanup(rig, monkeypatch):
    from types import SimpleNamespace
    from gateway.fork_ext.restart_followups import take_followups
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = runner._claim_active_session_slot
    def delayed(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return acquire(*args)
    monkeypatch.setattr(runner, "_claim_active_session_slot", delayed)
    runner._restart_task = None
    runner._status_action_label = lambda: "restart"
    class AfterDrain(Exception):
        pass
    def after_drain():
        raise AfterDrain
    runner._active_api_run_count = after_drain
    turn = asyncio.create_task(runner._handle_message(_make_event(text="pending input")))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop = asyncio.create_task(runner._stop_release_runtime_state(SimpleNamespace()))
        # Spool must happen without requiring the registry worker to return.
        for _ in range(200):
            rows, _ = await asyncio.to_thread(take_followups)
            if rows:
                break
            await asyncio.sleep(0.01)
        assert rows, "pending acquisition input was not spooled"
        assert rows[0]["text"] == "pending input"
    finally:
        proceed.set()
        await asyncio.gather(turn, *( [stop] if stop else []), return_exceptions=True)
    runner._handle_message_with_agent.assert_not_awaited()
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_completed_reply_handoff_precedes_held_release_and_shutdown(rig, monkeypatch):
    from types import SimpleNamespace
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    delivered = asyncio.Event()
    replies = []
    loop = asyncio.get_running_loop()
    real_release = active_sessions.ActiveSessionLease.release

    def held_release(lease):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(10)
        real_release(lease)

    async def adapter_handoff():
        replies.append(await runner._handle_message(_make_event()))
        delivered.set()

    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", held_release)
    turn = asyncio.create_task(adapter_handoff())
    runner._background_tasks.add(turn)
    runner._restart_task = None
    runner._active_api_run_count = lambda: 0
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Shutdown cancels ordinary background work while durable release is held.
        stop = asyncio.create_task(runner._stop_release_runtime_state(SimpleNamespace()))
        await asyncio.wait_for(delivered.wait(), 2)
        assert replies == ["ok"]
        assert not turn.cancelled()
        assert len(active_sessions._read_entries(path)) == 1
        assert runner._active_session_release_tasks
    finally:
        proceed.set()
        await asyncio.gather(turn, *([stop] if stop else []), return_exceptions=True)
        await runner._drain_active_session_releases()
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_failed_pending_spool_retains_original_after_owner_cleanup(rig, monkeypatch, recover):
    from gateway.session import build_session_key
    from gateway.fork_ext.restart_followups import take_followups
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = runner._claim_active_session_slot
    event = _make_event(text="retry original input")
    key = build_session_key(event.source)
    real_preserve = runner._preserve_followup_across_restart
    runner._status_action_label = lambda: "restart"

    def delayed(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(10)
        return acquire(*args)

    monkeypatch.setattr(runner, "_claim_active_session_slot", delayed)
    runner._preserve_followup_across_restart = AsyncMock(return_value=False)
    turn = asyncio.create_task(runner._handle_message(event))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runner._preserve_pending_session_claims()
    finally:
        proceed.set()
        await asyncio.gather(turn, return_exceptions=True)
    assert not runner._active_session_claim_tasks
    assert not runner._running_agents
    assert not active_sessions._read_entries(path)
    retained = getattr(runner, "_pending_session_claim_inputs", {})
    assert retained.get(turn) == (key, event), "cancelled input lost with owner state"
    if recover:
        runner._preserve_followup_across_restart = real_preserve
    from types import SimpleNamespace
    class AfterDrain(Exception):
        pass
    def after_drain():
        raise AfterDrain
    runner._restart_task = None
    runner._active_api_run_count = after_drain
    with pytest.raises(AfterDrain):
        await runner._stop_release_runtime_state(SimpleNamespace())
    rows, _ = await asyncio.to_thread(take_followups)
    if recover:
        assert [row["text"] for row in rows] == [event.text]
        assert not retained
        await runner._preserve_pending_session_claims()
        assert (await asyncio.to_thread(take_followups))[0] == rows
    else:
        assert rows == []
        assert retained.get(turn) == (key, event)
        assert not getattr(turn, "_claim_input_spooled", False)
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_registry_waiters_leave_default_executor_available(rig, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    runner, path = rig
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    pool = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(pool)
    entered = asyncio.Event()
    proceed = threading.Event()
    real_write = active_sessions._write_entries
    def held_write(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return real_write(*args)
    monkeypatch.setattr(active_sessions, "_write_entries", held_write)
    tasks = [asyncio.create_task(runner._claim_active_session_slot_off_loop(str(i), _make_event().source))
             for i in range(3)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Registry operations serialize at flock; waiting there must not occupy
        # every shared executor thread needed by shutdown persistence.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "heartbeat"), 1) == "heartbeat"
    finally:
        proceed.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, tuple) and result[0] is not None:
                await runner._release_active_session_lease(result[0])
        loop._default_executor = previous
        pool.shutdown(wait=True)
    assert not active_sessions._read_entries(path)


def _held_io_shutdown_child(home, mode, negative):
    """Disposable process only: the registry barrier is NEVER released."""
    import os
    from pathlib import Path
    from gateway import run_shutdown
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore, build_session_key
    from tests.gateway.restart_test_helpers import make_restart_runner

    os.environ["HERMES_HOME"] = home
    os.environ.pop("PYTEST_CURRENT_TEST", None)  # enable the real thread watchdog

    async def exercise():
        runner, adapter = make_restart_runner()
        runner._restart_task = None
        runner._restart_drain_timeout = 0
        runner._active_session_leases = {}
        runner.session_store = SessionStore(sessions_dir=Path(home) / "sessions", config=runner.config)
        event = _make_event(text="held queue survives")
        key = build_session_key(event.source)
        runner.session_store.get_or_create_session(event.source)
        runner._running_agents[key] = MagicMock()  # real active turn, not pending sentinel
        runner._held_queues = {key: [event]}
        adapter._pending_messages[key] = _make_event(text="adapter queue survives")
        runner._clear_plugin_message_injector = lambda: None
        runner._mark_api_runs_shutdown_requested = lambda: None
        runner._notify_active_sessions_of_shutdown = AsyncMock()
        runner._cancel_pending_boot_resumes_for_shutdown = AsyncMock()
        runner._drain_active_agents = AsyncMock(return_value=([], False))
        runner._active_cron_job_count = lambda: 0
        runner._active_api_run_count = lambda: 0
        runner._finalize_shutdown_agents = AsyncMock()
        runner._cancel_process_completion_batch_tasks = AsyncMock()
        # Only accelerate the leash in this child. Keep stop(), phase ordering,
        # resume persistence, adapter teardown and watchdog hard-exit real.
        run_shutdown._armed_watchdog_delay = lambda *a, **kw: 2.0
        GatewayRunner._rearm_shutdown_watchdog = lambda *a, **kw: None
        if negative == "no-watchdog":
            os.environ["PYTEST_CURRENT_TEST"] = "negative control"
        elif negative == "no-mark":
            GatewayRunner._mark_running_sessions_resume_pending = AsyncMock(return_value=[])
        elif negative == "no-spool":
            runner._preserve_followup_across_restart = AsyncMock(return_value=False)
        release_phase = GatewayRunner._stop_release_runtime_state
        async def observed_release(self, ctx):
            print("RELEASE_PHASE", flush=True)
            await release_phase(self, ctx)
        GatewayRunner._stop_release_runtime_state = observed_release

        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def held_write(*args):
            loop.call_soon_threadsafe(entered.set)
            threading.Event().wait()  # deliberately infinite; parent kills on failure

        if mode == "release":
            lease, _ = await runner._claim_active_session_slot_off_loop(key, event.source)
            assert lease is not None
            active_sessions._write_entries = held_write
            runner._release_active_session_lease(lease)
        else:
            active_sessions._write_entries = held_write
            async def claim_owner():
                await runner._claim_active_session_slot_off_loop(key, event.source)
            owner = asyncio.create_task(claim_owner())
            runner._background_tasks.add(owner)
        await asyncio.wait_for(entered.wait(), 5)
        print("IO_HELD", os.getpid(), flush=True)
        await runner.stop(restart=True)
        raise AssertionError("stop unexpectedly returned with registry I/O held")

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["acquire", "release"])
@pytest.mark.parametrize("negative", ["", "no-watchdog", "no-mark", "no-spool"])
def test_shutdown_watchdog_preserves_resume_and_spool(tmp_path, mode, negative):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore, build_session_key

    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    code = (
        "from tests.gateway.test_active_session_lease_offload import _held_io_shutdown_child; "
        "import sys; _held_io_shutdown_child(*sys.argv[1:])"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), mode, negative],
        cwd=Path(__file__).resolve().parents[2], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as child:
        # Import/setup time is not shutdown time, especially on loaded CI.
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=90):
                child.kill()
                out, err = child.communicate()
                pytest.fail(f"child did not reach registry barrier: {out} {err}")
            ready = child.stdout.readline()
        try:
            out, err = child.communicate(timeout=12)
            timed_out = False
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            timed_out = True
    assert "IO_HELD" in ready, (ready, out, err)
    assert "RELEASE_PHASE" in out, (out, err)
    assert timed_out == (negative == "no-watchdog"), (out, err)
    if timed_out:
        print(f"pid={child.pid} mode={mode} negative=no-watchdog parent-killed-after=12s")
        return
    assert child.returncode == 1, (out, err)
    dump = (tmp_path / "logs/gateway-shutdown-watchdog.log").read_text()
    header = json.loads(dump.splitlines()[0])
    assert header["event"] == "shutdown_watchdog_fired"
    assert header["pid"] == child.pid
    assert header["delay_s"] == 2.0
    assert "_stop_impl" in dump
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.lookup_by_session_key(build_session_key(_make_event().source))
    assert entry.resume_pending == (negative != "no-mark")
    payloads = [p.read_text() for p in (tmp_path / "gateway/restart_followups").glob("*.json")]
    held = any("held queue survives" in p and "queue_held" in p for p in payloads)
    adapter = any("adapter queue survives" in p for p in payloads)
    assert held == adapter == (negative != "no-spool")
    print(f"pid={child.pid} mode={mode} negative={negative or 'none'} exit=1 watchdog=2s resume={entry.resume_pending} held={held} adapter={adapter}")

