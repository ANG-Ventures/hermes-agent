"""t_8d085477: shutdown-path blocking work stays off the event loop, and the
shutdown watchdog dump names the await the stop path is parked on.
"""

import ast
import asyncio
import threading
from pathlib import Path

import gateway.run as gateway_run
from gateway.shutdown_watchdog import _format_asyncio_tasks, _write_watchdog_dump


def _stop_impl_calls_to(name: str):
    """Every Call node naming ``name`` inside GatewayRunner.stop()."""
    tree = ast.parse(Path(gateway_run.__file__).read_text(encoding="utf-8"))
    runner = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "GatewayRunner"
    )
    stop = next(
        n for n in runner.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "stop"
    )
    direct, offloaded = [], []
    for node in ast.walk(stop):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            direct.append(node.lineno)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == name
        ):
            offloaded.append(node.lineno)
    return direct, offloaded


def test_kill_tool_subprocesses_never_runs_on_the_loop():
    # The loop thread was dumped parked in _fire_job_lock (4 force-exits) and
    # in a terminal-env glob sweep (1) under this helper. Both call sites must
    # hand it to a worker thread.
    direct, offloaded = _stop_impl_calls_to("_kill_tool_subprocesses")
    assert direct == [], f"_kill_tool_subprocesses called on the loop at {direct}"
    assert len(offloaded) == 2, offloaded


def test_shutdown_cron_mark_is_bounded():
    src = Path(gateway_run.__file__).read_text(encoding="utf-8")
    assert "lock_timeout=_SHUTDOWN_CRON_MARK_LOCK_TIMEOUT_S" in src
    assert 0 < gateway_run._SHUTDOWN_CRON_MARK_LOCK_TIMEOUT_S < 15.0


def test_watchdog_dump_includes_parked_coroutine(tmp_path):
    loop = asyncio.new_event_loop()
    parked = threading.Event()
    gate = None

    async def _stop_path_awaiting_teardown():
        parked.set()
        await gate.wait()

    def _run():
        nonlocal gate
        asyncio.set_event_loop(loop)
        gate = asyncio.Event()
        loop.run_until_complete(_stop_path_awaiting_teardown())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert parked.wait(5)
    try:
        text = _format_asyncio_tasks(loop)
        dump = tmp_path / "wd.log"
        _write_watchdog_dump(dump, delay_s=1.0, snapshot={}, loop=loop)
    finally:
        loop.call_soon_threadsafe(gate.set)
        t.join(5)
        loop.close()

    assert "_stop_path_awaiting_teardown" in text
    body = dump.read_text(encoding="utf-8")
    assert "--- asyncio tasks ---" in body
    assert "_stop_path_awaiting_teardown" in body


def test_task_dump_without_loop_is_explicit():
    assert "no loop" in _format_asyncio_tasks(None)
