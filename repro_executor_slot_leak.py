"""Prove the slot-LEAK class behind the 2026-09-20 boot-resume starvation.

The repository-wide census found three gateway paths that wrap executor
housekeeping in ``asyncio.wait_for(...)`` and proceed after timeout:

  gateway/run.py            _finalize_session_off_loop
  gateway/run.py            _cleanup_agent_resources_off_loop
  gateway/slash_commands.py _handle_reset_command cleanup

``asyncio.wait_for`` bounds the AWAIT. It does not bound the OCCUPANCY: a
``concurrent.futures`` work item that has already begun executing cannot be
cancelled, so the abandoned worker keeps its pool slot for as long as the
blocking call inside it runs. N such abandonments permanently retire N of the
10 slots.

This drives the REAL _cleanup_agent_resources_off_loop against a wedged agent
and measures pool slots still held AFTER every caller has given up waiting.

Exit 0 = housekeeping saturation reproduced and turn-pool isolation verified.
"""
import asyncio
from pathlib import Path
import sys
import threading
import time
import types

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from gateway.run import GatewayRunner  # noqa: E402


def _bare_runner(wedge: threading.Event, entered: threading.Semaphore):
    obj = types.SimpleNamespace()
    obj._executor_lock = threading.Lock()
    obj._executor = None
    obj._housekeeping_executor = None
    obj._executor_closing = False
    # Shorten the real constant so the test is fast; the MECHANISM under test
    # is the abandonment, not the specific budget.
    obj._CLEANUP_TIMEOUT_S = 1.0
    obj._get_executor = types.MethodType(GatewayRunner._get_executor, obj)
    obj._get_housekeeping_executor = types.MethodType(
        GatewayRunner._get_housekeeping_executor, obj
    )
    obj._submit_with_context = types.MethodType(
        GatewayRunner._submit_with_context, obj
    )
    obj._run_in_executor_with_context = types.MethodType(
        GatewayRunner._run_in_executor_with_context, obj
    )
    obj._run_housekeeping_in_executor = types.MethodType(
        GatewayRunner._run_housekeeping_in_executor, obj
    )

    def _wedged_cleanup(agent):
        """Stands in for _cleanup_agent_resources with a wedged provider."""
        entered.release()
        wedge.wait(60)

    obj._cleanup_agent_resources = _wedged_cleanup
    # Bind the REAL off-loop wrapper -- the code under test.
    obj._cleanup_agent_resources_off_loop = types.MethodType(
        GatewayRunner._cleanup_agent_resources_off_loop, obj
    )
    return obj


def _live_pool_threads(prefix="hermes-gateway"):
    return sum(
        1
        for t in threading.enumerate()
        if t.name.startswith(prefix) and t.is_alive()
    )


async def main():
    wedge = threading.Event()
    entered = threading.Semaphore(0)
    runner = _bare_runner(wedge, entered)
    turn_pool = runner._get_executor()
    hk_pool = runner._get_housekeeping_executor()
    max_workers = turn_pool._max_workers
    hk_workers = hk_pool._max_workers
    print(f"turn pool max_workers = {max_workers}")
    print(f"housekeeping pool max_workers = {hk_workers}")
    assert turn_pool is not hk_pool, "pools must be distinct"

    # Abandon MORE cleanups than the housekeeping pool can hold, so the
    # housekeeping pool is not merely full but backlogged. Under the old
    # shared-pool design this retired every turn slot.
    abandoned = max_workers + hk_workers
    t0 = time.monotonic()
    await asyncio.gather(
        *(
            runner._cleanup_agent_resources_off_loop(
                object(), context="session expiry"
            )
            for _ in range(abandoned)
        )
    )
    gave_up_after = time.monotonic() - t0
    for _ in range(hk_workers):
        assert await asyncio.to_thread(entered.acquire, True, 10), "never entered"
    print(
        f"all {abandoned} cleanup callers returned after {gave_up_after:.2f}s "
        "(each logged 'worker thread is left to finish on its own')"
    )

    hk_held = len(hk_pool._threads or ())
    hk_queued = hk_pool._work_queue.qsize()
    turn_held = len(turn_pool._threads or ())
    print(
        f"housekeeping pool: {hk_held}/{hk_workers} workers wedged, "
        f"{hk_queued} queued"
    )
    print(f"turn pool workers occupied by housekeeping: {turn_held}")

    # A boot resume arrives while housekeeping is fully wedged AND backlogged.
    started = {}

    def boot_resume_body():
        started["t"] = time.monotonic()

    submitted = time.monotonic()
    resume = asyncio.ensure_future(
        runner._run_in_executor_with_context(boot_resume_body)
    )
    await asyncio.wait_for(resume, timeout=10)
    latency = started["t"] - submitted
    print(f"boot-resume body started after {latency:.3f}s")

    ok = True
    if turn_held != 0:
        print(f"FAIL: housekeeping occupied {turn_held} turn-pool workers")
        ok = False
    if hk_held != hk_workers or hk_queued == 0:
        print("FAIL: housekeeping pool was not saturated+backlogged")
        ok = False
    if latency > 1.0:
        print(f"FAIL: boot resume was delayed {latency:.2f}s")
        ok = False

    wedge.set()
    if not ok:
        return 1
    print(
        "FIXED: housekeeping saturated and backlogged on its OWN pool; the "
        f"turn pool stayed entirely free and the boot resume started in "
        f"{latency:.3f}s."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
