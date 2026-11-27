"""Browser Use vault must follow its selected target, never origin-search another tab."""
import json
from types import SimpleNamespace

import pytest

from tests.tools._browser_use_daemon_fake import FakeDaemon, install
from tools import browser_use_cli as bu
from tools import browser_vault_tool as vault


def test_browser_use_origin_probe_does_not_search_other_tabs():
    # Origin-based tab search is gone entirely: the leased target (or bound document) owns the page.
    assert not hasattr(vault, "_focus_bound_origin")
    assert not hasattr(vault, "_TAB_PROBES")


@pytest.fixture
def target_route(monkeypatch):
    from tools import browser_use_target as target
    from tools import browser_supervisor as sup
    monkeypatch.setattr(target, "_routes", {})
    monkeypatch.setattr(bu, "is_browser_use_cli_mode", lambda: True)
    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", lambda url: url)
    selected = [42, "login-tab"]
    install(monkeypatch, target, FakeDaemon(observe=lambda route: tuple(selected)))
    calls = []
    def evaluate(expression, **kw):
        calls.append((expression, kw["target_id"]))
        return {"ok": True, "result": "https://idmsa.apple.com/login"}
    monkeypatch.setattr(sup.SUPERVISOR_REGISTRY, "get_or_start", lambda **kw: SimpleNamespace(evaluate_runtime=evaluate))
    route = target.record_route("task", {"BU_CDP_WS": "ws://browser/generation1", "BU_NAME": "named"})
    return target, route, selected, calls


def test_inspection_and_secret_write_pin_same_target(target_route, monkeypatch):
    target, route, selected, calls = target_route
    monkeypatch.setattr(vault, "_bot_desktop_browser_session", lambda tid: False)
    token = target.begin("task", expected_route=route)
    try:
        assert vault._current_page_origin("task") == "https://idmsa.apple.com"
        assert vault._eval_js_secret("task", "synthetic-secret-expression")["success"]
        assert [tid for _, tid in calls] == ["login-tab", "login-tab"]
        selected[1] = "other-tab"
        assert not vault._eval_js_secret("task", "must-not-write")["success"]
        assert len(calls) == 2
    finally:
        target.end(token)


@pytest.mark.parametrize("change", ["closed", "daemon", "endpoint"])
def test_changed_target_fails_closed(target_route, change):
    target, route, selected, calls = target_route
    token = target.begin("task", expected_route=route)
    try:
        if change == "closed":
            selected[1] = None
        elif change == "daemon":
            selected[0] += 1
        else:
            target.record_route("task", {"BU_CDP_WS": "ws://browser/generation2"})
        assert not vault._eval_js("task", "window.location.href")["success"]
        assert calls == []
    finally:
        target.end(token)


def test_redirect_reports_origin_mismatch_before_secret_read(target_route, monkeypatch):
    target, route, selected, calls = target_route
    meta = SimpleNamespace(kind="login", origin="https://appleid.apple.com", allowed_origins=[],
                           fill_supported=True)
    def no_secret(*a):
        pytest.fail("secret read on mismatched redirect")
    backend = SimpleNamespace(needs_unlock=False, get_meta=lambda h: meta, resolve_password=no_secret)
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda h: backend)
    result = json.loads(vault._handle_vault_fill({"handle": "synthetic"}, task_id="task"))
    assert result["error_type"] == "origin_mismatch"
    assert "https://idmsa.apple.com" in result["error"]
    # Witness pin + origin read, both on the leased target and nothing else.
    assert {tid for _, tid in calls} == {"login-tab"}


