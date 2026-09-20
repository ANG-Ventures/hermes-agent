"""managed_scope's test-context predicate must answer for the SESSION, not one phase.

``PYTEST_CURRENT_TEST`` is set only while a test function runs. pytest unsets it at
collection, in any thread that outlives its test, and at interpreter shutdown — so a
predicate that reads only that var lets a real ``/etc/hermes`` managed scope back into the
suite in exactly those phases and pin admin policy onto the tests. Measured on pytest 9.x.

These cases drive the CURRENT_TEST-absent phases directly (by scrubbing the var, which is
what pytest itself does outside the in-test phase) against a stand-in for the system default.
"""
import pytest

from hermes_cli import managed_scope

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


def test_predicate_is_not_a_second_private_copy():
    """One definition: managed_scope delegates to the guard's, it does not re-implement it."""
    import hermes_state_guard

    assert managed_scope._in_test_context is hermes_state_guard._in_test_context
