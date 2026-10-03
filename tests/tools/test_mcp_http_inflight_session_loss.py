"""In-flight MCP HTTP calls fail fast when the server restarts mid-call.

Incident (clanker, 2026-10-02 19:33): tier3-scheduler-mcp (FastMCP,
streamable-http) restarted while ``hacr`` was in flight. mcp 1.x dropped the
response stream without resolving the request, so the call rode the full
300 s tool timeout holding ``_rpc_lock``, and every other session's call to
that server queued behind it (up to 260 s). The idle keepalive could not help:
it skips probing while an RPC is in flight.

``MCPServerTask._watch_http_session`` races the RPC: it fails the call when
the server task replaces the session (transport crash + reconnect) or when a
``ping`` on the call's session reports session loss.

Two layers:

- unit tests drive the real handler + real ``MCPServerTask`` with a session
  whose ``call_tool`` never resolves (the mcp 1.x behaviour), so they fail
  without the watcher whatever SDK version CI runs;
- an e2e test restarts a real streamable-http server mid-call and asserts the
  error arrives within a few seconds, not the tool timeout.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytest.importorskip("mcp")


# ---------------------------------------------------------------------------
# Unit layer: real handler, real MCPServerTask, fake hanging session
# ---------------------------------------------------------------------------


class _HangingSession:
    """A session whose tools/call never answers (mcp 1.x after a restart)."""

    def __init__(self, ping_behaviour):
        self.calls = 0
        self.pings = 0
        self._ping = ping_behaviour

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        await asyncio.sleep(3600)

    async def send_ping(self):
        self.pings += 1
        return await self._ping(self)


# The fork split tools/mcp_tool.py into sibling modules (parity 2026-10-01): the handler
# factory, discovery and lifecycle entry points live there, state stays on mcp_tool.
def _handlers():
    from tools import mcp_tool_handlers
    return mcp_tool_handlers


def _discovery():
    from tools import mcp_tool_discovery
    return mcp_tool_discovery


def _lifecycle():
    from tools import mcp_tool_lifecycle
    return mcp_tool_lifecycle


def _install_http_server(mcp_tool, name, session):
    server = mcp_tool.MCPServerTask(name)
    server._config = {"url": "http://127.0.0.1:9/mcp"}
    server.session = session
    mcp_tool._servers[name] = server
    mcp_tool._server_error_counts.pop(name, None)
    mcp_tool._server_breaker_opened_at.pop(name, None)
    return server


def _cleanup(mcp_tool, name):
    mcp_tool._servers.pop(name, None)
    mcp_tool._server_error_counts.pop(name, None)
    mcp_tool._server_breaker_opened_at.pop(name, None)


@pytest.fixture
def mcp_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool as mod

    # raising=False: on a pre-fix tree the knobs don't exist and the tests
    # must fail on behaviour (hang -> slow error), not on setup.
    monkeypatch.setattr(mod, "_HTTP_INFLIGHT_PROBE_INTERVAL", 0.2, raising=False)
    monkeypatch.setattr(mod, "_HTTP_INFLIGHT_PROBE_TIMEOUT", 0.5, raising=False)
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    return mod


def _reconnect_requested(mod, server):
    # _signal_reconnect sets the event via call_soon_threadsafe.
    for _ in range(50):
        if server._reconnect_event.is_set():
            return True
        time.sleep(0.02)
    return False


def test_ping_session_loss_fails_inflight_call_fast(mcp_tool):
    async def _terminated(_s):
        raise RuntimeError("Session terminated")

    session = _HangingSession(_terminated)
    server = _install_http_server(mcp_tool, "srv-http-lost", session)
    try:
        handler = _handlers()._make_tool_handler("srv-http-lost", "hacr", 12.0)
        t0 = time.monotonic()
        parsed = json.loads(handler({}))
        elapsed = time.monotonic() - t0
        assert "error" in parsed, parsed
        assert "lost the HTTP session mid-call" in parsed["error"], parsed
        assert elapsed < 5.0, f"took {elapsed:.1f}s, tool timeout is 12s"
        assert _reconnect_requested(mcp_tool, server)
        # The tool may already have run server-side: never auto-retry it.
        assert session.calls == 1
        # The call slot is released, so the next caller is not serialized.
        assert not server._rpc_lock.locked()
    finally:
        _cleanup(mcp_tool, "srv-http-lost")


def test_session_replaced_by_reconnect_fails_inflight_call_fast(mcp_tool):
    async def _hang(_s):
        await asyncio.sleep(3600)  # dead session: ping never answers

    session = _HangingSession(_hang)
    server = _install_http_server(mcp_tool, "srv-http-swap", session)
    try:
        handler = _handlers()._make_tool_handler("srv-http-swap", "hacr", 12.0)
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", handler({})))
        t0 = time.monotonic()
        th.start()
        time.sleep(0.5)
        # The server task rebuilt the transport: a fresh session object.
        server.session = _HangingSession(_hang)
        th.join(20)
        elapsed = time.monotonic() - t0
        parsed = json.loads(out["r"])
        assert "lost the HTTP session mid-call" in parsed.get("error", ""), parsed
        assert elapsed < 5.0, f"took {elapsed:.1f}s"
        assert session.calls == 1
    finally:
        _cleanup(mcp_tool, "srv-http-swap")


def test_healthy_slow_call_is_not_failed(mcp_tool):
    """A long call on a live session (ping answers) must complete normally."""

    async def _pong(_s):
        return None

    class _SlowSession(_HangingSession):
        async def call_tool(self, name, arguments=None):
            self.calls += 1
            await asyncio.sleep(1.2)
            from types import SimpleNamespace

            return SimpleNamespace(
                isError=False, is_error=False,
                content=[SimpleNamespace(type="text", text="done")],
                structuredContent=None, structured_content=None, meta=None,
            )

    session = _SlowSession(_pong)
    _install_http_server(mcp_tool, "srv-http-slow", session)
    try:
        handler = _handlers()._make_tool_handler("srv-http-slow", "hacr", 30.0)
        parsed = json.loads(handler({}))
        assert parsed.get("result") == "done", parsed
        assert session.pings >= 1, "watcher never probed a >1s call"
    finally:
        _cleanup(mcp_tool, "srv-http-slow")


def test_slow_ping_alone_does_not_fail_the_call(mcp_tool):
    """A busy server that is slow to answer ping is not a dead server."""

    async def _slow_pong(_s):
        await asyncio.sleep(3600)

    class _SlowSession(_HangingSession):
        async def call_tool(self, name, arguments=None):
            self.calls += 1
            await asyncio.sleep(1.5)
            from types import SimpleNamespace

            return SimpleNamespace(
                isError=False, is_error=False,
                content=[SimpleNamespace(type="text", text="done")],
                structuredContent=None, structured_content=None, meta=None,
            )

    session = _SlowSession(_slow_pong)
    _install_http_server(mcp_tool, "srv-http-busy", session)
    try:
        handler = _handlers()._make_tool_handler("srv-http-busy", "hacr", 30.0)
        parsed = json.loads(handler({}))
        assert parsed.get("result") == "done", parsed
    finally:
        _cleanup(mcp_tool, "srv-http-busy")


# ---------------------------------------------------------------------------
# E2E layer: a real streamable-http server restarted mid-call
# ---------------------------------------------------------------------------

_SERVER_SRC = textwrap.dedent(
    """
    import asyncio, sys
    import uvicorn
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer

    srv = MCPServer("restart-e2e")

    @srv.tool()
    async def slow(seconds: float = 30.0) -> str:
        await asyncio.sleep(seconds)
        return "slow-done"

    @srv.tool()
    async def fast() -> str:
        return "fast-done"

    uvicorn.run(srv.streamable_http_app(), host="127.0.0.1",
                port=int(sys.argv[1]), log_level="error")
    """
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(script, port):
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.skip("scratch MCP server failed to start")
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.skip("scratch MCP server did not listen in time")


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def test_server_restart_midcall_errors_within_seconds(monkeypatch, tmp_path):
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool

    script = tmp_path / "server.py"
    script.write_text(_SERVER_SRC)
    port = _free_port()
    proc = _start(script, port)
    name = "restart_e2e"
    tool_timeout = 60.0
    try:
        _discovery().register_mcp_servers({
            name: {"url": f"http://127.0.0.1:{port}/mcp",
                   "timeout": tool_timeout, "skip_preflight": True},
        })
        slow = _handlers()._make_tool_handler(name, "slow", tool_timeout)
        fast = _handlers()._make_tool_handler(name, "fast", tool_timeout)
        assert "fast-done" in fast({})

        results = {}

        def _timed(label, fn):
            t0 = time.monotonic()
            out = fn()
            results[label] = (time.monotonic() - t0, out)

        inflight = threading.Thread(
            target=_timed, args=("inflight", lambda: slow({"seconds": 45}))
        )
        inflight.start()
        time.sleep(1.0)
        _stop(proc)
        time.sleep(0.5)
        proc = _start(script, port)
        # A second caller (another session) queued behind the in-flight call.
        queued = threading.Thread(target=_timed, args=("queued", lambda: fast({})))
        queued.start()
        inflight.join(tool_timeout + 10)
        queued.join(tool_timeout + 10)

        in_s, in_out = results["inflight"]
        assert "error" in json.loads(in_out), in_out
        assert in_s < 10.0, f"in-flight call took {in_s:.1f}s to fail: {in_out}"
        q_s, _q_out = results["queued"]
        assert q_s < 10.0, f"queued call waited {q_s:.1f}s behind the dead call"
        # The server is usable again on the rebuilt session.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "fast-done" in fast({}):
                break
            time.sleep(0.5)
        else:
            pytest.fail("server never became usable after the restart")
    finally:
        _lifecycle().shutdown_mcp_servers()
        _stop(proc)
