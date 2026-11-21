"""Tests for MCP tool-handler transport-session auto-reconnect.

When a Streamable HTTP MCP server garbage-collects its server-side
session (idle TTL, server restart, pod rotation, …) it rejects
subsequent requests with a JSON-RPC error containing phrases like
``"Invalid or expired session"``.  The OAuth token remains valid —
only the transport session state needs rebuilding.

Before the #13383 fix, this class of failure fell through as a plain
tool error with no recovery path, so every subsequent call on the
affected MCP server failed until the gateway was manually restarted.
"""
import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from tools import mcp_tool_loop as _mcp_loop


# ---------------------------------------------------------------------------
# _is_session_expired_error — unit coverage
# ---------------------------------------------------------------------------


def test_is_session_expired_detects_invalid_or_expired_session():
    """Reporter's exact wpcom-mcp error message (#13383)."""
    from tools.mcp_tool_errors import _is_session_expired_error
    exc = RuntimeError("Invalid params: Invalid or expired session")
    assert _is_session_expired_error(exc) is True


def test_is_session_expired_detects_expired_session_variant():
    """Generic ``session expired`` / ``expired session`` phrasings used
    by other SDK servers."""
    from tools.mcp_tool_errors import _is_session_expired_error
    assert _is_session_expired_error(RuntimeError("Session expired")) is True
    assert _is_session_expired_error(RuntimeError("expired session: abc")) is True


def test_is_session_expired_detects_session_not_found():
    """Server-side GC produces ``session not found`` / ``unknown session``
    on some implementations."""
    from tools.mcp_tool_errors import _is_session_expired_error
    assert _is_session_expired_error(RuntimeError("session not found")) is True
    assert _is_session_expired_error(RuntimeError("Unknown session: abc123")) is True


def test_is_session_expired_traversal_is_budget_bounded():
    """Pathologically long chains stop at the node budget without spinning."""
    from tools import mcp_tool_errors as _mcp_errors
    from tools.mcp_tool_errors import _is_session_expired_error

    exc: BaseException = RuntimeError("leaf")
    for i in range(_mcp_errors._EXC_TRAVERSAL_MAX_NODES * 2):
        wrapper = RuntimeError(f"layer {i}")
        wrapper.__cause__ = exc
        exc = wrapper

    # Terminates quickly and classifies false (no transport signal within
    # budget). The exact outcome past the budget is unspecified; the
    # invariant under test is termination.
    assert _is_session_expired_error(exc) is False


def test_is_session_expired_walks_group_chain():
    """A group's own ``__cause__``/``__context__`` are inspected like any node's: a marker there classifies
    as expired, and an InterruptedError there still overrides a marker inside the group."""
    from tools.mcp_tool_errors import _is_session_expired_error

    group = ExceptionGroup("task group", [ValueError("unrelated")])
    group.__context__ = RuntimeError("session terminated")
    assert _is_session_expired_error(group) is True

    group = ExceptionGroup("task group", [RuntimeError("session terminated")])
    group.__context__ = InterruptedError()
    assert _is_session_expired_error(group) is False


# ---------------------------------------------------------------------------
# Handler integration — verify the recovery plumbing wires end-to-end
# ---------------------------------------------------------------------------


