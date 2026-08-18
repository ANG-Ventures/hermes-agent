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
    purged_modules,
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


class TestPurgedModulesRestores:
    """``purged_modules`` is the executable form of the gate's fix recipe.

    Each case is driven through ``_removed_by`` where it can be, so the
    assertion is "the gate stays quiet", not "the helper looks right".
    """

    def _pred(self, root):
        return lambda name: name == root or name.startswith(root + ".")

    @pytest.fixture
    def package(self):
        """A two-module package: ``agent._pm_pkg`` + its ``.leaf`` submodule."""
        root, leaf = "agent._pm_pkg", "agent._pm_pkg.leaf"
        pkg, sub = types.ModuleType(root), types.ModuleType(leaf)
        setattr(pkg, "leaf", sub)
        sys.modules[root], sys.modules[leaf] = pkg, sub
        yield root, leaf, pkg, sub
        sys.modules.pop(leaf, None)
        sys.modules.pop(root, None)

    def test_purge_is_visible_inside_the_block(self, package):
        root, leaf, _pkg, _sub = package
        with purged_modules(self._pred(root)):
            assert root not in sys.modules
            assert leaf not in sys.modules

    def test_gate_stays_quiet_after_the_block(self, package):
        root, _leaf, _pkg, _sub = package

        def body():
            with purged_modules(self._pred(root)):
                pass

        assert _removed_by(body) == set()

    def test_control_an_unrestored_purge_does_fire(self, package):
        """Control arm: prove the previous test isn't vacuously green.

        If the purge were a no-op (or the modules were already gone), the
        clean-exit assertion above would pass for the wrong reason. This shows
        the same predicate over the same modules DOES trip the gate when the
        restore is skipped.
        """
        root, leaf, pkg, sub = package
        pred = self._pred(root)

        def unrestored():
            for name in [n for n in list(sys.modules) if pred(n)]:
                sys.modules.pop(name, None)

        try:
            assert _removed_by(unrestored) == {root, leaf}
        finally:
            sys.modules[root], sys.modules[leaf] = pkg, sub

    def test_module_identity_is_restored(self, package):
        """Restoring the NAME is not enough — later tests hold the OBJECT."""
        root, leaf, pkg, sub = package
        with purged_modules(self._pred(root)):
            sys.modules[root] = types.ModuleType(root)  # a "fresh import"
        assert sys.modules[root] is pkg
        assert sys.modules[leaf] is sub

    def test_leaf_the_reimport_never_pulls_back_is_restored(self, package):
        """THE bug: a purged leaf whose only importer is a DIFFERENT parent.

        ``hermes_cli._subprocess_compat`` is imported at collection time by
        ``agent.skill_preprocessing``, so a fixture re-importing
        ``hermes_cli.kanban_db`` never re-imports it. Without restore, it stays
        gone and the gate fires.
        """
        root, leaf, pkg, sub = package
        with purged_modules(self._pred(root)):
            # Re-import only the ROOT, exactly like the kanban fixtures do.
            sys.modules[root] = types.ModuleType(root)
        assert sys.modules.get(leaf) is sub, "leaf must come back even if nothing re-imported it"

    def test_fresh_modules_created_inside_are_evicted(self, package):
        """A bare ``sys.modules.update(saved)`` strands orphaned copies."""
        root, _leaf, _pkg, _sub = package
        extra = root + ".created_during_test"
        with purged_modules(self._pred(root)):
            sys.modules[extra] = types.ModuleType(extra)
        assert extra not in sys.modules

    def test_parent_attribute_is_restored(self, package):
        """``from pkg import leaf`` reads the ATTRIBUTE, not sys.modules."""
        root, _leaf, pkg, sub = package
        with purged_modules(self._pred(root)):
            setattr(pkg, "leaf", types.ModuleType("orphan"))
        assert getattr(pkg, "leaf") is sub

    def test_parent_attribute_absent_before_stays_absent(self):
        """Restore must not invent an attribute the parent never had."""
        root, leaf = "agent._pm_noattr", "agent._pm_noattr.leaf"
        pkg, sub = types.ModuleType(root), types.ModuleType(leaf)
        sys.modules[root], sys.modules[leaf] = pkg, sub  # no pkg.leaf binding
        try:
            with purged_modules(self._pred(root)):
                pass
            assert not hasattr(pkg, "leaf")
        finally:
            sys.modules.pop(leaf, None)
            sys.modules.pop(root, None)

    def test_restore_happens_even_when_the_body_raises(self, package):
        root, _leaf, pkg, _sub = package
        with pytest.raises(RuntimeError):
            with purged_modules(self._pred(root)):
                raise RuntimeError("boom")
        assert sys.modules[root] is pkg

    def test_non_matching_modules_are_untouched(self, package):
        root, _leaf, _pkg, _sub = package
        bystander = "agent._pm_bystander"
        mod = types.ModuleType(bystander)
        sys.modules[bystander] = mod
        try:
            with purged_modules(self._pred(root)):
                assert sys.modules[bystander] is mod
            assert sys.modules[bystander] is mod
        finally:
            sys.modules.pop(bystander, None)
