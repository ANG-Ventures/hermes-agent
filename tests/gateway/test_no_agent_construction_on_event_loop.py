"""Source contract: heavy synchronous work never runs directly on the gateway
event loop.

Incident 2026-09-24 (Apollo): after the LCM freeze-#3 fix (#966) cut the
context-engine load from 13 min to 1-9 s, the Discord websocket STILL dropped
7x in 50 min.  ``PHASE=event_loop_blocked`` named
``plugins/context_engine/__init__.py _load_engine_from_dir`` (20-60 s) and
``gateway/run.py _check_unavailable_skill`` — both reached from ``async def``
bodies: session hygiene built its ``AIAgent`` inline (the engine load waits on
the process-global ``_LOAD_LOCK`` held by concurrent worker turns), ``/compress``
did the same, and an unknown ``/command`` walked 900+ SKILL.md files with
``rglob`` + ``read_text``.  Discord's heartbeat ACK window is ~41 s; any of
these crossing it closes the socket, and every reconnect re-registers 900
skills and re-injects backfill while Ace's turns land as stubs.

The fix hands each of those calls to ``asyncio.to_thread``.  This test locks
the class, not the three sites: no ``async def`` in the gateway package may
call ``AIAgent(...)`` or ``_check_unavailable_skill(...)`` directly.  A call
inside a nested ``def``/``lambda`` (the ``to_thread`` payload) is fine — the
walk stops at nested function scopes.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILES = [REPO / "gateway" / "run.py", REPO / "gateway" / "slash_commands.py"]
# Constructors / helpers measured blocking the loop.  Extend when a new
# ``PHASE=event_loop_blocked`` site is root-caused to a direct call.
BLOCKING_CALLEES = {
    "AIAgent",
    "_check_unavailable_skill",
    # The context-engine load waits on the process-global _LOAD_LOCK (held for
    # the whole LCM engine build by whichever worker turn got there first).
    "load_context_engine",
    "_load_engine_from_dir",
    # Persisted route lookup takes SessionStore._lock (a threading.Lock held by
    # worker threads across routing load/save): ~100 s loop stall, t_ac9e21cf.
    "_persisted_session_route_identity",
    "lookup_persisted_route_identity",
}


def _callee_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _direct_calls_in_async_bodies(tree: ast.AST):
    """Yield (async_fn_name, lineno, callee) for direct calls in async bodies.

    Nested ``def``/``async def``/``lambda`` scopes are not descended: a call
    inside them is executed by whoever invokes that closure (to_thread), not by
    the coroutine itself.
    """
    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.hits = []
            self._async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self._async_stack.append(node.name)
            for stmt in node.body:
                self._scan(stmt)
            self._async_stack.pop()

        def _scan(self, node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if isinstance(node, ast.AsyncFunctionDef):
                    self.visit_AsyncFunctionDef(node)
                return
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name in BLOCKING_CALLEES:
                    self.hits.append((self._async_stack[-1], node.lineno, name))
            for child in ast.iter_child_nodes(node):
                self._scan(child)

    w = Walker()
    w.visit(tree)
    return w.hits


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_no_blocking_construction_directly_in_async_def(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _direct_calls_in_async_bodies(tree)
    assert not hits, (
        f"{path.name}: heavy sync call(s) directly on the event loop — wrap in "
        f"asyncio.to_thread (see module docstring): "
        + ", ".join(f"{fn}:{ln} {callee}(...)" for fn, ln, callee in hits)
    )


def test_walker_catches_a_direct_call_and_ignores_to_thread_payload():
    """Negative control: the contract must actually fire."""
    bad = ast.parse(
        "async def h():\n"
        "    a = AIAgent(model='x')\n"
        "    b = await asyncio.to_thread(lambda: AIAgent(model='y'))\n"
        "    def inner():\n"
        "        return _check_unavailable_skill('z')\n"
        "    return a, b, inner\n"
    )
    hits = _direct_calls_in_async_bodies(bad)
    assert hits == [("h", 2, "AIAgent")]


# ---------------------------------------------------------------------------
# One-hop ratchet: sync GatewayRunner helpers that reach ``self.session_store``
# ---------------------------------------------------------------------------
#
# ``test_async_session_store`` forbids ``self.session_store.X(...)`` directly in
# an ``async def``.  The t_ac9e21cf stall slipped past it one hop away: an async
# body called a SYNC runner method, and THAT method called the store (which
# takes ``SessionStore._lock``).  This ratchet flags every direct call, from an
# ``async def``, to a sync ``GatewayRunner`` method whose body references
# ``session_store``.  Existing sites are pinned by name below; the list may only
# shrink.  A new site must be offloaded (``asyncio.to_thread`` or
# ``self.async_session_store``) instead of being added here.
KNOWN_ONE_HOP_STORE_CALLS = {
    # (async caller, sync callee) — burn-down tracked on the follow-up card.
    ("_announce_switch", "_session_key_for_source"),
    ("_defer_goal_status_notice_after_delivery", "_session_key_for_source"),
    ("_handle_message", "_session_key_for_source"),
    ("_handle_message_with_agent_admitted", "_apply_post_turn_resume_gate"),
    ("_handle_message_with_agent_admitted", "_session_key_for_source"),
    ("_handler", "_session_key_for_source"),
    ("_loop_wakeup_watcher", "_build_process_event_source"),
    ("_loop_wakeup_watcher", "_session_key_for_source"),
    ("_maybe_confirm_destructive_slash", "_session_key_for_source"),
    ("_platform_reconnect_watcher", "_schedule_resume_pending_sessions"),
    ("_post_turn_goal_continuation", "_session_key_for_source"),
    ("_prepare_auto_resume_decisions", "_reconcile_deferred_restarts_at_boot"),
    ("_prepare_auto_resume_decisions", "_sweep_resume_requests"),
    ("_prepare_inbound_message_text", "_session_key_for_source"),
    ("_request_slash_confirm", "_session_key_for_source"),
    ("_restore_resume_pending_sessions_at_startup", "_schedule_resume_pending_sessions"),
    ("_restore_telegram_topic_session", "_session_key_for_source"),
    ("_run_agent_admitted", "_session_key_for_source"),
    ("_run_secondary_profile_reconnect", "_configure_profile_adapter"),
    ("_session_expiry_watcher", "_sweep_idle_cached_agents"),
    ("_start_one_profile_adapters", "_configure_profile_adapter"),
    ("_stop_impl_body", "_mark_resume_pending_for_shutdown"),
    ("start", "_schedule_resume_pending_sessions"),
    ("start", "_suspend_stuck_loop_sessions"),
}


def _sync_runner_methods_touching_store(tree: ast.AST) -> set[str]:
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "GatewayRunner"
    )
    out = set()
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Attribute)
                and n.attr == "session_store"
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
            ) or (isinstance(n, ast.Constant) and n.value == "session_store"):
                out.add(fn.name)
                break
    return out


def _one_hop_store_calls(tree: ast.AST, callees: set[str]) -> set[tuple[str, str]]:
    hits: set[tuple[str, str]] = set()

    def scan(node, owner):
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.AsyncFunctionDef) and node is not owner:
            for stmt in node.body:
                scan(stmt, node)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in callees
        ):
            hits.add((owner.name, node.func.attr))
        for child in ast.iter_child_nodes(node):
            scan(child, owner)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for stmt in node.body:
                scan(stmt, node)
    return hits


def test_one_hop_session_store_calls_on_loop_only_shrink():
    path = REPO / "gateway" / "run.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _one_hop_store_calls(tree, _sync_runner_methods_touching_store(tree))
    new = sorted(hits - KNOWN_ONE_HOP_STORE_CALLS)
    stale = sorted(KNOWN_ONE_HOP_STORE_CALLS - hits)
    assert not new, (
        "async def calls a sync GatewayRunner method that reaches session_store "
        "(SessionStore._lock) on the event loop — wrap it in asyncio.to_thread: "
        + ", ".join(f"{a} -> {c}" for a, c in new)
    )
    assert not stale, (
        "fixed site(s) still pinned in KNOWN_ONE_HOP_STORE_CALLS — remove them "
        "so the ratchet tightens: " + ", ".join(f"{a} -> {c}" for a, c in stale)
    )


def test_one_hop_walker_fires_on_the_t_ac9e21cf_shape():
    """Negative control: the exact regressed shape must be caught."""
    bad = ast.parse(
        "class GatewayRunner:\n"
        "    def _lookup(self, k):\n"
        "        return self.session_store.lookup_persisted_route_identity(k)\n"
        "    async def handle(self, k):\n"
        "        a = self._lookup(k)\n"
        "        b = await asyncio.to_thread(self._lookup, k)\n"
        "        return a, b\n"
    )
    callees = _sync_runner_methods_touching_store(bad)
    assert callees == {"_lookup"}
    assert _one_hop_store_calls(bad, callees) == {("handle", "_lookup")}