def _install_stub_server(name: str = "wpcom"):
    """Register a minimal server stub that _handle_session_expired_and_retry
    can signal via _reconnect_event, and that reports ready+session after
    the event fires."""

    _mcp_loop._ensure_mcp_loop()

    server = MagicMock()
    server.name = name

    ready_flag = threading.Event()
    ready_flag.set()

    class _ReadyAdapter:
        def is_set(self):
            return ready_flag.is_set()

        def clear(self):
            ready_flag.clear()

        def set(self):
            ready_flag.set()

    server._ready = _ReadyAdapter()

    # _reconnect_event is called via loop.call_soon_threadsafe(…set); use
    # a threading-safe substitute.  The production reconnect path must not
    # treat the old stale session as fresh, so this test double swaps in a
    # distinct session object when reconnect is requested.
    reconnect_flag = threading.Event()

    class _EventAdapter:
        def set(self):
            reconnect_flag.set()
            old_session = server.session
            new_session = MagicMock()
            for method_name in (
                "call_tool",
                "list_resources",
                "read_resource",
                "list_prompts",
                "get_prompt",
            ):
                if hasattr(old_session, method_name):
                    setattr(new_session, method_name, getattr(old_session, method_name))
            server.session = new_session
            ready_flag.set()

    server._reconnect_event = _EventAdapter()

    # session attr must be truthy for the handler's initial check
    # (``if not server or not server.session``) and for the post-
    # reconnect readiness probe (``srv.session is not None``).
    server.session = MagicMock()
    return server, reconnect_flag


