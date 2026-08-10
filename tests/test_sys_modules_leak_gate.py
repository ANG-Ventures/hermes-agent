"""Proof that the sys.modules leak gate FIRES — and stays quiet otherwise.

A gate that has never been shown to fail is not a gate. These exercise the
gate's real logic (``snapshot_watched`` + ``leak_failure_message``) rather than
driving pytest internals, so both directions are provable and the test is not
coupled to fixture plumbing.
"""

from __future__ import annotations

import sys
import types

import pytest

from tests.sys_modules_leak_gate import (
    _watched,
    leak_failure_message,
    snapshot_watched,
)

NODE = "tests/fake/test_thing.py::test_case"


def _removed_by(body) -> set:
    """Run ``body`` and return the watched modules it removed without restoring."""
    before = snapshot_watched()
    body()
    return before - snapshot_watched()


@pytest.fixture
def sentinel_module():
    name = "agent._leak_gate_sentinel"
    sys.modules[name] = types.ModuleType(name)
    yield name
    sys.modules.pop(name, None)


class TestGateFires:
    def test_unrestored_purge_is_detected(self, sentinel_module):
        """THE case: a purge with no restore must be caught."""
        removed = _removed_by(lambda: sys.modules.pop(sentinel_module, None))
        assert sentinel_module in removed

    def test_failure_message_is_produced_and_names_the_test(self, sentinel_module):
        removed = _removed_by(lambda: sys.modules.pop(sentinel_module, None))
        msg = leak_failure_message(NODE, removed)
        assert msg, "gate produced no failure message for a real leak"
        assert NODE in msg
        assert sentinel_module in msg
        assert "did not restore" in msg

    def test_message_carries_the_fix_recipe(self, sentinel_module):
        """The next person shouldn't have to re-derive the fix."""
        removed = _removed_by(lambda: sys.modules.pop(sentinel_module, None))
        msg = leak_failure_message(NODE, removed)
        assert "sys.modules.update(saved)" in msg
        assert "parent.__dict__" in msg
        assert "allow_sys_modules_purge" in msg

    def test_message_truncates_a_large_leak(self):
        removed = {f"agent.mod{i}" for i in range(20)}
        msg = leak_failure_message(NODE, removed)
        assert "removed 20 module(s)" in msg
        assert "+14 more" in msg


class TestGateStaysQuiet:
    def test_restored_purge_is_clean(self, sentinel_module):
        """Purge + restore — the CORRECT pattern — must not fire."""
        def body():
            saved = dict(sys.modules)
            sys.modules.pop(sentinel_module, None)
            sys.modules.update(saved)
        assert _removed_by(body) == set()

    def test_adding_modules_is_not_a_leak(self):
        """Imports happen; only REMOVING pre-existing modules is the hazard."""
        name = "agent._leak_gate_added"
        try:
            assert _removed_by(lambda: sys.modules.setdefault(name, types.ModuleType(name))) == set()
        finally:
            sys.modules.pop(name, None)

    def test_untouched_run_is_clean(self):
        assert _removed_by(lambda: None) == set()

    def test_unwatched_module_removal_is_ignored(self):
        """Third-party lazy-import churn must not produce noise."""
        name = "some_vendor_lib._lazy"
        sys.modules[name] = types.ModuleType(name)
        assert _removed_by(lambda: sys.modules.pop(name, None)) == set()

    def test_empty_removal_yields_no_message(self):
        assert leak_failure_message(NODE, set()) == ""


class TestWatchedPrefixesAreNarrow:
    @pytest.mark.parametrize("name,expected", [
        ("run_agent", True),
        ("agent.title_generator", True),
        ("tools.browser_tool", True),
        ("hermes_logging", True),
        ("gateway.run", True),
        ("plugins.discord", False),   # deliberately excluded — discovery tests re-scan
        ("botocore.session", False),
        ("urllib3.connection", False),
        # must not match on a bare substring — a broad check would be pure noise
        ("agentic_unrelated", False),
        ("my_agent", False),
    ])
    def test_prefix_matching(self, name, expected):
        assert _watched(name) is expected
