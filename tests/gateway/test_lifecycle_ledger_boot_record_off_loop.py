"""The boot lifecycle record must not block the event loop.

``gateway/run.py`` awaits ``lifecycle_ledger.record_startup_async()`` during
gateway boot, after the PID-file claim and before MCP discovery.  Every step
of that record is blocking:

* ``detect_unclean_exit`` reads the sentinel + the loop heartbeat and probes
  the prior pid through psutil;
* ``attribute_unclean_exit`` shells out to ``log show`` / ``journalctl`` for
  up to ``KILL_ATTRIBUTION_TIMEOUT_S`` (10s);
* ``_emit_unclean_report`` appends to ``gateway-exit-diag.log``;
* ``_claim_sentinel`` ends in ``utils.atomic_json_write`` — ``mkstemp`` +
  ``fsync`` + ``os.replace``, unbounded under filesystem pressure.

The shape before this change offloaded ONLY the attribution probe, so the
remaining three ran on the loop thread.  Measured on that shape with
``os.replace`` held open, the loop advanced a sibling ticker **0** times
while the rename was in flight; with the whole body offloaded it advanced
45 times.  That is the 2026-09-20 incident class exactly — a rename several
plain-``def`` frames below a coroutine stalling every other task, including
the platform heartbeats this boot path runs alongside.

These tests pin the fix without wall-clock thresholds: the rename is held on
a real barrier and the loop must make progress anyway.  They also pin the
properties the offload must not trade away — the return value, the durable
sentinel contents, and the unclean-exit evidence path.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from gateway import lifecycle_ledger


@pytest.fixture()
def ledger_home(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _clean_prior_life(home: Path) -> None:
    """A previous life that exited cleanly: no probe, no report — the only
    blocking work left is the sentinel write itself."""
    lifecycle_ledger.get_lifecycle_sentinel_path(home).write_text(
        json.dumps({"phase": "exited", "pid": 1, "exit_code": 0}),
        encoding="utf-8",
    )


def _unclean_prior_life(home: Path) -> None:
    """A previous life that died uncleanly: drives detect + probe + report."""
    lifecycle_ledger.get_lifecycle_sentinel_path(home).write_text(
        json.dumps({"phase": "running", "pid": 999999, "start_time": 1.0}),
        encoding="utf-8",
    )


class _HeldReplace:
    """Replace ``os.replace`` with one that blocks until released."""

    def __init__(self, monkeypatch):
        self._gate = threading.Event()
        self._entered = threading.Event()
        self._real = os.replace
        monkeypatch.setattr(os, "replace", self._blocking, raising=True)

    def _blocking(self, src, dst, *a, **kw):
        self._entered.set()
        self._gate.wait(timeout=10.0)
        return self._real(src, dst, *a, **kw)

    def wait_until_entered(self, timeout: float = 5.0) -> bool:
        return self._entered.wait(timeout)

    def release(self) -> None:
        self._gate.set()


async def _ticks_while_held(home: Path, held: _HeldReplace, *, call) -> tuple:
    """Run ``call`` while a sibling ticker counts loop iterations."""
    ticks = 0
    running = True

    async def ticker() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    before = ticks

    async def release_when_entered() -> None:
        # Release from a helper task so the assertion never deadlocks: the
        # point is that the loop can still RUN this task while the rename
        # is held, which is precisely what the pre-fix shape cannot do.
        await asyncio.to_thread(held.wait_until_entered, 5.0)
        await asyncio.sleep(0.3)
        held.release()

    releaser = asyncio.create_task(release_when_entered())
    result = await call()
    during = ticks - before
    running = False
    await ticker_task
    await releaser
    assert held.wait_until_entered(0.1), "the write never reached os.replace"
    return during, result


def test_boot_record_does_not_block_the_loop(ledger_home, monkeypatch):
    """A stalled sentinel rename must not stop the loop running other tasks.

    No stopwatch: the rename is held on a barrier for as long as the
    assertion needs, and the loop must still advance a sibling task.
    """
    _clean_prior_life(ledger_home)

    async def scenario():
        held = _HeldReplace(monkeypatch)
        during, evidence = await _ticks_while_held(
            ledger_home,
            held,
            call=lambda: lifecycle_ledger.record_startup_async(home=ledger_home),
        )
        assert evidence is None, "a clean prior life yields no unclean evidence"
        assert during > 5, (
            "the loop advanced only %d times while the sentinel rename was "
            "held — the boot record is still on the loop thread" % during
        )
        # Durability is preserved: the sentinel lands, claimed by THIS pid.
        sentinel = json.loads(
            lifecycle_ledger.get_lifecycle_sentinel_path(ledger_home).read_text(
                encoding="utf-8"
            )
        )
        assert sentinel["phase"] == "running"
        assert sentinel["pid"] == os.getpid()
        # And the prior clean exit is still carried forward.
        assert sentinel["prior_phase"] == "exited"
        assert sentinel["prior_exit_code"] == 0

    asyncio.run(scenario())


def test_gate_proof_the_pre_fix_shape_does_block_the_loop(ledger_home, monkeypatch):
    """The test above is not vacuous.

    Reconstruct the pre-fix shape — only the attribution probe offloaded,
    the sentinel write inline on the loop — and the very same barrier now
    starves the loop.
    """
    _clean_prior_life(ledger_home)

    async def pre_fix_record_startup_async(home=None):
        evidence = lifecycle_ledger.detect_unclean_exit(home)
        if evidence is not None:
            attribution = await asyncio.to_thread(
                lifecycle_ledger._probe_attribution, evidence
            )
            lifecycle_ledger._apply_attribution(evidence, attribution)
            lifecycle_ledger._emit_unclean_report(evidence, home)
        lifecycle_ledger._claim_sentinel(evidence, home)
        return evidence

    async def scenario():
        held = _HeldReplace(monkeypatch)
        during, _ = await _ticks_while_held(
            ledger_home,
            held,
            call=lambda: pre_fix_record_startup_async(home=ledger_home),
        )
        assert during == 0, (
            "expected the inline shape to starve the loop completely, but it "
            "ticked %d times — the barrier is not actually holding" % during
        )

    asyncio.run(scenario())


def test_the_whole_blocking_body_runs_on_one_worker_thread(ledger_home, monkeypatch):
    """Not just the probe: detection, the report append and the sentinel
    write must all leave the loop thread — and share ONE worker, so the
    boot record costs a single hop rather than four."""
    _unclean_prior_life(ledger_home)

    threads: dict[str, set] = {
        "detect": set(),
        "probe": set(),
        "report": set(),
        "claim": set(),
    }

    def _spy(name, real):
        def wrapper(*a, **kw):
            threads[name].add(threading.current_thread())
            return real(*a, **kw)

        return wrapper

    monkeypatch.setattr(
        lifecycle_ledger,
        "detect_unclean_exit",
        _spy("detect", lifecycle_ledger.detect_unclean_exit),
    )
    monkeypatch.setattr(
        lifecycle_ledger,
        "attribute_unclean_exit",
        _spy("probe", lambda pid, when=None: {"killer": "SIGKILL"}),
    )
    monkeypatch.setattr(
        lifecycle_ledger,
        "_emit_unclean_report",
        _spy("report", lifecycle_ledger._emit_unclean_report),
    )
    monkeypatch.setattr(
        lifecycle_ledger,
        "_claim_sentinel",
        _spy("claim", lifecycle_ledger._claim_sentinel),
    )

    seen: dict = {}

    async def main():
        seen["loop_thread"] = threading.current_thread()
        return await lifecycle_ledger.record_startup_async(home=ledger_home)

    evidence = asyncio.run(main())

    assert evidence is not None and evidence["killer"] == "SIGKILL"
    loop_thread = seen["loop_thread"]
    for name, observed in threads.items():
        assert observed, "%s never ran at all — the spy is vacuous" % name
        assert loop_thread not in observed, (
            "%s ran on the event loop thread; the boot record must be "
            "entirely off-loop" % name
        )
    workers = set().union(*threads.values())
    assert len(workers) == 1, (
        "expected one worker thread for the whole body, saw %d — the offload "
        "is still per-step" % len(workers)
    )


def test_offload_failure_is_swallowed_and_never_reaches_boot(ledger_home, monkeypatch):
    """Best-effort contract: a forensics failure must never affect the
    lifecycle it observes, so the offload cannot raise into boot."""
    _clean_prior_life(ledger_home)

    async def exploding_to_thread(fn, *a, **kw):
        raise RuntimeError("executor is gone")

    monkeypatch.setattr(lifecycle_ledger.asyncio, "to_thread", exploding_to_thread)

    async def main():
        return await lifecycle_ledger.record_startup_async(home=ledger_home)

    assert asyncio.run(main()) is None


def test_sync_and_async_entry_points_cannot_drift(ledger_home, monkeypatch):
    """The async wrapper must delegate to ``record_startup`` itself, so the
    two entry points can never grow different behaviour."""
    _clean_prior_life(ledger_home)
    calls: list = []
    real = lifecycle_ledger.record_startup

    def spy(home=None):
        calls.append(home)
        return real(home)

    monkeypatch.setattr(lifecycle_ledger, "record_startup", spy)

    async def main():
        return await lifecycle_ledger.record_startup_async(home=ledger_home)

    asyncio.run(main())
    assert calls == [ledger_home], (
        "record_startup_async must run record_startup's body verbatim, not a "
        "reimplementation that can drift from it"
    )