@pytest.mark.parametrize(
    "transport_config, expected_route",
    [
        ({"command": "librarian-mcp"}, "stdio"),
        ({"url": "https://neo4j.example.test/mcp", "skip_preflight": True}, "http"),
    ],
    ids=["stdio", "http"],
)
@pytest.mark.parametrize("application_error", [False, True], ids=["success", "application-error"])
def test_call_tool_handler_rebuilds_configured_server_transport(
    monkeypatch, tmp_path, transport_config, expected_route, application_error
):
    """The real server run loop selects and rebuilds its configured transport."""
    from anyio import ClosedResourceError
    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask
    from tools.mcp_tool_handlers import _make_tool_handler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mcp_loop._ensure_mcp_loop()
    transport_ready = threading.Event()
    routes = []
    configs = []
    sessions = []
    call_count = {"n": 0}

    class _Session:
        async def call_tool(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ClosedResourceError
            result = MagicMock()
            result.is_error = application_error
            result.content = [MagicMock(type="text", text="reconnected")]
            result.structured_content = None
            return result

    class _LifecycleTask(MCPServerTask):
        async def _serve_transport(self, route, config):
            routes.append(route)
            configs.append(dict(config))
            self.session = _Session()
            sessions.append(self.session)
            self._ready.set()
            transport_ready.set()
            return await self._wait_for_lifecycle_event()

        async def _run_stdio(self, config):
            return await self._serve_transport("stdio", config)

        async def _run_http(self, config):
            return await self._serve_transport("http", config)

    server = _LifecycleTask("resumed")
    mcp_tool._servers["resumed"] = server
    mcp_tool._server_error_counts.pop("resumed", None)
    mcp_tool._server_breaker_opened_at.pop("resumed", None)
    # Auto-retry after session expiry is only safe (and only performed) for
    # tools positively annotated read-only; a write may already have
    # executed server-side (#88821).
    mcp_tool._tool_read_only_hints.setdefault("resumed", {})["health"] = True
    loop = mcp_tool._mcp_loop
    assert loop is not None
    run_future = asyncio.run_coroutine_threadsafe(
        server.run(transport_config), loop
    )

    try:
        assert transport_ready.wait(3), "server lifecycle did not establish transport"
        handler = _make_tool_handler("resumed", "health", 10.0)
        parsed = json.loads(handler({}))

        if expected_route == "stdio":
            # A stdio pipe closing after dispatch is an ambiguous mid-call death: the transport
            # is rebuilt for future calls, but the call itself is not replayed (#106440).
            assert parsed["outcome_uncertain"] is True, parsed
            assert call_count["n"] == 1
        else:
            assert parsed == {"error" if application_error else "result": "reconnected"}
            # The recovered result is the tool's real answer either way; an application error is
            # still one breaker strike (#10447), a success resets the counter.
            assert mcp_tool._server_error_counts.get("resumed", 0) == (1 if application_error else 0)
            assert call_count["n"] == 2
        assert routes == [expected_route, expected_route]
        assert configs == [transport_config, transport_config]
        assert len(sessions) == 2
        assert sessions[0] is not sessions[1]
    finally:
        loop.call_soon_threadsafe(server._shutdown_event.set)
        run_future.result(timeout=5)
        mcp_tool._servers.pop("resumed", None)
        mcp_tool._server_error_counts.pop("resumed", None)
        mcp_tool._server_breaker_opened_at.pop("resumed", None)
        mcp_tool._tool_read_only_hints.pop("resumed", None)


def test_session_expired_retry_waits_for_new_session(monkeypatch, tmp_path):
    """Regression for long-lived HTTP/stream MCP sessions.

    If the reconnect helper only checks ``_ready.is_set()`` and
    ``session is not None``, it can return immediately while ``session`` still
    points at the stale transport. The retry then hits the same dead session
    and the circuit breaker eventually reports the server as unreachable. The
    handler must wait for a distinct session object before retrying.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools import mcp_tool_loop as _mcp_loop
    from tools.mcp_tool_handlers import _make_tool_handler

    _mcp_loop._ensure_mcp_loop()
    server = MagicMock()
    server.name = "hindsight"
    ready_flag = threading.Event()
    ready_flag.set()

    class _ReadyAdapter:
        def is_set(self):
            return ready_flag.is_set()

        def clear(self):
            ready_flag.clear()

        def set(self):
            ready_flag.set()

    old_session = MagicMock()

    async def _old_call(*a, **kw):
        raise RuntimeError("Session terminated")

    old_session.call_tool = _old_call
    new_session = MagicMock()

    async def _new_call(*a, **kw):
        result = MagicMock()
        result.is_error = False
        result.content = [MagicMock(type="text", text="bank ok")]
        result.structured_content = None
        return result

    new_session.call_tool = _new_call
    server.session = old_session
    server._ready = _ReadyAdapter()

    class _ReconnectAdapter:
        def set(self):
            server.session = new_session
            ready_flag.set()

    server._reconnect_event = _ReconnectAdapter()
    mcp_tool._servers["hindsight"] = server
    mcp_tool._server_error_counts["hindsight"] = 7
    # Read-only annotation: the session-expired auto-retry now only fires
    # for tools positively marked readOnlyHint=True (write-capable calls
    # get the outcome-unknown path instead).
    mcp_tool._tool_read_only_hints.setdefault("hindsight", {})["get_bank"] = True
    # Stamp the breaker "open" far enough in the past that the cooldown has
    # provably elapsed, so this call is a half-open probe. The breaker compares
    # against time.monotonic() (tools/mcp_tool.py), whose origin is arbitrary and
    # small on a freshly-booted CI container — a hardcoded literal like 123.0
    # only looked "elapsed" on a long-uptime dev box and flaked under CI.
    mcp_tool._server_breaker_opened_at["hindsight"] = (
        time.monotonic() - mcp_tool._CIRCUIT_BREAKER_COOLDOWN_SEC - 1.0
    )

    try:
        handler = _make_tool_handler("hindsight", "get_bank", 10.0)
        parsed = json.loads(handler({}))
        assert parsed.get("result") == "bank ok", parsed
        assert mcp_tool._server_error_counts.get("hindsight", 0) == 0
        assert "hindsight" not in mcp_tool._server_breaker_opened_at
    finally:
        mcp_tool._servers.pop("hindsight", None)
        mcp_tool._server_error_counts.pop("hindsight", None)
        mcp_tool._server_breaker_opened_at.pop("hindsight", None)
        mcp_tool._tool_read_only_hints.pop("hindsight", None)


def test_session_expired_handler_returns_none_without_loop(monkeypatch):
    """Defensive: if the MCP loop isn't running (cold start / shutdown
    race), the handler must fall through cleanly instead of hanging
    or raising."""
    from tools import mcp_tool
    from tools.mcp_tool_handlers import _handle_session_expired_and_retry

    # Install a server stub but make the event loop unavailable.
    server = MagicMock()
    server._reconnect_event = MagicMock()
    server._ready = MagicMock()
    server._ready.is_set = MagicMock(return_value=True)
    server.session = MagicMock()
    mcp_tool._servers["srv-noloop"] = server

    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)

    try:
        out = _handle_session_expired_and_retry(
            "srv-noloop",
            RuntimeError("Invalid or expired session"),
            lambda: '{"ok": true}',
            "tools/call",
        )
        assert out is None, (
            "Without an event loop, session-expired handler must fall "
            "through to caller's generic error path — not hang or raise."
        )
        # A write-capable call still gets the outcome-uncertain verdict: a generic "call failed"
        # would invite the model to replay a write that may have landed.
        out = _handle_session_expired_and_retry(
            "srv-noloop", RuntimeError("Invalid or expired session"), lambda: '{"ok": true}',
            "tools/call", call_may_have_side_effects=True,
        )
        assert out is not None and json.loads(out).get("outcome_uncertain") is True
    finally:
        mcp_tool._servers.pop("srv-noloop", None)


def test_session_expired_handler_returns_none_without_server_record():
    """If the server has been torn down / isn't in _servers, fall
    through cleanly — nothing to reconnect to."""
    from tools.mcp_tool_handlers import _handle_session_expired_and_retry
    out = _handle_session_expired_and_retry(
        "does-not-exist",
        RuntimeError("Invalid or expired session"),
        lambda: '{"ok": true}',
        "tools/call",
    )
    assert out is None


# ---------------------------------------------------------------------------
# Parallel coverage for resources/list, resources/read, prompts/list,
# prompts/get — all four handlers share the same exception path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_factory, handler_kwargs, session_method, op_label",
    [
        ("_make_list_resources_handler", {"tool_timeout": 10.0}, "list_resources", "list_resources"),
        ("_make_read_resource_handler", {"tool_timeout": 10.0}, "read_resource", "read_resource"),
        ("_make_list_prompts_handler", {"tool_timeout": 10.0}, "list_prompts", "list_prompts"),
        ("_make_get_prompt_handler", {"tool_timeout": 10.0}, "get_prompt", "get_prompt"),
    ],
)
def test_non_tool_handlers_also_reconnect_on_session_expired(
    monkeypatch, tmp_path, handler_factory, handler_kwargs, session_method, op_label
):
    """All four non-``tools/call`` MCP handlers share the recovery
    pattern and must reconnect the same way on session-expired."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    server, reconnect_flag = _install_stub_server(f"srv-{op_label}")
    mcp_tool._servers[f"srv-{op_label}"] = server
    mcp_tool._server_error_counts.pop(f"srv-{op_label}", None)

    call_count = {"n": 0}

    async def _sequence(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Invalid or expired session")
        # Return something with the shapes each handler expects.
        # Explicitly set primitive attrs — MagicMock's default auto-attr
        # behaviour surfaces ``MagicMock`` values for optional fields
        # like ``description``, which break ``json.dumps`` downstream.
        result = MagicMock()
        result.resources = []
        result.prompts = []
        result.contents = []
        result.messages = []  # get_prompt
        result.description = None  # get_prompt optional field
        return result

    setattr(server.session, session_method, _sequence)

    from tools import mcp_tool_handlers as _mcp_handlers
    factory = getattr(_mcp_handlers, handler_factory)
    # list_resources / list_prompts take (server_name, timeout).
    # read_resource / get_prompt take the same signature.
    try:
        handler = factory(f"srv-{op_label}", **handler_kwargs)
        if op_label == "read_resource":
            out = handler({"uri": "file://foo"})
        elif op_label == "get_prompt":
            out = handler({"name": "p1"})
        else:
            out = handler({})
        parsed = json.loads(out)
        assert "error" not in parsed, (
            f"{op_label}: expected retry success, got {parsed}"
        )
        assert reconnect_flag.is_set(), (
            f"{op_label}: reconnect should fire for session-expired"
        )
        assert call_count["n"] == 2, (
            f"{op_label}: expected 1 original + 1 retry"
        )
    finally:
        mcp_tool._servers.pop(f"srv-{op_label}", None)
        mcp_tool._server_error_counts.pop(f"srv-{op_label}", None)


# ---------------------------------------------------------------------------
# At-most-once guard for write-capable tools: a session-expired/transport
# failure on a call that may already have executed server-side must NOT be
# auto-retried. The transport is healed and an outcome_uncertain error tells
# the model to verify first. Only readOnlyHint=True tools keep the retry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("read_only", [False, True], ids=["write-capable", "read-only"])
def test_session_expired_retry_only_for_read_only_tools(monkeypatch, tmp_path, read_only):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool_handlers import _make_tool_handler

    _mcp_loop._ensure_mcp_loop()
    server, reconnect_flag = _install_stub_server("srv")
    call_count = {"n": 0}

    async def _sequence(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Session terminated")
        result = MagicMock()
        result.is_error = False
        result.content = [MagicMock(type="text", text="fresh data")]
        result.structured_content = None
        return result

    server.session.call_tool = _sequence
    mcp_tool._servers["srv"] = server
    mcp_tool._server_error_counts.pop("srv", None)
    if read_only:
        mcp_tool._tool_read_only_hints.setdefault("srv", {})["tool"] = True
    # else: no readOnlyHint entry -> fails safe to write-capable.

    try:
        parsed = json.loads(_make_tool_handler("srv", "tool", 10.0)({}))
        assert reconnect_flag.is_set()  # transport healed either way
        if read_only:
            assert parsed.get("result") == "fresh data", parsed
            assert call_count["n"] == 2  # original + one retry
        else:
            assert parsed.get("outcome_uncertain") is True, parsed
            assert call_count["n"] == 1  # exactly one dispatch
            # Successful reconnect clears breaker state (session-state failure, not server health).
            assert mcp_tool._server_error_counts.get("srv", 0) == 0
    finally:
        mcp_tool._servers.pop("srv", None)
        mcp_tool._server_error_counts.pop("srv", None)
        mcp_tool._server_breaker_opened_at.pop("srv", None)
        mcp_tool._tool_read_only_hints.pop("srv", None)


def test_tool_is_read_only_fails_safe():
    """Unknown server/tool metadata classifies as write-capable (False)."""
    from tools import mcp_tool
    from tools.mcp_tool_handlers import _tool_is_read_only

    assert _tool_is_read_only("no-such-server", "tool") is False
    mcp_tool._tool_read_only_hints["known"] = {"reader": True, "writer": False}
    try:
        assert _tool_is_read_only("known", "reader") is True
        assert _tool_is_read_only("known", "writer") is False
        assert _tool_is_read_only("known", "unlisted") is False
    finally:
        mcp_tool._tool_read_only_hints.pop("known", None)


# ---------------------------------------------------------------------------
# A write the server REFUSED with HTTP 404 for its session id never ran (t_90d34dcf: the first tool call
# after every tier3 restart), so it is resent once on the rebuilt session. The JSON-RPC message alone is
# not evidence (a tool can commit, then answer HTTP 200 with the same error: Prism 36d0ba56a234); only the
# transport's 404 for THIS call's POST is. Any other failure of the resend stays at-most-once.
# ---------------------------------------------------------------------------


def _session_refusal(message="Session not found"):
    from mcp.shared.exceptions import MCPError
    return MCPError(-32600, message)


@pytest.mark.parametrize("exc, expected", [
    (_session_refusal(), True),
    (_session_refusal("Session terminated"), True),  # mcp 2.x's stand-in for a bare HTTP 404
    (RuntimeError("Session not found"), False),  # no JSON-RPC reply
    (_session_refusal("Invalid params"), False),
    (ExceptionGroup("g", [_session_refusal()]), False),  # groups are not trusted
])
def test_is_jsonrpc_session_refusal(exc, expected):
    from tools.mcp_tool_errors import _is_jsonrpc_session_refusal
    assert _is_jsonrpc_session_refusal(exc) is expected


def test_bare_jsonrpc_refusal_is_not_pre_dispatch_evidence():
    from tools.mcp_tool_errors import _SessionRejectedBeforeDispatch, _is_session_rejected_before_dispatch
    assert _is_session_rejected_before_dispatch(_session_refusal()) is False
    assert _is_session_rejected_before_dispatch(_SessionRejectedBeforeDispatch("Session not found")) is True


def _http_response(status, body, *, session_id="sid-1", method="POST"):
    from tools.mcp_tool import sdk_httpx
    httpx = sdk_httpx()
    headers = {"mcp-session-id": session_id} if session_id else {}
    request = httpx.Request(method, "http://127.0.0.1:1/mcp", json=body, headers=headers)
    return httpx.Response(status, request=request, json={"jsonrpc": "2.0", "id": 1,
                                                          "error": {"code": -32600, "message": "Session not found"}})


_CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "set_timer", "arguments": {"m": 5}}}