@pytest.mark.parametrize("initial_route", ["remote", "absent"])
def test_route_change_between_fence_and_pin_refuses_before_secret_access(target_route, monkeypatch, initial_route):
    target, route, selected, calls = target_route
    if initial_route == "absent":
        target._routes.clear()
        monkeypatch.setattr(bu, "is_browser_use_cli_mode", lambda: False)
        monkeypatch.setattr("tools.browser_tool._active_sessions", {})
    fenced = []

    def race(session, operation):
        fenced.append(session.get("features", {}).get("local", False))
        target.record_route("task", {"BU_CDP_WS": "ws://local-browser"}, local=True)
        return operation()

    monkeypatch.setattr("tools.browser_tool_session.run_fenced", race)
    result = json.loads(vault._fenced_page_op(
        "task", lambda: pytest.fail("secret access under stale remote fence")))
    assert fenced == [False]
    assert result["error_type"] == "selected_target_unavailable"
    assert calls == []


def test_route_change_during_target_probe_refuses_before_secret_access(target_route, monkeypatch):
    target, route, selected, calls = target_route

    daemon = install(monkeypatch, target, FakeDaemon(observe=lambda route: tuple(selected)))
    daemon.hooks["lease_acquire"] = lambda: target.record_route(
        "task", {"BU_CDP_WS": "ws://replacement"}, local=True)
    with pytest.raises(RuntimeError, match="route changed"):
        target.begin("task", expected_route=route)
    assert calls == []
    assert daemon.lease is None, "a refused begin must not leave the daemon selection leased"


@pytest.mark.parametrize("cleanup", ["error", "timeout", "stalled"])
def test_evaluation_success_is_not_overridden_or_delayed_by_detach(monkeypatch, cleanup):
    import asyncio
    from tools import browser_supervisor as sup

    supervisor = sup.CDPSupervisor("cleanup-test", "ws://synthetic")
    supervisor._active = True
    supervisor._page_session_id = "initial"
    monkeypatch.setattr(supervisor, "_loop", SimpleNamespace(is_running=lambda: True))
    calls = []

    async def cdp(method, params, **kwargs):
        calls.append(method)
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"type": "page", "targetId": "login"}}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "pinned"}}
        if method == "Runtime.evaluate":
            return {"result": {"result": {"value": "filled", "type": "string"}}}
        if cleanup == "stalled":
            await asyncio.Event().wait()
        if cleanup == "timeout":
            raise TimeoutError("synthetic detach timeout")
        raise RuntimeError("synthetic detach failure")

    def schedule(coro, loop, *, timeout):
        async def run():
            operation = asyncio.create_task(coro)
            # wait_for adds scheduler turns but cleanup must remain detached.
            for _ in range(8):
                if operation.done():
                    break
                await asyncio.sleep(0)
            assert operation.done(), "detach consumed the evaluation result deadline"
            response = operation.result()
            # Run the wait_for child and cleanup exception handler, without wall-clock sleeps.
            for _ in range(6):
                await asyncio.sleep(0)
            return response
        return asyncio.run(run())

    monkeypatch.setattr(supervisor, "_cdp", cdp)
    monkeypatch.setattr(sup, "_schedule", schedule)
    result = supervisor.evaluate_runtime("synthetic-write", target_id="login")
    assert result == {"ok": True, "result": "filled", "result_type": "string"}
    assert calls.count("Runtime.evaluate") == 1
    assert "Target.detachFromTarget" in calls


def test_no_route_never_falls_into_builtin(monkeypatch):
    from tools import browser_use_target as target
    monkeypatch.setattr(target, "_routes", {})
    monkeypatch.setattr(bu, "is_browser_use_cli_mode", lambda: True)
    monkeypatch.setattr("tools.browser_tool_session._run_browser_command", lambda *a, **kw: pytest.fail("wrong stack"))
    assert not vault._eval_js("task", "window.location.href")["success"]
    assert not vault._eval_js_secret("task", "synthetic-secret")["success"]


