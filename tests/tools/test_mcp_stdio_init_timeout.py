"""Regression test for the stdio-MCP subprocess/FD leak (#59349).

A stdio MCP server that never completes ``initialize`` (e.g. emits a
non-JSON-RPC frame and then blocks on stdin) used to hang ``_run_stdio``
forever on the background event loop: ``connect_timeout`` bounded only the
*caller's* ``.result()`` wait, not the coroutine itself. Because the connect
never unwound, the cleanup ``finally`` in ``_run_stdio`` never ran, so the
spawned child process and its stdio pipes / pidfd leaked on *every* discovery
retry — unbounded, until the gateway hit EMFILE.

The fix wraps ``session.initialize()`` in
``asyncio.wait_for(..., timeout=connect_timeout)`` so a stalled handshake fails
instead of hanging, which lets the existing ``finally`` reap the child.

This test drives the *real* ``_run_stdio`` with a fake transport whose
``initialize()`` hangs, and asserts the connect is bounded by
``connect_timeout`` rather than blocking forever. It is fully hermetic — no real
subprocess, no network (the drain-to-zero behaviour was additionally verified
manually against the reporter's live repro).

The bound is asserted as an **ordering fact**, not a stopwatch reading: the
coroutine must unwind *on its own* (via the inner ``connect_timeout``) rather
than only when the test's outer hang-guard gives up on it. The previous form
(``assert elapsed < 2.0``) made the OS scheduler part of the assertion and
failed on a loaded box with nothing wrong in the code under test. Profiling the
measured window showed **2.34s of the 2.35s was ``_snapshot_child_pids``** (a
``ps``-based child scan run before the handshake) — the threshold was dominated
by setup cost it was never meant to measure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")


class _HangingSession:
    """Stand-in ClientSession whose handshake never completes."""

    def __init__(self):
        # Set only when the handshake is torn down by a cancellation — which
        # is exactly what an ``asyncio.wait_for`` timeout does to the
        # coroutine it wraps. This is the witness that the bound was applied
        # *here*, rather than the whole connect merely being abandoned.
        self.handshake_cancelled = False

    async def initialize(self):
        try:
            await asyncio.Event().wait()  # never set — a genuine hang
        except asyncio.CancelledError:
            self.handshake_cancelled = True
            raise


class _FakeAsyncCM:
    """Minimal async context manager yielding a fixed value; spawns nothing."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_exc):
        return False


def _fake_stdio_client(*_args, **_kwargs):
    # `async with stdio_client(...) as (read, write)` — no subprocess spawned.
    return _FakeAsyncCM((object(), object()))


def _make_fake_client_session(session):
    def _fake_client_session(*_args, **_kwargs):
        # `async with ClientSession(...) as session` -> a session that hangs.
        return _FakeAsyncCM(session)

    return _fake_client_session


class TestStdioInitializeTimeout:
    def test_hanging_initialize_is_bounded_not_leaked(self):
        """A stdio server that hangs at ``initialize`` must fail within
        ``connect_timeout`` — not block ``_run_stdio`` forever (#59349)."""
        from tools import mcp_tool

        server = mcp_tool.MCPServerTask("leak-guard")
        config = {"command": "fake-mcp", "args": [], "connect_timeout": 0.2}

        async def drive():
            session = _HangingSession()
            with patch.object(mcp_tool, "stdio_client", _fake_stdio_client), \
                 patch.object(mcp_tool, "ClientSession",
                              _make_fake_client_session(session)), \
                 patch.object(mcp_tool, "_resolve_stdio_command", lambda c, e: (c, e)), \
                 patch.object(mcp_tool, "_write_stderr_log_header", lambda *_a, **_k: None), \
                 patch.object(mcp_tool, "_get_mcp_stderr_log", lambda: None), \
                 patch("tools.osv_check.check_package_for_malware",
                       lambda *_a, **_k: None):
                task = asyncio.ensure_future(server._run_stdio(config))
                # ORDERING WITNESS (not a stopwatch): with the fix,
                # ``_run_stdio`` unwinds BY ITSELF via the inner
                # ``connect_timeout``. Without it the coroutine hangs forever
                # and is still pending when this bounded wait returns. The 10s
                # is a hang-guard 50x the 0.2s connect_timeout so a regression
                # fails fast instead of wedging the suite — it is not what is
                # being asserted, and no realistic scheduling delay approaches
                # it.
                done, pending = await asyncio.wait({task}, timeout=10.0)
                for stuck in pending:
                    stuck.cancel()
                    try:
                        await stuck
                    except (asyncio.CancelledError, Exception):
                        pass
                return task, done, session

        async def check():
            task, done, session = await drive()
            assert task in done, (
                "_run_stdio never unwound on its own — the connect_timeout "
                "bound was not applied to session.initialize(); the #59349 "
                "subprocess/FD leak has regressed."
            )
            # It unwound because the handshake TIMED OUT, not because the
            # connect somehow succeeded or failed for an unrelated reason.
            with pytest.raises(asyncio.TimeoutError):
                task.result()
            assert session.handshake_cancelled, (
                "the hanging initialize() was never torn down — "
                "connect_timeout did not wrap the handshake"
            )

        asyncio.run(check())
