"""C6 (FleetReview backfill, #1007): a turn invalidated by /stop during
pre-flight must not consume the session-keyed native-image buffer.

The gate is ordering inside a ~40k-line coroutine that cannot be driven in a
unit test without a live agent, so this pins the invariant at the source: the
ONE consume site in the turn path is conditioned on ``_run_still_current()``.
"""
from __future__ import annotations

import ast
from pathlib import Path

# parity 2026-10-01: the turn body (TurnRunner.run_sync) lives in gateway/run_turn_runner.py.
RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run_turn_runner.py"


def _consume_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_consume_pending_native_image_paths"
            and ast.unparse(node.args[0]) == "ctx.session_key"
        ):
            yield node


def test_turn_path_image_consume_is_gated_on_run_ownership():
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    calls = list(_consume_calls(tree))
    assert len(calls) == 1, "expected exactly one turn-path consume site"
    guard = parents.get(calls[0])
    assert isinstance(guard, ast.IfExp), "consume must be the body of an ownership conditional"
    assert guard.body is calls[0]
    assert "_run_still_current()" in ast.unparse(guard.test)
