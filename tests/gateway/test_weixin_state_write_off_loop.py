"""The Weixin adapter's durable JSON writes must not block the event loop.

Three writes in ``gateway/platforms/weixin.py`` end in the mkstemp + fsync +
``os.replace`` tail of ``utils.atomic_json_write``, whose duration is unbounded
under filesystem pressure, and all three are driven from coroutines:

  * ``ContextTokenStore._persist``  <- ``_process_message``  (PER INBOUND MESSAGE)
  * ``_save_sync_buf``              <- ``_poll_loop``        (long-poll cursor)
  * ``save_weixin_account``         <- ``qr_login``          (QR confirm)

On a loop thread that rename stalls every other task in the process -- the
2026-09-20 incident class.

Note the first one is INVISIBLE to the reachability ratchet and always was:
``tests/gateway/_loop_atomic_write_reachability.py`` keys its index on
``(file, function_name)``, so ``ContextTokenStore.set`` (line ~333) and
``TypingTicketCache.set`` (line ~364) collapse onto one entry and the DFS reads
the second one's call set.  Measured on the pre-fix tree: the index entry for
``('gateway/platforms/weixin.py', 'set')`` reports ``calls={'time.time','time'}``
-- the typing cache's body, not the token store's.  That is why this file
asserts on the hot path directly instead of trusting the ratchet to have
frozen it.

No wall-clock thresholds anywhere.  The rename is held open on a barrier that
ONLY the assertion path releases, which gives a deterministic oracle on the
resource rather than on timing:

  * off-loop -- the coroutine's call returns while the rename is still held,
    so the durable file provably does NOT exist yet when control comes back,
    and the loop can still run a sibling task.
  * inline (pre-fix) -- the call cannot return until ``os.replace`` has run,
    so the file DOES exist on return.  Every "does not block" test below
    therefore fails on the pre-fix shape; each has a paired gate-proof test
    that forces the inline branch and asserts exactly that.

An earlier draft of this file waited on the sibling task with a timeout
instead, and passed on the pre-fix shape too -- the timeout only starts
counting after the inline block has already returned.  Asserting on the file
is what makes these tests real.
"""
import asyncio
import json
import os
import threading

import pytest

from gateway.platforms import weixin


@pytest.fixture()
def weixin_home(tmp_path):
    """An isolated HERMES_HOME for the account/cursor/token files."""
    return tmp_path


@pytest.fixture(autouse=True)
def _drain_lane_between_tests():
    """No test may leave work queued for the next one."""
    yield
    weixin.fence_weixin_write_lane(timeout=10.0)


class _HeldReplace:
    """Replace ``os.replace`` with one that blocks until released.

    A watchdog thread releases the gate after *watchdog* seconds so the
    PRE-FIX inline shape cannot hang the suite: the inline call returns late,
    the assertion sees the file already written, and the test fails with a
    readable message instead of deadlocking.  The watchdog delay never gates
    a PASS -- on the fixed shape the call returns immediately and the
    assertion runs long before the watchdog fires.
    """

    def __init__(self, monkeypatch, watchdog=3.0):
        self._gate = threading.Event()
        self._entered = threading.Event()
        self._real = os.replace
        self._watchdog = threading.Timer(watchdog, self._gate.set)
        self._watchdog.daemon = True
        self._watchdog.start()
        monkeypatch.setattr(os, "replace", self._blocking, raising=True)

    def _blocking(self, src, dst, *a, **kw):
        self._entered.set()
        self._gate.wait()
        return self._real(src, dst, *a, **kw)

    def wait_until_entered(self, timeout=5.0):
        return self._entered.wait(timeout)

    def release(self):
        self._watchdog.cancel()
        self._gate.set()


def _token_path(home, account_id="acct"):
    return home / "weixin" / "accounts" / f"{account_id}.context-tokens.json"


def _cursor_path(home, account_id="acct"):
    return weixin._sync_buf_path(str(home), account_id)


def _account_path(home, account_id="acct"):
    return home / "weixin" / "accounts" / f"{account_id}.json"


# ---------------------------------------------------------------------------
# The per-inbound-message path (ratchet-invisible; asserted directly)
# ---------------------------------------------------------------------------


