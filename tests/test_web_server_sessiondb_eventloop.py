import ast
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import web_server
from hermes_cli.web_routers import sessions as web_sessions


TARGET_HANDLERS = {
    "bulk_delete_sessions_endpoint",
    "count_empty_sessions_endpoint",
    "delete_empty_sessions_endpoint",
    "get_session_latest_descendant",
    "get_session_messages",
    "delete_session_endpoint",
    "export_session_endpoint",
    "prune_sessions_endpoint",
    "get_usage_analytics",
    "get_models_analytics",
}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_sessiondb_handlers_open_connections_inside_executor_helpers():
    # The session route handlers were extracted to web_routers/sessions.py
    # (wave 2); the analytics handlers and the executor helpers still live in
    # web_server.py — scan both modules' top-level bodies.
    handlers: dict[str, ast.AsyncFunctionDef] = {}
    top_level_helpers: dict[str, ast.FunctionDef] = {}
    for mod in (web_server, web_sessions):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in TARGET_HANDLERS:
                handlers[node.name] = node
            elif isinstance(node, ast.FunctionDef):
                top_level_helpers[node.name] = node
    assert handlers.keys() == TARGET_HANDLERS

    for name, handler in handlers.items():
        helpers = {
            **top_level_helpers,
            **{
                node.name: node
                for node in handler.body
                if isinstance(node, ast.FunctionDef)
            },
        }
        offloaded = {
            arg.id
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and _call_name(node) == "to_thread"
            for arg in node.args[:1]
            if isinstance(arg, ast.Name)
        }
        db_open_owners = {
            helper_name
            for helper_name, helper in helpers.items()
            if helper_name in offloaded
            and any(
                isinstance(node, ast.Call)
                and _call_name(node) == "_open_session_db_for_profile"
                for node in ast.walk(helper)
            )
        }
        assert db_open_owners, f"{name} does not offload SessionDB open + work"


def test_sessiondb_opens_declare_access_mode():
    for mod in (web_server, web_sessions):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == "_open_session_db_for_profile"
        ]
        assert calls
        for call in calls:
            assert any(keyword.arg == "read_only" for keyword in call.keywords)


def test_bulk_delete_sessiondb_work_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    db_threads: list[int] = []
    db_modes: list[bool] = []

    class _DB:
        def delete_sessions(self, ids):
            db_threads.append(threading.get_ident())
            assert ids == ["one", "two"]
            return 2

        def close(self):
            db_threads.append(threading.get_ident())

    def _open_db(profile=None, *, read_only):
        assert profile is None
        db_modes.append(read_only)
        return _DB()

    monkeypatch.setattr(web_server, "_open_session_db_for_profile", _open_db)

    result = asyncio.run(
        web_server.bulk_delete_sessions_endpoint(
            web_server.BulkDeleteSessions(ids=["one", "two"])
        )
    )

    assert result == {"ok": True, "deleted": 2}
    assert db_modes == [False]
    assert db_threads
    assert all(thread_id != loop_thread for thread_id in db_threads)


def test_heavy_read_semaphore_config_is_lazy_and_loop_bound(monkeypatch):
    from hermes_cli import session_db_heavy_gate as gate

    configured_values = [3]
    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(
        gate,
        "_configured_max_concurrency",
        lambda: configured_values[-1],
    )

    async def first_loop():
        sem_a = gate.session_db_heavy_read_semaphore()
        sem_b = gate.session_db_heavy_read_semaphore()
        assert sem_a is sem_b
        assert sem_a._value == 3
        configured_values.append(4)
        sem_c = gate.session_db_heavy_read_semaphore()
        assert sem_c is not sem_a
        return sem_c._value

    assert asyncio.run(first_loop()) == 4

    async def second_loop():
        sem = gate.session_db_heavy_read_semaphore()
        return sem._value

    assert asyncio.run(second_loop()) == 4


def test_ws_and_rest_session_db_heavy_reads_share_async_bound(monkeypatch):
    from hermes_cli import session_db_heavy_gate as gate
    from tui_gateway import ws as ws_gateway

    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 2)

    lock = threading.Lock()
    release = threading.Event()
    in_flight = 0
    max_in_flight = 0
    entered = 0

    def heavy_job(label):
        nonlocal entered, in_flight, max_in_flight
        with lock:
            entered += 1
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if entered >= 2:
                release.set()
        try:
            assert release.wait(timeout=1), "forced-overlap gate never opened"
            time.sleep(0.02)
            return label
        finally:
            with lock:
                in_flight -= 1

    def fake_ws_request(req, transport):
        return {"jsonrpc": "2.0", "id": req["id"], "result": {"label": heavy_job(req["id"])}}

    monkeypatch.setattr(ws_gateway.server, "handle_request_bound", fake_ws_request)

    async def run_reads():
        rest_a = web_server._session_db_read(heavy_job, "rest-a", heavy=True)
        rest_b = web_server._session_db_read(heavy_job, "rest-b", heavy=True)
        ws_a = ws_gateway._dispatch_ws_request(
            {"id": "ws-a", "method": "session.list", "params": {}}, None
        )
        ws_b = ws_gateway._dispatch_ws_request(
            {"id": "ws-b", "method": "session.list", "params": {}}, None
        )
        return await asyncio.gather(rest_a, rest_b, ws_a, ws_b)

    results = asyncio.run(run_reads())

    assert results[0:2] == ["rest-a", "rest-b"]
    assert {results[2]["result"]["label"], results[3]["result"]["label"]} == {"ws-a", "ws-b"}
    assert max_in_flight == 2


def test_queued_ws_heavy_reads_do_not_consume_default_executor_threads(monkeypatch):
    from hermes_cli import session_db_heavy_gate as gate
    from tui_gateway import ws as ws_gateway

    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 1)

    first_heavy_entered = threading.Event()
    release_first_heavy = threading.Event()

    def fake_heavy_request(req, transport):
        if req.get("method") == "session.list":
            first_heavy_entered.set()
            assert release_first_heavy.wait(timeout=1), "heavy request was not released"
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"sessions": []}}
        return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

    monkeypatch.setattr(ws_gateway.server, "handle_request_bound", fake_heavy_request)
    monkeypatch.setattr(
        ws_gateway.server,
        "dispatch",
        lambda req, transport=None: {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}},
    )

    async def run_probe():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        first = asyncio.create_task(
            ws_gateway._dispatch_ws_request(
                {"id": "heavy-1", "method": "session.list", "params": {}}, None
            )
        )
        assert await asyncio.to_thread(first_heavy_entered.wait, 1)
        queued = asyncio.create_task(
            ws_gateway._dispatch_ws_request(
                {"id": "heavy-2", "method": "session.list", "params": {}}, None
            )
        )
        await asyncio.sleep(0.02)

        fast = await asyncio.wait_for(
            ws_gateway._dispatch_ws_request(
                {"id": "fast", "method": "fast.check", "params": {}}, None
            ),
            timeout=0.2,
        )
        assert fast["result"] == {"ok": True}
        assert not queued.done()

        release_first_heavy.set()
        await asyncio.wait_for(asyncio.gather(first, queued), timeout=1)

    asyncio.run(run_probe())


def test_heavy_read_shed_returns_retryable_busy_and_logs(monkeypatch, caplog):
    from hermes_cli import session_db_heavy_gate as gate

    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 1)
    monkeypatch.setattr(gate, "_QUEUE_WAIT_TIMEOUT_S", 0.01)

    async def run_shed():
        async with gate.session_db_heavy_read_slot(surface="test", operation="held"):
            with pytest.raises(web_server.HTTPException) as excinfo:
                await web_server._session_db_read(lambda: "late", heavy=True)
            return excinfo.value

    caplog.set_level("INFO")
    exc = asyncio.run(run_shed())

    assert exc.status_code == 503
    assert exc.detail["error"] == "backend_busy"
    assert exc.detail["retryable"] is True
    assert gate.session_db_heavy_read_stats()["shed_count"] == 1
    assert "queue_wait" in caplog.text


def test_ws_heavy_read_shed_returns_retryable_busy(monkeypatch, caplog):
    from hermes_cli import session_db_heavy_gate as gate
    from tui_gateway import ws as ws_gateway

    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 1)
    monkeypatch.setattr(gate, "_QUEUE_WAIT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(
        ws_gateway.server,
        "handle_request_bound",
        lambda req, transport=None: pytest.fail("shed path should not enter the worker"),
    )

    async def run_shed():
        async with gate.session_db_heavy_read_slot(surface="test", operation="held"):
            return await ws_gateway._dispatch_ws_request(
                {"id": "busy", "method": "session.list", "params": {}}, None
            )

    caplog.set_level("INFO")
    resp = asyncio.run(run_shed())

    assert resp["id"] == "busy"
    assert resp["error"]["message"] == "backend busy; retry shortly"
    assert resp["error"]["data"]["error"] == "backend_busy"
    assert resp["error"]["data"]["retryable"] is True
    assert gate.session_db_heavy_read_stats()["shed_count"] == 1
    assert "queue_wait" in caplog.text
