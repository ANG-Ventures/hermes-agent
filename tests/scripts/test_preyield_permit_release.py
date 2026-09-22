"""Regression + guard tests for the pre-yield permit leak class.

Two halves:

1. A BEHAVIOUR test on the live site — ``session_db_heavy_read_slot`` must hand
   its permit back when its pre-yield region raises, and a fresh caller must be
   admitted immediately afterwards (the burned-slot symptom, not just the
   counter).  Mirrors ``tests/gateway/test_turn_concurrency.py::
   test_pre_yield_failure_releases_acquired_permits`` from PR #827.

2. The pytest wrapper for ``scripts/check_preyield_permit_release.py`` — the
   repo-level guard that stops a THIRD such context manager landing unguarded.
   Same shape as ``tests/tools/test_subprocess_stdin_guard.py``.
"""

import asyncio
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_preyield_permit_release.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_preyield_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# 1. Behaviour: the live site releases when the pre-yield region raises.
# --------------------------------------------------------------------------


def test_pre_yield_failure_releases_the_heavy_read_permit(monkeypatch):
    """Anything raised after acquire but before yield must release the permit.

    ``session_db_heavy_read_slot`` is an async generator: if it raises before
    its first ``yield``, ``__aexit__`` never runs, so the pre-yield region is
    the ONLY place that can hand the permit back.  Without that guard, each
    fault burns one of ``max_concurrency`` permits permanently; at zero, every
    heavy read sheds ``SessionDBHeavyReadBusy`` until the process restarts.

    The statements in that window (``_record_stats``, ``_LOG.info``) do not
    raise today — this is a latent leak, gated so a refactor cannot make it
    live.  Injected at ``_record_stats``, the window's first statement.
    """
    from hermes_cli import session_db_heavy_gate as gate

    cap = 2
    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: cap)

    calls = []
    real_record_stats = gate._record_stats

    def boom(**kwargs):
        if kwargs.get("acquired"):
            calls.append(kwargs)
            raise RuntimeError("injected pre-yield fault")
        return real_record_stats(**kwargs)

    async def run():
        semaphore = gate.session_db_heavy_read_semaphore()
        assert semaphore._value == cap

        monkeypatch.setattr(gate, "_record_stats", boom)
        entered = False
        with pytest.raises(RuntimeError, match="injected pre-yield fault"):
            async with gate.session_db_heavy_read_slot(
                surface="test", operation="victim"
            ):
                entered = True

        assert calls, "pre-yield region did not reach the injected fault"
        assert entered is False
        assert semaphore._value == cap, "permit burned by the pre-yield raise"

        # The burned-slot symptom: repeat it cap times, then a healthy caller
        # must still be served immediately rather than shed.
        for _ in range(cap):
            with pytest.raises(RuntimeError):
                async with gate.session_db_heavy_read_slot(
                    surface="test", operation="victim"
                ):
                    pass

        monkeypatch.setattr(gate, "_record_stats", real_record_stats)
        served = False
        async with gate.session_db_heavy_read_slot(
            surface="test", operation="healthy"
        ):
            served = True

        assert served, "gate starved: a healthy caller was shed after the faults"
        assert semaphore._value == cap

    asyncio.run(run())
    gate.reset_session_db_heavy_read_gate_for_tests()


# --------------------------------------------------------------------------
# 2. The repo-level guard.
# --------------------------------------------------------------------------