def test_context_token_persist_does_not_block_the_loop(weixin_home, monkeypatch):
    """A stalled rename must not stop the loop from running other tasks.

    The oracle is the RESOURCE: with the rename held open, the call must have
    returned before the file exists.  On the pre-fix inline shape the call
    cannot return until the rename has run, so the file would already be there.
    """

    async def scenario():
        store = weixin.ContextTokenStore(str(weixin_home))
        held = _HeldReplace(monkeypatch)
        ticked = asyncio.Event()

        async def sibling():
            await asyncio.sleep(0)
            ticked.set()

        sibling_task = asyncio.create_task(sibling())
        try:
            # The call every inbound message makes.
            store.set("acct", "wxid_peer", "ctx-token-1")

            assert held.wait_until_entered(), "the write never reached os.replace"
            assert not _token_path(weixin_home).exists(), (
                "the call did not return until the held rename completed -- "
                "the write is still inline on the loop"
            )
            # ...and the loop is genuinely free to run other work.
            await asyncio.wait_for(ticked.wait(), timeout=5.0)
        finally:
            held.release()

        await sibling_task
        # Durability is preserved: the payload lands once the write completes.
        assert await asyncio.to_thread(weixin.fence_weixin_write_lane, 10.0)
        assert json.loads(_token_path(weixin_home).read_text(encoding="utf-8")) == {
            "wxid_peer": "ctx-token-1"
        }

    asyncio.run(scenario())


def test_gate_proof_inline_context_token_persist_does_block_the_loop(
    weixin_home, monkeypatch
):
    """The test above is not vacuous.

    Force the loop-conditional dispatch to choose the inline branch -- the
    pre-fix shape -- and the very same barrier now blocks the caller until the
    rename lands, which is precisely what the test above forbids.
    """
    monkeypatch.setattr(weixin, "_loop_is_running", lambda: False, raising=True)

    async def scenario():
        store = weixin.ContextTokenStore(str(weixin_home))
        held = _HeldReplace(monkeypatch)
        ticked = asyncio.Event()

        async def sibling():
            await asyncio.sleep(0)
            ticked.set()

        asyncio.create_task(sibling())
        # The inline call cannot release itself; _HeldReplace's watchdog does.
        store.set("acct", "wxid_peer", "ctx-token-1")

        assert _token_path(weixin_home).exists(), (
            "the barrier is not actually holding the rename, so the sibling "
            "test proves nothing"
        )
        assert not ticked.is_set(), (
            "the loop ran a sibling task while the write was inline"
        )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The long-poll cursor
# ---------------------------------------------------------------------------


def test_sync_buf_save_does_not_block_the_loop(weixin_home, monkeypatch):
    """``_poll_loop``'s cursor write must not stall the loop."""

    async def scenario():
        held = _HeldReplace(monkeypatch)
        ticked = asyncio.Event()

        async def sibling():
            await asyncio.sleep(0)
            ticked.set()

        sibling_task = asyncio.create_task(sibling())
        try:
            weixin._save_sync_buf(str(weixin_home), "acct", "cursor-1")

            assert held.wait_until_entered(), "the write never reached os.replace"
            assert not _cursor_path(weixin_home).exists(), (
                "the cursor write is still inline on the loop"
            )
            await asyncio.wait_for(ticked.wait(), timeout=5.0)
        finally:
            held.release()

        await sibling_task
        assert await asyncio.to_thread(weixin.fence_weixin_write_lane, 10.0)
        assert weixin._load_sync_buf(str(weixin_home), "acct") == "cursor-1"

    asyncio.run(scenario())


def test_gate_proof_inline_sync_buf_save_does_block_the_loop(
    weixin_home, monkeypatch
):
    """Pin the cursor test non-vacuous on the pre-fix inline shape."""
    monkeypatch.setattr(weixin, "_loop_is_running", lambda: False, raising=True)

    async def scenario():
        held = _HeldReplace(monkeypatch)
        weixin._save_sync_buf(str(weixin_home), "acct", "cursor-1")
        assert _cursor_path(weixin_home).exists(), (
            "the barrier is not actually holding the cursor rename"
        )

    asyncio.run(scenario())


