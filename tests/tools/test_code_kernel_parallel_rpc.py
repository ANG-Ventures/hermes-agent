"""Concurrent in-script tool calls on the session-kernel RPC transport.

A script's tool calls made from threads run at the same time, up to
``code_execution.max_parallel_tool_calls`` (1 = the old one-at-a-time wire).
Each call still passes the token, allow-list and per-cell budget once, and a
call bound to a cell that has settled or been interrupted never dispatches
under (or is charged to) a later cell.
"""

import json
import os
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools.code_execution_tool import execute_code
from tools.code_kernel import shutdown_all_kernels


@pytest.fixture(autouse=True)
def _local_terminal_and_fresh_kernels(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


@contextmanager
def _config(**overrides):
    config = {"mode": "strict", "timeout": 30}
    config.update(overrides)
    with patch("tools.code_execution_tool._load_config", return_value=config):
        yield


class _SlowTool:
    """Fake dispatch: sleeps, records peak concurrency, echoes the query back."""

    def __init__(self, delay):
        self.delay, self.lock = delay, threading.Lock()
        self.inflight = self.peak = self.calls = 0
        self.first_start = 0.0
        self.queries = []

    def __call__(self, tool_name, tool_args, task_id=None):
        with self.lock:
            self.inflight += 1
            self.calls += 1
            self.first_start = self.first_start or time.monotonic()
            self.peak = max(self.peak, self.inflight)
            self.queries.append(tool_args.get("query"))
        try:
            time.sleep(self.delay)
            return json.dumps({"echo": tool_args.get("query")})
        finally:
            with self.lock:
                self.inflight -= 1


def _run(code, **kwargs):
    return json.loads(execute_code(code, task_id="parallel-rpc-test", **kwargs))


_FOUR_THREADS = """
import json, threading, time
from hermes_tools import web_search
out = {}
def go(i):
    out[i] = web_search(query="q%d" % i)["echo"]
t0 = time.monotonic()
threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
print(json.dumps({"wall": time.monotonic() - t0, "out": out}))
"""


def _report(result):
    assert result["status"] == "success", result
    return json.loads(result["output"].strip().splitlines()[-1])


def test_four_concurrent_calls_overlap_and_each_caller_gets_its_own_result():
    tool = _SlowTool(1.0)
    with _config(max_parallel_tool_calls=4), patch("model_tools.handle_function_call", new=tool):
        result = _run(_FOUR_THREADS)
    report = _report(result)
    assert report["out"] == {"0": "q0", "1": "q1", "2": "q2", "3": "q3"}
    assert report["wall"] < 2.0, report
    assert tool.peak == 4
    assert result["tool_calls_made"] == 4


def test_default_cap_is_parallel():
    tool = _SlowTool(1.0)
    with _config(), patch("model_tools.handle_function_call", new=tool):
        report = _report(_run(_FOUR_THREADS))
    assert report["wall"] < 2.0, report


def test_cap_one_runs_calls_strictly_one_at_a_time():
    tool = _SlowTool(0.3)
    with _config(max_parallel_tool_calls=1), patch("model_tools.handle_function_call", new=tool):
        result = _run(_FOUR_THREADS)
    report = _report(result)
    assert report["out"] == {"0": "q0", "1": "q1", "2": "q2", "3": "q3"}
    assert tool.peak == 1
    assert report["wall"] >= 1.2, report
    assert result["tool_calls_made"] == 4


def test_cap_bounds_in_flight_calls():
    tool = _SlowTool(0.5)
    with _config(max_parallel_tool_calls=2), patch("model_tools.handle_function_call", new=tool):
        report = _report(_run(_FOUR_THREADS))
    assert tool.peak == 2
    assert report["out"] == {"0": "q0", "1": "q1", "2": "q2", "3": "q3"}


def test_budget_is_exact_under_concurrency():
    """N concurrent calls against a budget of M (N > M) dispatch exactly M and refuse the rest."""
    tool = _SlowTool(0.5)
    code = """
import json, threading
from hermes_tools import web_search
res = []
lock = threading.Lock()
def go(i):
    r = web_search(query="q%d" % i)
    with lock:
        res.append("echo" if "echo" in r else r.get("error", ""))
threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
print(json.dumps(res))
"""
    with _config(max_parallel_tool_calls=8, max_tool_calls=3), \
            patch("model_tools.handle_function_call", new=tool):
        result = _run(code)
    assert result["status"] == "success", result
    res = json.loads(result["output"].strip().splitlines()[-1])
    assert tool.calls == 3
    assert res.count("echo") == 3
    refused = [r for r in res if r != "echo"]
    assert len(refused) == 5 and all("Tool call limit reached" in r for r in refused), res
    assert result["tool_calls_made"] == 3


def test_queued_call_of_a_settled_cell_never_runs_under_the_next_cell():
    """Cell 1 leaves 3 calls behind (cap 2: two run, one waits) and settles. The waiting call
    must be refused, not dispatched under cell 2, and cell 2 is charged nothing."""
    tool = _SlowTool(1.0)
    leave_behind = """
import threading
from hermes_tools import web_search
for i in range(3):
    threading.Thread(target=lambda i=i: web_search(query="late%d" % i), daemon=True).start()
import time; time.sleep(0.3)
"""
    with _config(max_parallel_tool_calls=2), patch("model_tools.handle_function_call", new=tool):
        first = _run(leave_behind)
        second = _run("import time; time.sleep(2.0)\nprint('ok')")
    assert first["status"] == "success", first
    assert second["status"] == "success", second
    assert tool.calls == 2, tool.queries
    assert second["tool_calls_made"] == 0
    assert "tool_errors" not in second, second


def test_in_flight_call_of_an_interrupted_cell_is_not_dispatched_or_counted_later():
    """Cap 2, three calls: two run, one waits for a slot. The cell is interrupted while they
    run, and the cell ignores SIGTERM so the kernel teardown takes its 5 s escalation. The
    waiting call must never dispatch, and the next cell is charged nothing."""
    tool = _SlowTool(1.0)
    code = """
import signal, threading, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
from hermes_tools import web_search
for i in range(3):
    threading.Thread(target=lambda i=i: web_search(query="x%d" % i), daemon=True).start()
time.sleep(30)
"""
    def interrupted_once_two_run():
        return tool.calls >= 2 and time.monotonic() - tool.first_start > 0.3
    with _config(max_parallel_tool_calls=2), patch("model_tools.handle_function_call", new=tool):
        with patch("tools.interrupt.is_interrupted", new=interrupted_once_two_run):
            interrupted = _run(code)
        second = _run("import time; time.sleep(1.5)\nprint('ok')")
    assert interrupted["status"] == "interrupted", interrupted
    assert second["status"] == "success", second
    assert tool.calls == 2, tool.queries
    assert second["tool_calls_made"] == 0
    assert "tool_errors" not in second, second


def test_parallel_helper_returns_results_in_order():
    tool = _SlowTool(1.0)
    code = """
import json, time
from agent_tools import parallel, web_search
t0 = time.monotonic()
out = parallel(lambda: web_search(query="a"), lambda: web_search(query="b"), lambda: web_search(query="c"))
print(json.dumps({"wall": time.monotonic() - t0, "out": [o["echo"] for o in out]}))
"""
    with _config(max_parallel_tool_calls=4), patch("model_tools.handle_function_call", new=tool):
        report = _report(_run(code))
    assert report["out"] == ["a", "b", "c"]
    assert report["wall"] < 2.0, report


def test_description_pins_batching_web_stub_and_parallel_guidance():
    from tools.code_execution_tool import build_execute_code_schema

    description = build_execute_code_schema(mode="project")["description"]
    assert "Batch three or more related tool calls into one script and print only what you need" in description
    assert "Do not wrap a single call or a two-call lookup in a script" in description
    assert ("Fetch web content in a script only through web_search/web_extract, never a direct HTTP "
            "client") in description
    assert "Stub calls run in parallel: parallel(fn1, fn2, ...)" in description
    no_web = build_execute_code_schema({"terminal", "read_file"}, mode="project")["description"]
    assert "web_extract" not in no_web
