"""managed_scope's test-context predicate must answer for the SESSION, not one phase.

``PYTEST_CURRENT_TEST`` is set only while a test function runs. pytest unsets it at
collection, in any thread that outlives its test, and at interpreter shutdown — so a
predicate that reads only that var lets a real ``/etc/hermes`` managed scope back into the
suite in exactly those phases and pin admin policy onto the tests. Measured on pytest 9.x.

These cases drive the CURRENT_TEST-absent phases directly (by scrubbing the var, which is
what pytest itself does outside the in-test phase) against a stand-in for the system default.
"""
from pathlib import Path

import pytest

from hermes_cli import managed_scope

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Phases where pytest leaves ``PYTEST_CURRENT_TEST`` unset but the session IS a test run:
#: module import / collection, a straggler thread, session-scoped teardown, atexit.
_CURRENT_TEST_ABSENT_PHASES = ("collection", "straggler-thread", "sessionfinish", "atexit")


@pytest.fixture
def system_managed_dir(tmp_path, monkeypatch):
    """A populated stand-in for the system ``/etc/hermes``, with no override set."""
    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: pinned/by-admin\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", managed)
    managed_scope.invalidate_managed_cache()
    yield managed
    managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("phase", _CURRENT_TEST_ABSENT_PHASES)
def test_system_scope_ignored_when_current_test_is_absent(phase, system_managed_dir, monkeypatch):
    """The system scope stays invisible in every CURRENT_TEST-absent phase of a test session."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert managed_scope._under_pytest() is True, f"{phase}: predicate lost the session"
    assert managed_scope.get_managed_dir() is None, f"{phase}: system managed scope leaked"
    assert managed_scope.managed_config_keys() == set(), f"{phase}: admin policy leaked"


def test_system_scope_ignored_in_test_phase(system_managed_dir):
    """The original in-test behaviour is unchanged."""
    assert managed_scope._under_pytest() is True
    assert managed_scope.get_managed_dir() is None


def test_explicit_override_still_wins_when_current_test_is_absent(tmp_path, monkeypatch):
    """Widening the predicate must not shadow the IT bootstrap override (it outranks the guard)."""
    override = tmp_path / "override"
    override.mkdir()
    (override / "config.yaml").write_text("model: pinned/by-it\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(override))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    managed_scope.invalidate_managed_cache()
    try:
        assert managed_scope.get_managed_dir() == override
        assert managed_scope.managed_config_keys() == {"model"}
    finally:
        managed_scope.invalidate_managed_cache()


def test_predicate_delegates_and_is_not_a_second_private_copy(monkeypatch):
    """One definition, proven at RUNTIME: swapping the guard's predicate swaps this one.

    An identity/source-text assertion would only grade the wiring's appearance. Patching the
    single definition and observing managed_scope's answer change proves the delegation is
    live — a private copy would be unaffected by the patch.
    """
    import hermes_state_guard

    monkeypatch.setattr(managed_scope, "_in_test_context", lambda: False)
    assert managed_scope._under_pytest() is False
    monkeypatch.setattr(managed_scope, "_in_test_context", lambda: True)
    assert managed_scope._under_pytest() is True

    # ...and the name it binds is the guard's, not a re-implementation.
    monkeypatch.undo()
    assert managed_scope._in_test_context is hermes_state_guard._in_test_context


def test_test_context_module_is_a_leaf():
    """The predicate module must not drag in a dependency graph.

    This is the assertion that keeps the delegation safe to make. ``hermes_state``
    transitively imports ``agent.redact``, which snapshots its enable-toggle at import
    time — so delegating *through* ``hermes_state`` would pull that snapshot forward and
    freeze the toggle before the config bridge ran. (Measured: that is exactly what broke
    tests/hermes_cli/test_redact_config_bridge.py on the fork arm of this fix, where the
    predicate has not yet been extracted.) Importing the leaf must pull neither.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import hermes_state_guard; "
        "print('redact', 'agent.redact' in sys.modules); "
        "print('state', 'hermes_state' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "redact False" in out.stdout, out.stdout
    assert "state False" in out.stdout, out.stdout