def test_repo_has_no_unguarded_preyield_permit_acquires():
    """Every @asynccontextmanager that acquires before yielding releases on raise."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"pre-yield permit release check failed:\n{result.stdout}\n{result.stderr}"
    )


def test_guard_enumerates_the_live_heavy_read_site():
    """Positive coverage assertion: a guard that enumerates nothing is vacuous.

    Named, not counted — "still clean" is indistinguishable from "clean because
    the discovery shape broke and found zero sites".
    """
    guard = _load_guard()
    source = (REPO_ROOT / "hermes_cli" / "session_db_heavy_gate.py").read_text()
    sites, violations = guard.scan_source(source, "hermes_cli/session_db_heavy_gate.py")

    assert [s["function"] for s in sites] == ["session_db_heavy_read_slot"]
    assert violations == []


def test_guard_flags_an_unguarded_acquire_before_yield():
    """The killer mutation: a NEW violating site the guard has never seen."""
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def brand_new_gate(sem, surface):
            await sem.acquire()
            _LOG.info("admitted surface=%s", surface)
            try:
                yield
            finally:
                sem.release()
        """
    )
    sites, violations = guard.scan_source(source, "new.py")

    assert [s["function"] for s in sites] == ["brand_new_gate"]
    assert [v["function"] for v in violations] == ["brand_new_gate"], (
        "an unguarded pre-yield region must be flagged"
    )


def test_guard_accepts_the_except_baseexception_remediation():
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def brand_new_gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted surface=%s", surface)
            except BaseException:
                sem.release()
                raise
            try:
                yield
            finally:
                sem.release()
        """
    )
    sites, violations = guard.scan_source(source, "new.py")

    assert [s["function"] for s in sites] == ["brand_new_gate"]
    assert violations == []


def test_guard_accepts_a_try_finally_remediation():
    """try/finally that releases is equally sound for the pre-yield window."""
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def brand_new_gate(sem, surface):
            await sem.acquire()
            admitted = False
            try:
                _LOG.info("admitted surface=%s", surface)
                admitted = True
            finally:
                if not admitted:
                    sem.release()
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == []


def test_guard_is_not_fooled_by_a_narrower_except():
    """``except Exception`` does not catch a cancel — still a leak."""
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def brand_new_gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted surface=%s", surface)
            except Exception:
                sem.release()
                raise
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert [v["function"] for v in violations] == ["brand_new_gate"]


def test_guard_does_not_use_an_await_only_discriminator():
    """The miss this guard exists to close.

    PR #827's class sweep cleared the heavy-read gate because its pre-yield
    window contains zero ``await``s — but the PR's own regression test injects
    a SYNCHRONOUS raise.  A window of purely synchronous, raise-capable
    statements must still be flagged.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def sync_window_gate(sem, surface):
            await sem.acquire()
            stats.record(acquired=1)
            _LOG.info("admitted surface=%s", surface)
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert [v["function"] for v in violations] == ["sync_window_gate"], (
        "a synchronous-only pre-yield window must still be flagged"
    )


def test_guard_does_not_flag_the_acquire_failure_path():
    """A raise in the acquire's own except/finally means acquire FAILED.

    There is no permit to hand back there, so demanding a release would be a
    false positive on correct code.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def shedding_gate(sem, surface):
            try:
                await asyncio.wait_for(sem.acquire(), timeout=1.0)
            except asyncio.TimeoutError as exc:
                _LOG.warning("shed surface=%s", surface)
                raise Busy() from exc
            try:
                _LOG.info("admitted surface=%s", surface)
            except BaseException:
                sem.release()
                raise
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == []


def test_guard_ignores_nested_coroutine_bodies():
    """A nested coroutine has its own lifecycle; it is not the pre-yield window."""
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate_with_helper(sem):
            await sem.acquire()

            async def later():
                _LOG.info("runs inside the body, not the window")

            try:
                yield later
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == []


def test_guard_honors_the_inline_exemption_marker():
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def brand_new_gate(sem, surface):
            await sem.acquire()  # noqa: preyield-permit
            _LOG.info("admitted surface=%s", surface)
            try:
                yield
            finally:
                sem.release()
        """
    )
    sites, violations = guard.scan_source(source, "new.py")
    assert [s["exempt"] for s in sites] == [True]
    assert violations == []


def test_guard_fails_loudly_when_it_enumerates_nothing(tmp_path):
    """Zero enumerated sites is exit 2, never a green pass."""
    (tmp_path / "nothing.py").write_text(
        "from contextlib import asynccontextmanager\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "vacuously green" in result.stderr