@pytest.mark.parametrize("status, body, session_id, recorded", [
    (404, _CALL, "sid-1", _CALL["params"]),  # the spec's unknown-session reply: recorded
    (200, _CALL, "sid-1", None),  # same JSON-RPC error over 200: the tool may have run
    (404, _CALL, None, None),  # no session id sent: 404 means method-not-found, not a session refusal
    (404, {"jsonrpc": "2.0", "id": 8, "method": "ping"}, "sid-1", None),  # only tools/call counts
    (400, _CALL, "sid-1", None),
])
def test_session_rejection_recorder(status, body, session_id, recorded):
    from types import SimpleNamespace
    from tools.mcp_tool_errors import _make_session_rejection_recorder
    server = SimpleNamespace(_tool_call_session_rejected=None)
    asyncio.run(_make_session_rejection_recorder(server)(_http_response(status, body, session_id=session_id)))
    assert server._tool_call_session_rejected == recorded


def _ok_result(text="fresh data"):
    result = MagicMock()
    result.is_error = False
    result.content = [MagicMock(type="text", text=text)]
    result.structured_content = None
    return result


def _install_write_server(name, behaviour):
    """Stub server whose call_tool runs ``behaviour(n, server, tool, args)`` (n = 1-based send count)."""
    from tools import mcp_tool
    server, reconnect_flag = _install_stub_server(name)
    server._rpc_lock = asyncio.Lock()
    server._inflight_tasks = None
    server._tool_call_session_rejected = None
    calls = {"n": 0}

    async def _call_tool(tool, arguments=None):
        calls["n"] += 1
        return behaviour(calls["n"], server, tool, arguments)

    server.session.call_tool = _call_tool
    mcp_tool._servers[name] = server
    mcp_tool._server_error_counts.pop(name, None)
    return server, reconnect_flag, calls


