# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""``write_runtime_status`` must not run on the gateway event loop.

``gateway/status.py::write_runtime_status`` does a read-merge-atomic-write of
``gateway_state.json`` plus a realpath walk and a psutil ``create_time`` call --
measured median 0.903ms / max 7.666ms on an IDLE box with a small file.  Twelve
coroutines in ``gateway/run.py`` and ``gateway/slash_commands.py`` reached it
(startup, drain, shutdown, the reconnect watcher, the scale-to-zero watcher, the
per-turn ``track_agent`` and ``_run_agent_inner``), all frozen in
``REACHABLE_BASELINE`` by #782.

The offload lives INSIDE ``_update_runtime_status`` /
``_update_platform_runtime_status`` (mirroring ``_persist_active_agents``, the
fix #773 shipped for the same sink) rather than at the ~28 call sites: the
helper names are a spy surface a dozen existing tests monkeypatch, so making the
callers await an async variant breaks them for no behavioural gain.

These are BEHAVIOURAL tests: they make the write genuinely slow, or genuinely
concurrent, and assert on observed behaviour rather than on the source.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(exist_ok=True)
    from gateway import status as gwstatus

    gwstatus._reset_identity_caches()
    return tmp_path


def _make_runner():
    import gateway.run as run

    runner = object.__new__(run.GatewayRunner)
    runner._restart_requested = False
    runner._running_agents = {}
    runner._running_agent_tasks = {}
    return runner


# ---------------------------------------------------------------------------
# The loop-stall property (the actual defect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_runtime_status_runs_off_the_loop_thread(sandbox_home):
    runner = _make_runner()
    from gateway import status as gwstatus

    seen: dict = {}
    done = threading.Event()
    real = gwstatus.write_runtime_status

    def spy(**kwargs):
        seen["thread"] = threading.current_thread()
        done.set()

    gwstatus.write_runtime_status = spy  # type: ignore[assignment]
    try:
        seen["loop_thread"] = threading.current_thread()
        runner._update_runtime_status("running")
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert done.is_set(), "the status write never happened"
    assert seen["thread"] is not seen["loop_thread"], (
        "gateway_state.json was written ON the event loop thread"
    )


@pytest.mark.asyncio
async def test_update_runtime_status_keeps_the_loop_responsive(sandbox_home):
    """A SLOW status write must not stall a concurrent coroutine.

    This is the assertion that fails without the fix: the blocking form lets
    the heartbeat tick ~0 times across the write.
    """
    runner = _make_runner()
    from gateway import status as gwstatus

    done = threading.Event()
    real = gwstatus.write_runtime_status

    def slow(**kwargs):
        time.sleep(0.5)
        done.set()

    gwstatus.write_runtime_status = slow  # type: ignore[assignment]

    ticks = 0
    stop = False

    async def heartbeat():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0.05)
        before = ticks
        runner._update_runtime_status("running")
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)
        after = ticks
    finally:
        stop = True
        await hb
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert done.is_set(), "the status write never happened"
    assert after - before > 10, (
        f"loop stalled during the status write: only {after - before} heartbeat "
        "ticks elapsed across a 0.5s write"
    )


@pytest.mark.asyncio
async def test_update_platform_runtime_status_runs_off_the_loop_thread(sandbox_home):
    runner = _make_runner()
    from gateway import status as gwstatus

    seen: dict = {}
    done = threading.Event()
    real = gwstatus.write_runtime_status

    def spy(**kwargs):
        seen["thread"] = threading.current_thread()
        done.set()

    gwstatus.write_runtime_status = spy  # type: ignore[assignment]
    try:
        seen["loop_thread"] = threading.current_thread()
        runner._update_platform_runtime_status("telegram", platform_state="connected")
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert done.is_set()
    assert seen["thread"] is not seen["loop_thread"]


# ---------------------------------------------------------------------------
# The two properties the offload must NOT break
# ---------------------------------------------------------------------------


def test_sync_callers_still_write_inline(sandbox_home):
    """``_enter_external_drain`` / ``_maybe_update_status`` and friends run with
    no loop; the offload is loop-conditional, so they must still write inline."""
    runner = _make_runner()
    from gateway import status as gwstatus

    seen: dict = {}
    real = gwstatus.write_runtime_status

    def spy(**kwargs):
        seen["thread"] = threading.current_thread()
        seen["kwargs"] = kwargs

    gwstatus.write_runtime_status = spy  # type: ignore[assignment]
    try:
        runner._update_runtime_status("draining")
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert seen["kwargs"]["gateway_state"] == "draining"
    assert seen["thread"] is threading.main_thread(), (
        "a no-loop caller must write inline, not on an executor"
    )