def test_cursor_writes_land_in_call_order(weixin_home):
    """Last-writer-wins must follow CALL order, not thread scheduling.

    A pool would let two renames of the same path race and durably persist an
    older cursor, which re-delivers messages the poll already consumed.  One
    FIFO worker makes on-disk order identical to call order.
    """

    async def scenario():
        for i in range(10):
            weixin._save_sync_buf(str(weixin_home), "acct", f"cursor-{i}")

    asyncio.run(scenario())
    assert weixin.fence_weixin_write_lane(timeout=10.0)
    assert weixin._load_sync_buf(str(weixin_home), "acct") == "cursor-9"


def test_disconnect_fences_the_lane_so_the_cursor_is_not_lost(
    weixin_home, monkeypatch
):
    """A queued cursor write must land before teardown returns.

    Otherwise a reconnect re-polls from a stale cursor and re-delivers
    messages that were already consumed.
    """

    async def scenario():
        held = _HeldReplace(monkeypatch)
        weixin._save_sync_buf(str(weixin_home), "acct", "cursor-final")
        assert held.wait_until_entered()
        assert not _cursor_path(weixin_home).exists(), (
            "the write was not queued; this test cannot prove the fence waits"
        )
        # Release on another thread so the fence genuinely has to wait.
        threading.Timer(0.2, held.release).start()
        # The exact fence disconnect() performs.
        assert await asyncio.to_thread(weixin.fence_weixin_write_lane, 10.0)
        assert weixin._load_sync_buf(str(weixin_home), "acct") == "cursor-final", (
            "the fence returned before the queued cursor write landed"
        )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Credentials, and the synchronous contract for non-loop callers
# ---------------------------------------------------------------------------


def test_qr_login_account_save_does_not_block_the_loop(weixin_home, monkeypatch):
    """``qr_login``'s credential write must not stall the loop."""

    async def scenario():
        held = _HeldReplace(monkeypatch)
        ticked = asyncio.Event()

        async def sibling():
            await asyncio.sleep(0)
            ticked.set()

        sibling_task = asyncio.create_task(sibling())
        try:
            weixin.save_weixin_account(
                str(weixin_home),
                account_id="acct",
                token="tok",
                base_url="https://example.invalid",
                user_id="wxid_self",
            )

            assert held.wait_until_entered(), "the write never reached os.replace"
            assert not _account_path(weixin_home).exists(), (
                "the credential write is still inline on the loop"
            )
            await asyncio.wait_for(ticked.wait(), timeout=5.0)
        finally:
            held.release()

        await sibling_task
        assert await asyncio.to_thread(weixin.fence_weixin_write_lane, 10.0)
        loaded = weixin.load_weixin_account(str(weixin_home), "acct")
        assert loaded is not None and loaded["token"] == "tok"

    asyncio.run(scenario())


def test_gate_proof_inline_account_save_does_block_the_loop(
    weixin_home, monkeypatch
):
    """Pin the credential test non-vacuous on the pre-fix inline shape."""
    monkeypatch.setattr(weixin, "_loop_is_running", lambda: False, raising=True)

    async def scenario():
        held = _HeldReplace(monkeypatch)
        weixin.save_weixin_account(
            str(weixin_home),
            account_id="acct",
            token="tok",
            base_url="https://example.invalid",
        )
        assert _account_path(weixin_home).exists(), (
            "the barrier is not actually holding the credential rename"
        )

    asyncio.run(scenario())


def test_credential_file_is_still_0600_when_written_on_the_lane(weixin_home):
    """The 0600 mode must not be lost by moving the write off the loop.

    The payload carries the bot token, so the chmod rides WITH the write
    rather than being applied by the (now non-blocking) caller.
    """

    async def scenario():
        weixin.save_weixin_account(
            str(weixin_home),
            account_id="acct",
            token="tok",
            base_url="https://example.invalid",
        )

    asyncio.run(scenario())
    assert weixin.fence_weixin_write_lane(timeout=10.0)
    path = weixin_home / "weixin" / "accounts" / "acct.json"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_non_loop_callers_keep_the_synchronous_contract(weixin_home):
    """The setup wizard and CLI helpers still get a completed write."""
    weixin.save_weixin_account(
        str(weixin_home),
        account_id="acct",
        token="tok",
        base_url="https://example.invalid",
    )
    # No fence: with no running loop the write already happened inline.
    path = weixin_home / "weixin" / "accounts" / "acct.json"
    assert path.exists(), "a non-loop caller must get a real written file"
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "tok"


