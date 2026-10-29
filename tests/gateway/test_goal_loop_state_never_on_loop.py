# fork-only: the static half reads source (upstream AGENTS.md forbids that); do not port.
"""Goal/loop state is never loaded or written on the gateway event-loop thread.

GoalManager/LoopManager construction calls load_goal/load_loop -> SessionDB.get_meta, which takes
SessionDB._lock. Any writer holding that lock (a WAL checkpoint, a big transcript append) then froze
EVERY platform: 169 PHASE=event_loop_blocked get_meta stalls of 10-30 s on Apollo, 2026-10-19..26,
124 of them from the 15 s goal idle-wake scan building a GoalManager inline (gateway/run_goals.py
_goal_idle_wake_one). Every such call now hops through _run_in_executor_with_context.

The guard: inside any ``async def`` of the goal/loop gateway mixins, a DIRECT call to a state
loader/writer is a failure. Passing the callable to the executor (``executor(LoopManager, sid)``) or
calling it inside a lambda / nested sync def handed to the executor is fine; those run on a worker.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ("gateway/run_goals.py", "gateway/slash_commands_goals.py")
# Each of these reads or writes state_meta under SessionDB._lock.
BLOCKING = {"GoalManager", "LoopManager", "load_goal", "load_loop", "goal_blocks_loop_tick",
            "dispatch_loop_command"}


def _direct_calls(fn: ast.AsyncFunctionDef):
    """Calls in ``fn``'s own body: not inside a lambda or a nested def (those run where they are sent)."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _offenders(src: str, path: str):
    out = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for call in _direct_calls(fn):
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name in BLOCKING:
                out.append(f"{path}:{call.lineno} {fn.name} calls {name}() on the event loop")
    return out


def test_goal_loop_state_is_never_touched_on_the_event_loop():
    found = []
    for rel in FILES:
        found += _offenders((ROOT / rel).read_text(), rel)
    assert not found, "hop these through self._run_in_executor_with_context:\n" + "\n".join(found)


def test_guard_catches_the_original_shape():
    # the pre-fix _goal_idle_wake_one line, verbatim shape
    bad = (
        "class M:\n"
        "    async def _goal_idle_wake_one(self, sid):\n"
        "        mgr = GoalManager(session_id=sid, default_max_turns=3)\n"
        "        if goal_blocks_loop_tick(sid):\n"
        "            return\n"
    )
    found = _offenders(bad, "x.py")
    assert len(found) == 2
    assert any("GoalManager()" in f for f in found) and any("goal_blocks_loop_tick()" in f for f in found)


def test_guard_allows_executor_hops():
    ok = (
        "class M:\n"
        "    async def f(self, sid):\n"
        "        mgr = await self._run_in_executor_with_context(LoopManager, sid)\n"
        "        g = await self._run_in_executor_with_context(lambda: GoalManager(session_id=sid))\n"
        "        def _load():\n"
        "            return lambda s: GoalManager(session_id=s)\n"
        "        return mgr, g, _load\n"
    )
    assert _offenders(ok, "x.py") == []