def test_selected_probe_uses_named_route_and_fixed_argv(monkeypatch):
    from tools import browser_use_target as target
    route = target.BrowserUseRoute({"BU_NAME": "named-session"}, "ws://browser", False)
    monkeypatch.setattr(bu, "_find_cli", lambda: ["python", "-m", "browser_harness.run"])
    seen = []
    def run(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout='{"pid":42,"reply":{"targetId":"login-tab","generation":3}}')
    monkeypatch.setattr(target.subprocess, "run", run)
    assert target.check_lease(route, "synthetic-lease-token") == (42, "login-tab", 3)
    assert seen[0][1]["env"]["BU_NAME"] == "named-session"
    import sys
    assert seen[0][0][:4] == [sys.executable, "-I", "-c", target._PROBE]
    assert len(seen[0][0]) == 5
    assert "PYTHONPATH" not in seen[0][1]["env"]
    assert all(isinstance(root, str) for root in json.loads(seen[0][0][4]))
    # The lease token rides the probe's stdin, never its argv.
    assert json.loads(seen[0][1]["input"]) == {"meta": "lease_check", "token": "synthetic-lease-token"}
    assert not any("synthetic-lease-token" in arg for arg in seen[0][0])


def test_profile_and_task_route_isolation(monkeypatch):
    from tools import browser_use_target as target
    home = ["profile-a"]
    monkeypatch.setattr(target, "hermes_home_key", lambda: home[0])
    monkeypatch.setattr(target, "_routes", {})
    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", lambda url: url)
    a = target.record_route("task", {"BU_CDP_WS": "ws://a"})
    assert target.route_for_task("other") is None
    home[0] = "profile-b"
    assert target.route_for_task("task") is None
    b = target.record_route("task", {"BU_CDP_WS": "ws://b"})
    assert target.route_for_task("task") is b
    home[0] = "profile-a"
    assert target.route_for_task("task") is a


def test_supervisor_pinned_wire_ignores_blank_initial_tab(monkeypatch):
    import asyncio
    import threading
    import websockets
    from tools.browser_supervisor import CDPSupervisor

    ready = threading.Event()
    state = {"closed": False, "writes": []}
    loop = asyncio.new_event_loop()

    async def serve(ws):
        async for raw in ws:
            req = json.loads(raw)
            method, params = req["method"], req.get("params", {})
            result = {}
            error = None
            if method == "Target.getTargets":
                result = {"targetInfos": [{"type": "page", "targetId": "blank", "url": "about:blank"},
                                          {"type": "page", "targetId": "login", "url": "https://idmsa.apple.com"}]}
            elif method == "Target.getTargetInfo":
                if state["closed"]:
                    error = {"message": "No target with given id"}
                else:
                    result = {"targetInfo": {"type": "page", "targetId": params["targetId"]}}
            elif method == "Target.attachToTarget":
                result = {"sessionId": "session-" + params["targetId"]}
            elif method == "Runtime.evaluate":
                state["writes"].append(req.get("sessionId"))
                result = {"result": {"type": "string", "value": req.get("sessionId")}}
            await ws.send(json.dumps({"id": req["id"], **({"error": error} if error else {"result": result})}))

    async def start():
        server = await websockets.serve(serve, "127.0.0.1", 0)
        state["server"] = server
        state["port"] = server.sockets[0].getsockname()[1]
        ready.set()
    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        loop.run_forever()
    thread = threading.Thread(target=run)
    thread.start()
    supervisor = None
    try:
        assert ready.wait(10)
        supervisor = CDPSupervisor("wire-test", f"ws://127.0.0.1:{state['port']}")
        supervisor.start()
        # Initial supervisor setup evaluates its bridge in the blank tab. Only count
        # the explicit inspection/write below.
        state["writes"].clear()
        assert supervisor.evaluate_runtime("synthetic-inspection", target_id="login")["result"] == "session-login"
        assert supervisor.evaluate_runtime("synthetic-write", target_id="login")["result"] == "session-login"
        assert state["writes"] == ["session-login", "session-login"]
        state["closed"] = True
        assert not supervisor.evaluate_runtime("must-not-write", target_id="login")["ok"]
        assert state["writes"] == ["session-login", "session-login"]
    finally:
        if supervisor:
            supervisor.stop()
        async def close():
            state["server"].close()
            await state["server"].wait_closed()
        if ready.is_set():
            asyncio.run_coroutine_threadsafe(close(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()