def test_non_loop_callers_still_see_write_failures(weixin_home, monkeypatch):
    """Error propagation for sync callers must survive the refactor.

    ``test_weixin.py::test_save_weixin_account_preserves_existing_file_on_replace_failure``
    depends on this: off the loop, an ``OSError`` from the rename must still
    reach the caller rather than being swallowed by a lane.
    """

    def _boom(_src, _dst, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("utils.os.replace", _boom, raising=True)

    with pytest.raises(OSError):
        weixin.save_weixin_account(
            str(weixin_home),
            account_id="acct",
            token="tok",
            base_url="https://example.invalid",
        )


def test_lane_write_failure_does_not_escape_to_the_caller(
    weixin_home, monkeypatch
):
    """A lane failure must never break an inbound message turn."""

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(weixin, "_atomic_json_write_now", _boom, raising=True)

    async def scenario():
        store = weixin.ContextTokenStore(str(weixin_home))
        store.set("acct", "wxid_peer", "ctx-token-1")
        assert await asyncio.to_thread(weixin.fence_weixin_write_lane, 10.0), (
            "a failing lane write must still publish its completion, or the "
            "fence hangs for its full timeout"
        )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Durability across the gateway's OWN exit path.
# ---------------------------------------------------------------------------
#
# The lane keeps an unbounded rename off the loop, but it also means a write
# can be QUEUED when the process exits.  ``atexit`` does not cover that:
# ``gateway.run._exit_after_graceful_shutdown`` ends in ``os._exit`` (#53107)
# and hand-rolls every atexit replacement it needs.  Measured through that
# funnel with the write still queued, payload files on disk after process
# death were 0 at every hold >= 0.5s, against 1/1 on a ``sys.exit`` control --
# so the lane registers with ``shutdown_flush.register_hard_exit_fence`` and
# this drives a REAL child process through the production funnel to prove it.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))

_WEIXIN_EXIT_CHILD = r'''
import os, sys, threading
from pathlib import Path

out = Path(sys.argv[1])
hold = float(sys.argv[2])
arm = sys.argv[3]

from gateway.platforms import weixin

release = threading.Event()
occupied = threading.Event()

def _occupy():
    occupied.set()
    release.wait(60.0)

# Occupy the single lane worker so the real write is genuinely QUEUED behind
# it -- the only window an exit fence exists for.
weixin._get_weixin_write_lane().submit(_occupy)
assert occupied.wait(5.0), "lane occupant never started"

weixin._submit_weixin_write(out / "cursor.json", {"get_updates_buf": "important"})
print("FILES_AT_EXIT_CALL:", len(list(out.glob("*.json"))), flush=True)

# Release only AFTER the exit call is under way.
threading.Timer(hold, release.set).start()

if arm == "production":
    from gateway.run import _exit_after_graceful_shutdown
    _exit_after_graceful_shutdown(0)
else:
    sys.exit(0)
'''


@pytest.mark.parametrize("arm", ["production", "sysexit"])
def test_a_queued_lane_write_survives_the_gateway_exit_path(tmp_path, arm):
    """A queued cursor/credential write must not die with the process.

    Both arms, because the ``sys.exit`` arm is the CONTROL: it is the path
    where ``atexit`` does fire, so it passed even before the registration.
    Only the ``production`` arm (``os._exit``) distinguishes a wired fence
    from an unwired one -- an in-process ``fence_weixin_write_lane()`` call
    cannot, since pytest itself exits via ``sys.exit``.
    """
    import subprocess
    import sys as _sys

    child = tmp_path / "exit_child.py"
    child.write_text(_WEIXIN_EXIT_CHILD)
    out = tmp_path / "out"
    out.mkdir()

    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    proc = subprocess.run(
        [_sys.executable, str(child), str(out), "1.0", arm],
        capture_output=True, text=True, timeout=180, env=env, cwd=_REPO_ROOT,
    )

    assert "FILES_AT_EXIT_CALL: 0" in proc.stdout, (
        "the write was not actually queued at the exit call, so this test "
        f"proves nothing. stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )
    landed = sorted(p.name for p in out.glob("*.json"))
    assert landed == ["cursor.json"], (
        f"[{arm}] the queued weixin write did not survive process exit: "
        f"{landed}. The gateway's funnel uses os._exit, which never runs the "
        "atexit fence -- the lane must register with "
        "shutdown_flush.register_hard_exit_fence. "
        f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )
