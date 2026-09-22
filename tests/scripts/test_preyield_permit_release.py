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
import ast
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


def test_guard_fails_loudly_when_a_SWEEP_enumerates_nothing(tmp_path):
    """Zero enumerated sites on a DIRECTORY sweep is exit 2, never a green pass."""
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


def test_per_file_invocation_with_no_sites_is_not_exit_2(tmp_path):
    """The documented ``[paths...]`` form must be usable per-file.

    Most files legitimately hold zero sites, so exit 2 there made a pre-commit
    / lint-staged invocation fail on every clean file. Vacuity is only a
    broken-discovery signal for a SWEEP, which the test above still pins.
    """
    site_less = tmp_path / "nothing.py"
    site_less.write_text("from contextlib import asynccontextmanager\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(site_less)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # ...and a named file that DOES violate is still exit 1.
    offender = tmp_path / "leaky.py"
    offender.write_text(
        textwrap.dedent(
            """
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def leaky(sem, surface):
                await sem.acquire()
                _LOG.info("admitted surface=%s", surface)
                try:
                    yield
                finally:
                    sem.release()
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(offender)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr


# --------------------------------------------------------------------------
# 3. Coverage gaps closed after FleetReview on PR #860.
# --------------------------------------------------------------------------


def test_guard_window_is_positional_not_line_based():
    """A leak sharing a LINE with the acquire or the yield must still fire.

    The first build bounded the window with ``acquire.lineno < n.lineno <
    yield.lineno``, so a semicolon-joined statement on either boundary line
    fell outside it and passed clean.
    """
    guard = _load_guard()

    on_acquire_line = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire(); _LOG.info("admitted surface=%s", surface)
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(on_acquire_line, "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        "a leak on the acquire's own line must be flagged"
    )

    on_yield_line = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            _LOG.info("admitted surface=%s", surface); yield
            sem.release()
        """
    )
    _sites, violations = guard.scan_source(on_yield_line, "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        "a leak on the yield's own line must be flagged"
    )


@pytest.mark.parametrize(
    "yield_expr",
    [
        "yield",
        "received = yield",
        "acc += yield",
        "handle((yield))",
        "await handle((yield))",
        "pair = ((yield), 1)",
        "v = (yield) if surface else None",
    ],
    ids=[
        "statement",
        "assign",
        "augassign",
        "call-arg",
        "awaited-call-arg",
        "tuple-element",
        "ifexp",
    ],
)
def test_guard_treats_every_yield_form_as_the_boundary(yield_expr):
    """An expression-form yield is the pre-yield boundary, both directions.

    A leaking window before it must FIRE, and the expression WRAPPING the yield
    must not itself be counted as a pre-yield offender (the yield runs first).
    """
    guard = _load_guard()
    leaking = textwrap.dedent(
        f"""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            _LOG.info("admitted surface=%s", surface)
            try:
                {yield_expr}
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(leaking, "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        f"a leak before `{yield_expr}` must be flagged"
    )

    guarded = textwrap.dedent(
        f"""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted surface=%s", surface)
            except BaseException:
                sem.release()
                raise
            try:
                {yield_expr}
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(guarded, "new.py")
    assert violations == [], (
        f"the expression wrapping `{yield_expr}` is not a pre-yield offender"
    )


_MULTI_PERMIT = """
from contextlib import asynccontextmanager

@asynccontextmanager
async def slot(self, key, *, internal=False):
    internal_acquired = total_acquired = False
    try:
        if internal:
            await self.internal.acquire()
            internal_acquired = True
        await self.total.acquire()
        total_acquired = True
        _LOG.info("admitted %s", key)
    except BaseException:
{release}
        raise
    try:
        yield
    finally:
        self.total.release()
        if internal_acquired:
            self.internal.release()
"""

_RELEASE_TOTAL = "        if total_acquired:\n            self.total.release()"
_RELEASE_INTERNAL = "        if internal_acquired:\n            self.internal.release()"


def test_guard_enumerates_every_acquired_object_at_a_multi_permit_site():
    guard = _load_guard()
    source = _MULTI_PERMIT.format(
        release=_RELEASE_TOTAL + "\n" + _RELEASE_INTERNAL
    )
    sites, violations = guard.scan_source(source, "new.py")
    assert [s["acquired_objects"] for s in sites] == [["self.internal", "self.total"]]
    assert violations == []


@pytest.mark.parametrize(
    "release, missing",
    [
        (_RELEASE_TOTAL, "self.internal"),
        (_RELEASE_INTERNAL, "self.total"),
        ("        self.unrelated.release()", "both"),
    ],
    ids=["total-only", "internal-only", "wrong-object"],
)
def test_guard_rejects_a_partial_release_at_a_multi_permit_site(release, missing):
    """Releasing SOME acquired object is not releasing the permit.

    ``gateway/turn_admission.py`` holds two semaphores (total + internal); the
    first build matched on the method name ``release`` alone, so an except arm
    that handed back only one of them — or an unrelated object entirely —
    satisfied the check while the other permit leaked.
    """
    guard = _load_guard()
    _sites, violations = guard.scan_source(
        _MULTI_PERMIT.format(release=release), "new.py"
    )
    assert [v["function"] for v in violations] == ["slot"], (
        f"a pre-yield handler that never releases {missing} must be flagged"
    )


def test_guard_requires_every_exiting_except_arm_to_release():
    """A sibling arm that re-raises without releasing is the arm that runs.

    The first build accepted the try as soon as ANY ``except BaseException``
    arm released, so a narrower sibling that re-raised bare leaked whenever it
    matched.
    """
    guard = _load_guard()
    leaking_sibling = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, key):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", key)
            except ValueError:
                _LOG.warning("re-raised without releasing")
                raise
            except BaseException:
                sem.release()
                raise
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(leaking_sibling, "new.py")
    assert [v["function"] for v in violations] == ["gate"]

    every_arm_releases = leaking_sibling.replace(
        '_LOG.warning("re-raised without releasing")', "sem.release()"
    )
    _sites, violations = guard.scan_source(every_arm_releases, "new.py")
    assert violations == []


def test_guard_does_not_demand_a_release_from_a_swallowing_arm():
    """An arm that SWALLOWS falls through to the yield still holding the permit.

    Demanding a release there would be a false positive, and acting on it would
    double-release — so only arms that can exit owe the permit back.

    The arm body must be PROVABLY inert.  This fixture used to hold
    ``_LOG.warning("tolerated; still admitted")``, which is not: driven with a
    benign logger that arm leaks 0, and with a bad format argument — identical
    AST — it leaks 1.  A fixture whose safety depends on runtime values cannot
    state the property, so it is narrowed to statements that cannot leave.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, key):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", key)
            except ValueError:
                tolerated = True
                pass
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


@pytest.mark.parametrize(
    "shape,arm",
    [
        ("a call that raises", "except BaseException:\n    _reraise()\n"),
        ("an assert", "except BaseException:\n    assert surface\n"),
        ("arithmetic", "except BaseException:\n    x = 1 / surface\n"),
        ("a logging call", 'except BaseException:\n    _LOG.info("swallowed")\n'),
        ("an attribute load", "except BaseException:\n    x = surface.depth\n"),
        ("a subscript", "except BaseException:\n    x = surface[0]\n"),
        ("an await", "except BaseException:\n    await _drain()\n"),
        # A bare NAME load raises UnboundLocalError when the binding has not
        # run yet — an ordinary possibility inside an except arm, and a
        # measured 1-permit leak.
        ("a name load", "except BaseException:\n    x = surface\n"),
        # An assignment TARGET is not a value: a tuple/list target UNPACKS,
        # which raises TypeError on a non-iterable and ValueError on an arity
        # mismatch, even when every name in it is inert.
        ("a tuple-target unpack", "except BaseException:\n    a, b = 0\n"),
        ("a starred tuple-target unpack", "except BaseException:\n    a, *b = 0\n"),
        ("a list-target unpack", "except BaseException:\n    [a, b] = 0\n"),
        ("an attribute target", "except BaseException:\n    surface.x = 0\n"),
        ("a subscript target", "except BaseException:\n    surface[0] = 0\n"),
        # Only a nested def's BODY is deferred; its signature runs now.
        (
            "a nested def with a raising default",
            "except BaseException:\n    def _later(x=_boom()):\n        pass\n",
        ),
        (
            "a nested def with a raising kw-only default",
            "except BaseException:\n    def _later(*, x=_boom()):\n        pass\n",
        ),
        (
            "a nested def with a raising arg annotation",
            "except BaseException:\n    def _later(x: _boom()):\n        pass\n",
        ),
        (
            "a nested def with a raising return annotation",
            "except BaseException:\n    def _later() -> _boom():\n        pass\n",
        ),
        # Set/dict displays HASH at construction, so inert elements are not
        # enough to make the display inert.
        ("an unhashable set literal", "except BaseException:\n    s = {[1]}\n"),
        ("an unhashable dict key", "except BaseException:\n    d = {[1]: 2}\n"),
        # AnnAssign evaluates its annotation at runtime without PEP 563.
        ("a raising annotation", "except BaseException:\n    x: _boom() = 1\n"),
    ],
)
def test_guard_flags_an_arm_that_can_leave_without_a_raise_statement(shape, arm):
    """An arm leaves for many reasons that are not an ``ast.Raise`` node.

    Asking "does this arm contain a ``raise``/``return`` STATEMENT?" excused a
    call that raises, an ``assert``, arithmetic that divides by zero, and a
    plain logging call given a bad format argument.  The first whitelist then
    excused a second family: an unpacking assignment target, a nested ``def``
    whose SIGNATURE (defaults / annotations) is evaluated at definition time, a
    set/dict display that hashes an unhashable element, and a bare name load
    that can be an unbound local.  Every shape here is a measured 1-permit
    leak.  The window check has always treated a call / attribute / subscript /
    await as raise-capable; the arm check now agrees, and additionally refuses
    to excuse anything whose targets or signature can raise.
    """
    guard = _load_guard()
    _sites, violations = guard.scan_source(_sibling_arm_source(arm), "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        f"an arm that can leave via {shape} must be asked to release"
    )


@pytest.mark.parametrize(
    "shape,arm",
    [
        ("pass", "except BaseException:\n    pass\n"),
        ("a bare constant", 'except BaseException:\n    "swallowed"\n'),
        ("a constant assignment", "except BaseException:\n    x = 0\n"),
        ("a constant tuple value", "except BaseException:\n    x = (1, 2)\n"),
        ("a hashable set literal", "except BaseException:\n    s = {1, 2}\n"),
        (
            "a constant annotated assignment",
            'except BaseException:\n    x: "int" = 0\n',
        ),
        (
            "a nested def",
            "except BaseException:\n    def _later():\n        sem.release()\n",
        ),
        (
            "a nested def with a constant default",
            "except BaseException:\n    def _later(x=0):\n        sem.release()\n",
        ),
    ],
)
def test_guard_still_excuses_a_provably_inert_arm(shape, arm):
    """The other side: an arm that provably cannot leave still owes nothing.

    Without this the swallow exemption would collapse to "never excuse
    anything" and the finding-3 false-positive fix would be undone.
    """
    guard = _load_guard()
    _sites, violations = guard.scan_source(_sibling_arm_source(arm), "new.py")
    assert violations == [], f"an arm holding only {shape} cannot leave"


# --------------------------------------------------------------------------
# 3b. Reachability: "can control reach the end of this arm?", not "is the last
#     statement a bare raise/return?".  Every shape below is a MEASURED
#     1-permit leak (real asyncio.Semaphore, synchronous fault at the pre-yield
#     log) that was CLEAN on both the #860 guard and the #863 candidate.
# --------------------------------------------------------------------------


def _sibling_arm_source(narrow_arm: str) -> str:
    """A two-arm gate: a narrow arm under test + a correct BaseException arm.

    The sibling matters: with only the narrow arm the try has no
    ``BaseException`` coverage and is flagged for that reason instead, so the
    fixture would pass for the wrong reason.  The correct sibling is what makes
    the narrow arm's own classification the thing under test.
    """
    return textwrap.dedent(
        """
        from contextlib import asynccontextmanager, suppress

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
        {arm}
            except BaseException:
                sem.release()
                raise
            try:
                yield
            finally:
                sem.release()
        """
    ).replace("{arm}", textwrap.indent(textwrap.dedent(narrow_arm), "    ").rstrip())


@pytest.mark.parametrize(
    "shape,arm",
    [
        ("if cond: raise", "except Boom:\n    if surface:\n        raise\n"),
        (
            "raise inside an inner try/finally",
            "except Boom:\n    try:\n        raise\n    finally:\n        pass\n",
        ),
        (
            "raise inside a with",
            "except Boom:\n    with suppress(ValueError):\n        raise\n",
        ),
        ("raise inside a for", "except Boom:\n    for _ in (1,):\n        raise\n"),
        ("if cond: return", "except Boom:\n    if surface:\n        return\n"),
    ],
)
def test_guard_flags_a_conditionally_exiting_arm_that_does_not_release(shape, arm):
    """FINDING 1: an arm that exits through a COMPOUND statement still owes.

    The old rule read ``reversed(handler.body)`` and returned True only on a
    BARE trailing ``Raise``/``Return``, breaking on any other statement type.
    So each shape here was classified as swallowing and excused from
    releasing, while the correct ``BaseException`` sibling made the chain look
    satisfied.  All five are measured 1-permit leaks.
    """
    guard = _load_guard()
    _sites, violations = guard.scan_source(_sibling_arm_source(arm), "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        f"an arm that exits via {shape} must be asked to release"
    )


def test_conditionally_exiting_arm_is_accepted_once_it_releases():
    """The discriminator's other side: releasing first clears the same shape.

    Argus's CONTROL I6 — measured NOT to leak — so the new rule must not flag
    it, or the fixture above would pass by flagging everything.
    """
    guard = _load_guard()
    releasing = "except Boom:\n    sem.release()\n    if surface:\n        raise\n"
    _sites, violations = guard.scan_source(_sibling_arm_source(releasing), "new.py")
    assert violations == []


@pytest.mark.parametrize(
    "shape,arm",
    [
        (
            "nested def",
            "except BaseException:\n    def _later():\n        sem.release()\n    raise\n",
        ),
        (
            "nested def under a condition",
            "except BaseException:\n    if surface:\n        def _later():\n            sem.release()\n    raise\n",
        ),
        (
            "lambda under a condition",
            "except BaseException:\n    if surface:\n        _later = lambda: sem.release()\n    raise\n",
        ),
        (
            "if False",
            "except BaseException:\n    if False:\n        sem.release()\n    raise\n",
        ),
        (
            "loop over an empty literal",
            "except BaseException:\n    for _ in []:\n        sem.release()\n    raise\n",
        ),
        (
            "after an unconditional raise",
            "except BaseException:\n    raise\n    sem.release()\n",
        ),
    ],
)
def test_guard_does_not_credit_an_unreachable_release(shape, arm):
    """FINDING 2: ``ast.walk`` counted a release that can never run.

    A release buried in a nested ``def`` (its own lifecycle), under
    ``if False:``, or in the body of a loop over an empty literal is textually
    present and never executes.  All three are measured 1-permit leaks.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
        {arm}
            try:
                yield
            finally:
                sem.release()
        """
    ).replace("{arm}", textwrap.indent(textwrap.dedent(arm), "    ").rstrip())
    _sites, violations = guard.scan_source(source, "new.py")
    assert [v["function"] for v in violations] == ["gate"], (
        f"a release reachable only via {shape} must not satisfy the check"
    )


@pytest.mark.parametrize(
    "shape,arm",
    [
        ("under a runtime condition", "if surface:\n    sem.release()\n"),
        ("in a loop over a non-empty literal", "for _ in (1,):\n    sem.release()\n"),
        ("in a with body", "with suppress(ValueError):\n    sem.release()\n"),
    ],
)
def test_guard_still_credits_a_conditionally_reachable_release(shape, arm):
    """The reachability narrowing must not reject releases that CAN run.

    Only statically-dead paths lose credit; anything the compiler cannot
    settle stays creditable, or the finding-2 fixture would pass by rejecting
    every non-trivial release.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager, suppress

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
            except BaseException:
        {arm}
                raise
            try:
                yield
            finally:
                sem.release()
        """
    ).replace("{arm}", textwrap.indent(textwrap.dedent(arm), "        ").rstrip())
    # Two releases: the one under test in the handler, and the finally around
    # the yield. If the fixture ever loses one, it is no longer the shape.
    assert source.count("sem.release()") == 2
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == [], f"a release {shape} is reachable and must count"


def test_guard_does_not_flag_a_sole_swallowing_baseexception_arm():
    """FINDING 3: ``except BaseException: pass`` as the ONLY arm is SAFE.

    The arm swallows and control proceeds to the yield still legitimately
    holding the permit — measured NOT to leak.  The old code asked for a
    ``BaseException`` among the EXITING arms only, so a chain whose sole arm
    swallows produced an empty exiting-set and was reported as unprotected,
    contradicting the guard's own swallow rationale.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
            except BaseException:
                pass
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == []


def test_guard_does_not_flag_a_narrow_releasing_arm_beside_a_swallowing_base():
    """FINDING 3 (H2): narrow arm releases+raises, BaseException swallows.

    Neither path leaks — the narrow arm hands the permit back before exiting,
    and the BaseException arm falls through to the yield still holding it.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
            except ValueError:
                sem.release()
                raise
            except BaseException:
                pass
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert violations == []


def test_swallowing_chain_still_needs_baseexception_coverage():
    """A swallow-only chain that does NOT cover BaseException is still a leak.

    ``except ValueError: pass`` catches nothing a cancel raises, so the window
    remains unprotected — finding 3's relaxation must not extend to it.
    """
    guard = _load_guard()
    source = textwrap.dedent(
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gate(sem, surface):
            await sem.acquire()
            try:
                _LOG.info("admitted %s", surface)
            except ValueError:
                pass
            try:
                yield
            finally:
                sem.release()
        """
    )
    _sites, violations = guard.scan_source(source, "new.py")
    assert [v["function"] for v in violations] == ["gate"]


def test_guard_enumerates_both_permits_at_the_live_turn_admission_site():
    """Positive coverage on the REAL multi-permit site, named not counted."""
    guard = _load_guard()
    source = (REPO_ROOT / "gateway" / "turn_admission.py").read_text()
    sites, violations = guard.scan_source(source, "gateway/turn_admission.py")

    assert [s["function"] for s in sites] == ["slot"]
    assert sites[0]["acquired_objects"] == ["self.internal", "self.total"]
    assert violations == []


def test_guard_fires_on_a_partial_release_mutation_of_the_live_site():
    """Independent oracle: mutate REAL source, not a hand-written fixture.

    Deleting only the ``self.internal`` release from ``TurnAdmission.slot``'s
    pre-yield handler leaks that permit on every cancel; the guard must say so.
    """
    guard = _load_guard()
    source = (REPO_ROOT / "gateway" / "turn_admission.py").read_text()
    intact = """            if internal_acquired:
                self.internal.release()
            raise"""
    assert source.count(intact) == 1, "live pre-yield handler shape changed"
    mutated = source.replace(intact, "            raise")

    _sites, violations = guard.scan_source(mutated, "gateway/turn_admission.py")
    assert [v["function"] for v in violations] == ["slot"], (
        "a partial release at the live multi-permit site must be flagged"
    )


# --------------------------------------------------------------------------
# 4. Gate module: the aborted pre-yield window must not be counted as served.
# --------------------------------------------------------------------------


def test_aborted_pre_yield_window_records_no_admission_stats(monkeypatch):
    """An abort before the yield means the caller never got the slot.

    ``_record_stats`` used to run FIRST in the window, so a fault after it left
    acquired_count / queued_count / queue_wait_seconds_total counting a slot
    that was never granted — the counters overstated served load exactly when
    the gate was faulting. It now runs LAST in the guarded window.
    """
    from hermes_cli import session_db_heavy_gate as gate

    gate.reset_session_db_heavy_read_gate_for_tests()
    monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 2)
    # Force was_queued so the log call (the injection point) is reached.
    monkeypatch.setattr(gate, "_QUEUE_WAIT_LOG_THRESHOLD_S", -1.0)

    def boom(*args, **kwargs):
        raise RuntimeError("injected pre-yield fault")

    async def run():
        semaphore = gate.session_db_heavy_read_semaphore()
        monkeypatch.setattr(gate._LOG, "info", boom)

        entered = False
        with pytest.raises(RuntimeError, match="injected pre-yield fault"):
            async with gate.session_db_heavy_read_slot(
                surface="test", operation="victim"
            ):
                entered = True
        assert entered is False
        assert semaphore._value == 2, "permit burned by the pre-yield raise"

        stats = gate.session_db_heavy_read_stats()
        assert stats["acquired_count"] == 0, (
            "an aborted pre-yield window counted a slot the caller never got"
        )
        assert stats["queued_count"] == 0
        assert stats["queue_wait_seconds_total"] == 0.0

        # ...and a GRANTED admission is still counted.
        monkeypatch.undo()
        monkeypatch.setattr(gate, "_configured_max_concurrency", lambda: 2)
        async with gate.session_db_heavy_read_slot(
            surface="test", operation="healthy"
        ):
            pass
        assert gate.session_db_heavy_read_stats()["acquired_count"] == 1

    asyncio.run(run())
    gate.reset_session_db_heavy_read_gate_for_tests()


def test_max_concurrency_read_does_not_deepcopy_config_per_admission():
    """The cap read runs on the event loop per admission — keep it off load_config().

    ``load_config()`` performs a full expansion + ``copy.deepcopy`` per call
    (measured ~429 us against the live 22 KB config.yaml, vs ~4.6 us for the
    read-only variant). Both share the same (mtime_ns, size) freshness key, so
    this is a cost change, not a behaviour change.
    """
    from hermes_cli import session_db_heavy_gate as gate

    # AST, not a text grep: the docstring legitimately names load_config to
    # explain the choice, so only real imports/calls count.
    tree = ast.parse(Path(gate.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "read_raw_config_readonly" in imported
    assert "load_config" not in imported, (
        "the per-admission cap read must not go through load_config()"
    )
    assert "load_config" not in called
    # Still resolves to a usable positive cap.
    assert gate._configured_max_concurrency() >= 1