def _cleanup(name):
    from tools import mcp_tool
    mcp_tool._servers.pop(name, None)
    mcp_tool._server_error_counts.pop(name, None)
    mcp_tool._server_breaker_opened_at.pop(name, None)


def _refuse_with_404(server, tool, args):
    server._tool_call_session_rejected = {"name": tool, "arguments": args}  # what the transport hook records
    raise _session_refusal()


@pytest.mark.parametrize("second, want_calls, want", [
    (None, 2, "result"),  # refused by 404, then resent once on the new session: the tool runs exactly once
    (RuntimeError("Connection closed"), 2, "outcome_uncertain"),  # resend may have run: never a 3rd send
    ("refused", 2, "error"),  # refused by 404 again: nothing ran, plain error, no 3rd send
], ids=["resent-once", "resend-uncertain", "resend-refused"])
def test_write_refused_by_404_is_resent_once(monkeypatch, tmp_path, second, want_calls, want):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool_handlers import _make_tool_handler

    def _behaviour(n, server, tool, args):
        if n == 1 or second == "refused":
            _refuse_with_404(server, tool, args)
        if second is not None:
            raise second
        return _ok_result()

    _server, reconnect_flag, calls = _install_write_server("srv", _behaviour)
    try:  # no readOnlyHint: write-capable
        parsed = json.loads(_make_tool_handler("srv", "tool", 10.0)({"m": 5}))
        assert reconnect_flag.is_set()
        assert calls["n"] == want_calls
        if want == "result":
            assert parsed.get("result") == "fresh data", parsed
        elif want == "outcome_uncertain":
            assert parsed.get("outcome_uncertain") is True, parsed
            assert "NOT automatically retried" not in parsed["error"]  # it WAS resent (Prism 9820a365b56b)
            assert "resent once" in parsed["error"]
        else:
            assert "error" in parsed and not parsed.get("outcome_uncertain"), parsed
    finally:
        _cleanup("srv")