@pytest.mark.asyncio
async def test_terminal_states_are_written_inline_not_offloaded(sandbox_home):
    """``stopped`` / ``startup_failed`` must land BEFORE the process can exit.

    A detached executor job can be dropped at interpreter shutdown, which would
    silently lose the final gateway_state -- the exact thing
    ``test_signal_initiated_shutdown_persists_running_not_stopped`` guards.
    """
    runner = _make_runner()
    from gateway import status as gwstatus

    seen: dict = {}
    real = gwstatus.write_runtime_status

    def spy(**kwargs):
        seen.setdefault("threads", []).append(threading.current_thread())
        seen.setdefault("states", []).append(kwargs.get("gateway_state"))

    gwstatus.write_runtime_status = spy  # type: ignore[assignment]
    try:
        loop_thread = threading.current_thread()
        runner._update_runtime_status("stopped", "sigterm")
        # No awaiting: it must ALREADY have happened, synchronously.
        assert seen.get("states") == ["stopped"], (
            "a terminal status write was deferred to an executor"
        )
        assert seen["threads"][0] is loop_thread
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_terminal_write_cannot_be_overtaken_by_older_deferred_write(
    sandbox_home, monkeypatch,
):
    """A queued ``running`` write must never land after terminal ``stopped``."""
    runner = _make_runner()
    loop = asyncio.get_running_loop()
    queued = []

    def hold_executor_callback(executor, callback):
        queued.append(callback)
        return loop.create_future()

    monkeypatch.setattr(loop, "run_in_executor", hold_executor_callback)
    runner._update_runtime_status("running")
    runner._update_runtime_status("stopped", "sigterm")
    # Replaying callbacks held by the old default-executor shape simulates the
    # delayed ``running`` write that used to overtake terminal state.
    for callback in queued:
        callback()

    from gateway import status as gwstatus

    record = gwstatus.read_runtime_status()
    assert record is not None
    assert record["gateway_state"] == "stopped"
    assert record["exit_reason"] == "sigterm"


# ---------------------------------------------------------------------------
# Concurrency: going off-loop makes the read-merge-write genuinely racy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_write_is_a_merge_so_concurrency_must_not_drop_fields(
    sandbox_home,
):
    """Every caller passes a SUBSET of fields and relies on the on-disk payload
    for the rest. Two unserialized public writers each read the pre-merge file
    and the loser's field vanishes. The public API itself must serialize so a
    direct caller cannot race the gateway's ordered lane."""
    from gateway import status as gwstatus

    real = gwstatus._write_json_file

    def slow(path, payload):
        # Widen the read-modify-write window so an unserialized race loses.
        time.sleep(0.05)
        return real(path, payload)

    gwstatus._write_json_file = slow  # type: ignore[assignment]
    try:
        await asyncio.gather(
            asyncio.to_thread(
                gwstatus.write_runtime_status, gateway_state="running",
            ),
            asyncio.to_thread(
                gwstatus.write_runtime_status,
                platform="telegram", platform_state="connected",
            ),
            asyncio.to_thread(
                gwstatus.write_runtime_status, exit_reason="none",
            ),
        )
    finally:
        gwstatus._write_json_file = real  # type: ignore[assignment]

    record = gwstatus.read_runtime_status()
    assert record is not None
    assert record.get("gateway_state") == "running", "gateway_state was clobbered"
    assert record.get("exit_reason") == "none", "exit_reason was clobbered"
    assert (
        record.get("platforms", {}).get("telegram", {}).get("state") == "connected"
    ), "the platform entry was clobbered"


def test_offloading_does_not_change_the_persisted_payload(sandbox_home):
    """Going off-loop must not change WHAT gets written."""
    from gateway import status as gwstatus

    gwstatus.write_runtime_status_locked(
        gateway_state="draining", exit_reason="restart", active_agents=3,
    )
    record = gwstatus.read_runtime_status()
    assert record is not None
    assert record["gateway_state"] == "draining"
    assert record["exit_reason"] == "restart"
    assert record["active_agents"] == 3
    assert record["schema_version"] == 2
    assert record["pid"] and record["boot_id"]
