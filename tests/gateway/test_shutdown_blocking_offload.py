"""t_8d085477: shutdown-path blocking work stays off the event loop, and the
shutdown watchdog dump names the await the stop path is parked on.
"""

import ast
import asyncio
import threading
from pathlib import Path

import gateway.run as gateway_run
import gateway.run_shutdown as gateway_run_shutdown
from gateway.shutdown_watchdog import _format_asyncio_tasks, _write_watchdog_dump


def _stop_impl_calls_to(name: str):
    """Every Call node naming ``name`` across the stop path (``GatewayShutdownMixin`` — parity
    2026-10-01: upstream split ``GatewayRunner.stop()`` into ``gateway/run_shutdown.py`` phase
    helpers; the kill sweep is ``_stop_kill_tool_subprocesses`` and its off-loop wrapper).

    Returns ``(direct, offloaded)``: ``direct`` are loop-thread call sites of the blocking
    sweep (any method except its own ``to_thread`` wrapper); ``offloaded`` are the
    ``await <wrapper>(...)`` sites, where the wrapper is the one method that hands
    ``name`` to ``asyncio.to_thread``.
    """
    tree = ast.parse(Path(gateway_run_shutdown.__file__).read_text(encoding="utf-8"))
    mixin = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "GatewayShutdownMixin"
    )

    def _names(func):
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    wrappers = set()
    for method in mixin.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and _names(node.func) == "to_thread"
                and node.args
                and _names(node.args[0]) == name
            ):
                wrappers.add(method.name)
    direct, offloaded = [], []
    for method in mixin.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            called = _names(node.func)
            if called == name and method.name not in wrappers:
                direct.append(node.lineno)
            if called in wrappers and method.name not in wrappers:
                offloaded.append(node.lineno)
    return direct, offloaded


def test_kill_tool_subprocesses_never_runs_on_the_loop():
    # The loop thread was dumped parked in _fire_job_lock (4 force-exits) and
    # in a terminal-env glob sweep (1) under this helper. Both call sites must
    # hand it to a worker thread.
    direct, offloaded = _stop_impl_calls_to("_stop_kill_tool_subprocesses")
    assert direct == [], f"_stop_kill_tool_subprocesses called on the loop at {direct}"
    assert len(offloaded) == 2, offloaded


def test_shutdown_cron_mark_is_bounded():
    src = Path(gateway_run_shutdown.__file__).read_text(encoding="utf-8")
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
