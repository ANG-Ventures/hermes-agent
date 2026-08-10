"""Gate: a test that purges ``sys.modules`` must restore it.

WHY THIS EXISTS
---------------
Three separate leakers of this exact shape were found in ``tests/agent`` on
2026-08-09, each costing a real chunk of the suite:

  * ``test_empty_tool_name_loop_dampening.py``  (#538) — 120 -> 74 failures
  * ``test_verification_stop_caching.py``               — ~20 more
  * a third found in parallel by a worker, same class

The pattern is always the same. A test wants a *fresh* import (usually to pick
up a patched module or a new ``HERMES_HOME``), so it does::

    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent."):
            del sys.modules[mod]
    import run_agent

...and never puts them back. Every LATER importer in the same pytest process
then receives a brand-new module object, so any subsequent test that captured a
reference to — or monkeypatched — one of those modules is operating on a
*different object* than the code under test imports. Its setup silently applies
to an orphaned copy.

The failures land far away from the cause and look like unrelated bugs
("No LLM provider configured", stale caches, missing attributes), which is what
made them expensive to find: a full bisect of the file list per victim.

WHAT THIS GATE DOES
-------------------
Fails LOUDLY, at the leaking test, naming it — instead of mysteriously
downstream. This is the "codify the lesson as a durable gate" half: the bisect
found the instances, this stops the class.

It is deliberately NARROW: it only fires when a test leaves ``sys.modules`` in a
state that can poison a sibling — modules that existed before the test and are
gone after. Adding new modules is normal (imports happen); REMOVING pre-existing
ones is the hazard.
"""

from __future__ import annotations

import sys

import pytest


# Only these prefixes can realistically poison a sibling test in this repo. A
# broad check would fire on third-party lazy-import churn (botocore, urllib3,
# importlib metadata) and be pure noise.
#
# 🔴 `plugins.` is deliberately EXCLUDED. Plugin-DISCOVERY tests
# (tests/providers/test_plugin_discovery.py, tests/hermes_cli/test_kanban_*)
# purge `plugins.model_providers.*` on purpose to force a re-scan, and that is
# the behaviour under test — the modules are re-imported by the very next
# discovery call, so they do not poison siblings the way a stale `agent.*`
# module object does. Gating them would produce ~34 false positives on tests
# that are working as designed. Verified 2026-08-09: both files pass on main and
# are not among the suite's cross-test failures.
_WATCHED_PREFIXES = ("run_agent", "agent.", "tools.", "hermes_", "gateway.")


def _watched(name: str) -> bool:
    return name == "run_agent" or name.startswith(_WATCHED_PREFIXES)


def snapshot_watched() -> set:
    """Watched modules currently in ``sys.modules`` (the 'before' half).

    Iterate a LIST copy, never ``sys.modules`` itself. Tests in this suite spawn
    background threads (and the gateway/MCP fixtures import lazily), so a
    concurrent import can insert into ``sys.modules`` mid-comprehension and
    raise ``RuntimeError: dictionary changed size during iteration`` — which
    fails the gate for a reason that has nothing to do with module leaks.
    Observed on CI slice 11/12. ``list(sys.modules)`` snapshots the keys under
    the GIL in one step, so the comprehension can no longer observe a resize.
    """
    return {name for name in list(sys.modules) if _watched(name)}


def leak_failure_message(nodeid: str, removed: set) -> str:
    """Render the failure text for a set of unrestored removals, or '' if clean.

    Split out from the fixture so the gate's LOGIC is directly testable without
    driving pytest internals.
    """
    if not removed:
        return ""
    sample = ", ".join(sorted(removed)[:6])
    more = f" (+{len(removed) - 6} more)" if len(removed) > 6 else ""
    return (
        f"{nodeid} removed {len(removed)} module(s) from sys.modules "
        f"and did not restore them: {sample}{more}\n\n"
        "Every later importer now gets a BRAND-NEW module object, so any test "
        "that captured a reference to (or monkeypatched) one of these will "
        "silently operate on an orphaned copy. This is the bug class fixed in "
        "PR #538 — it cost ~46 suite failures from a single fixture.\n\n"
        "Fix: save and restore around the purge, including parent-module "
        "attributes:\n"
        "    _MISSING = object()\n"
        "    saved = dict(sys.modules)\n"
        "    saved_attrs = {}   # (parent_module, child_name) -> value or _MISSING\n"
        "    ...purge + re-import...\n"
        "    finally:\n"
        "        for n in list(sys.modules):\n"
        "            if n not in saved: sys.modules.pop(n, None)\n"
        "        sys.modules.update(saved)\n"
        "        for (parent, child), v in saved_attrs.items():\n"
        "            parent.__dict__.pop(child, None) if v is _MISSING else "
        "parent.__dict__.__setitem__(child, v)\n\n"
        "If the purge is genuinely required to persist, mark the test "
        "@pytest.mark.allow_sys_modules_purge and say why."
    )


@pytest.fixture(autouse=True)
def _sys_modules_leak_gate(request):
    """Fail a test that DELETES a pre-existing watched module without restoring it.

    Opt out for a test that legitimately must leave the purge in place::

        @pytest.mark.allow_sys_modules_purge
        def test_something(): ...
    """
    if request.node.get_closest_marker("allow_sys_modules_purge"):
        yield
        return

    before = snapshot_watched()
    yield
    removed = before - snapshot_watched()
    message = leak_failure_message(request.node.nodeid, removed)
    if message:
        pytest.fail(message)
