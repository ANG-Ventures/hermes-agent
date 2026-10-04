"""Tests for tui_gateway inline-RPC pool routing under GIL pressure (#50005).

The WS read loop in ``handle_ws()`` processes requests sequentially via
``await asyncio.to_thread(server.dispatch, req, transport)``. Inline handlers
(NOT in ``_LONG_HANDLERS``) run ``handle_request()`` synchronously inside
``dispatch()``, blocking the loop from reading the next request. Under GIL
pressure from multiple concurrent agent turns, even lightweight RPCs like
``session.list`` and ``pet.info`` can take seconds, causing frontend requests
to time out (120s) and the WebSocket to disconnect — the false "needs setup"
failure mode (#50005).

The fix routes all frontend-polled RPCs through ``_LONG_HANDLERS`` so
``dispatch()`` returns immediately (``_pool.submit`` + ``return None``) and
the WS read loop is never blocked.
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only — keeping them active for
    # the whole test would poison modules first imported inside test bodies
    # (see tests/tui_gateway/test_protocol.py for the full rationale).
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")

    # Tests below stub handlers ("session.list", "prompt.submit", ...) in
    # the module-level _methods dict shared with every other test file in
    # the process — snapshot and restore it around each test.
    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    mod._sessions.clear()
    __import__("tui_gateway.server_requests", fromlist=["x"]).reset_for_tests()


def test_dispatch_inline_rpc_does_not_block_under_gil_pressure(server, monkeypatch):
    """A slow inline-turned-long handler must not prevent a concurrent fast
    handler from completing. This is the core invariant: dispatch() must
    return immediately for _LONG_HANDLERS so the WS read loop stays free.

    Simulates the GIL-pressure scenario from #50005: a slow handler (mimicking
    a session.list query under GIL contention) must not block a fast handler
    (mimicking setup.runtime_check).
    """
    released = threading.Event()
    slow_returned = threading.Event()

    def slow_session_list(rid, params):
        try:
            released.wait(timeout=30)
            return server._ok(rid, {"sessions": []})
        finally:
            slow_returned.set()

    monkeypatch.setitem(server._methods, "session.list", slow_session_list)
    monkeypatch.setitem(server._methods, "fast.check", lambda rid, params: server._ok(rid, {"ok": True}))

    # session.list is in _LONG_HANDLERS → dispatch returns None immediately
    assert server.dispatch({"id": "slow", "method": "session.list", "params": {}}) is None

    # fast.check is inline → dispatch runs it synchronously and returns the result
    fast_resp = server.dispatch({"id": "fast", "method": "fast.check", "params": {}})

    assert fast_resp["result"] == {"ok": True}
    # Ordering witness (replaces `fast_elapsed < 2.0`): the slow handler is
    # still parked when the fast one has already answered, i.e. fast.check
    # overtook it.  This test deliberately induces GIL pressure, which makes
    # its own stopwatch the least trustworthy instrument in the repo — the
    # overtake, not the duration, is the #50005 contract.
    assert not slow_returned.is_set(), (
        "fast handler was serialized behind the slow session.list — "
        "the WS read loop would stall, causing false 'needs setup' (#50005)."
    )

    released.set()