def test_write_that_ran_then_answered_session_not_found_is_not_resent(monkeypatch, tmp_path):
    """Prism 36d0ba56a234 repro: the tool commits (counter 1), then answers HTTP 200 with JSON-RPC -32600
    "Session not found". No 404 was recorded, so the write is never resent: counter stays 1, outcome_uncertain."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool_handlers import _make_tool_handler
    timers = {"n": 0}

    def _behaviour(n, server, tool, args):
        timers["n"] += 1  # the write landed
        raise _session_refusal()  # ...and the reply looks like a session refusal, over HTTP 200

    _server, _flag, calls = _install_write_server("srv", _behaviour)
    try:
        parsed = json.loads(_make_tool_handler("srv", "set_timer", 10.0)({"m": 5}))
        assert timers["n"] == 1 and calls["n"] == 1
        assert parsed.get("outcome_uncertain") is True, parsed
    finally:
        _cleanup("srv")


def test_404_recorded_for_another_call_does_not_license_a_resend(monkeypatch, tmp_path):
    """Two concurrent write calls on one server: A is refused by 404, B ran and answered 200 with the same
    error. Only A is resent; B (whose recorded 404 entry is not its own) stays outcome_uncertain and is sent once."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool_handlers import _make_tool_handler
    sends = {"A": 0, "B": 0}
    ran = {"A": 0, "B": 0}

    def _behaviour(n, server, tool, args):
        who = args["who"]
        sends[who] += 1
        if who == "A" and sends["A"] == 1:
            _refuse_with_404(server, tool, args)
        ran[who] += 1
        if who == "B":
            # B's POST got 200; the last 404 the transport saw is A's, still in the sink.
            server._tool_call_session_rejected = {"name": tool, "arguments": {"who": "A"}}
            raise _session_refusal()
        return _ok_result()

    _server, _flag, _calls = _install_write_server("srv", _behaviour)
    try:
        handler = _make_tool_handler("srv", "set_timer", 10.0)
        out = {}
        threads = [threading.Thread(target=lambda w=w: out.__setitem__(w, json.loads(handler({"who": w}))))
                   for w in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert out["A"].get("result") == "fresh data", out["A"]
        assert sends["A"] == 2 and ran["A"] == 1
        assert out["B"].get("outcome_uncertain") is True, out["B"]
        assert sends["B"] == 1 and ran["B"] == 1
    finally:
        _cleanup("srv")


def test_owned_streamable_client_records_a_404_for_its_own_server(monkeypatch):
    """The owned Streamable HTTP client that ``_streamable_http_transport`` builds carries the 404 recorder
    for THIS server (round-3 send-back item 1: the other tests set the record by hand)."""
    from tools import mcp_tool_transport
    from tools.mcp_tool import MCPServerTask, sdk_httpx

    httpx = sdk_httpx()
    seen = {}

    def _fake_stripper(*_a, **_k):
        def _build_client(**client_kwargs):
            seen.update(client_kwargs)
            raise AssertionError("the client is never built in this test")
        return _build_client

    monkeypatch.setattr(mcp_tool_transport, "_make_redirect_header_stripper", _fake_stripper)
    task = MCPServerTask("srv")
    cm = task._streamable_http_transport("http://127.0.0.1:1/mcp", {}, 5.0, True, None, None, False, set())

    async def _enter():
        try:
            async with cm:
                pass
        except AssertionError:
            pass

    asyncio.run(_enter())
    hooks = seen["event_hooks"]["response"]
    response = _http_response(404, _CALL)

    async def _fire():
        for hook in hooks:
            await hook(response)

    asyncio.run(_fire())
    assert task._tool_call_session_rejected == _CALL["params"]


def test_404_left_from_an_earlier_call_does_not_license_a_later_identical_call(monkeypatch, tmp_path):
    """Item 2: call A is refused by 404 and resent; then an identical call B runs and answers HTTP 200 with
    "Session not found". A's old record must not license resending B: B runs once, outcome_uncertain."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool_handlers import _make_tool_handler
    ran = {"n": 0}

    def _behaviour(n, server, tool, args):
        if n == 1:
            _refuse_with_404(server, tool, args)  # A's first send: refused, nothing ran
        ran["n"] += 1
        if n == 2:
            return _ok_result()  # A's resend
        raise _session_refusal()  # B (and any resend of B): the write ran, then the tool raised

    _server, _flag, calls = _install_write_server("srv", _behaviour)
    try:
        handler = _make_tool_handler("srv", "set_timer", 10.0)
        assert json.loads(handler({"m": 5})).get("result") == "fresh data"
        assert calls["n"] == 2 and ran["n"] == 1
        parsed = json.loads(handler({"m": 5}))
        assert calls["n"] == 3 and ran["n"] == 2, "B was resent"
        assert parsed.get("outcome_uncertain") is True, parsed
    finally:
        _cleanup("srv")


def test_404_with_a_reply_that_is_not_a_session_refusal_is_not_resent(monkeypatch, tmp_path):
    """Item 3: the transport saw a 404 for this POST, but the reply the call raised is not a session
    refusal (e.g. a proxy or a server that answers 404 with some other error). Not proven pre-dispatch:
    sent once, outcome_uncertain."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.exceptions import MCPError
    from tools.mcp_tool_handlers import _make_tool_handler

    def _behaviour(n, server, tool, args):
        server._tool_call_session_rejected = {"name": tool, "arguments": args}
        raise MCPError(-32603, "Internal error: session not found in tenant table")

    _server, _flag, calls = _install_write_server("srv", _behaviour)
    try:
        parsed = json.loads(_make_tool_handler("srv", "set_timer", 10.0)({"m": 5}))
        assert calls["n"] == 1
        assert "result" not in parsed, parsed
    finally:
        _cleanup("srv")


def test_late_404_for_a_timed_out_call_does_not_license_resending_an_identical_call(monkeypatch, tmp_path):
    """Prism 6260596e067d: call A times out while its POST is still pending; an identical call B runs, commits
    and answers HTTP 200 with "Session not found"; A's late 404 lands in the record after B cleared it.
    B must not be resent: it runs once and returns outcome_uncertain."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool_handlers import _make_tool_handler
    ran = {"B": 0}
    sends = {"n": 0}

    server, _flag, _calls = _install_write_server("srv", lambda *a: None)

    async def _call_tool(tool, arguments=None):
        sends["n"] += 1
        if sends["n"] == 1:  # A: the server never answers in time
            await asyncio.sleep(30)
        ran["B"] += 1  # B: the write lands...
        server._tool_call_session_rejected = {"name": tool, "arguments": arguments}  # ...A's late 404 arrives
        raise _session_refusal()  # ...and B's own reply is the tool's "Session not found" over HTTP 200

    server.session.call_tool = _call_tool
    server._reconnecting = False  # the timeout is a plain cancel, not a teardown
    try:
        handler = _make_tool_handler("srv", "set_timer", 0.5)
        first = json.loads(handler({"m": 5}))
        assert "result" not in first, first
        parsed = json.loads(handler({"m": 5}))
        assert ran["B"] == 1 and sends["n"] == 2, "B was resent"
        assert parsed.get("outcome_uncertain") is True, parsed
    finally:
        _cleanup("srv")
