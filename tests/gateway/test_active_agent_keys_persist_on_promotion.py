"""active_agent_keys must be re-persisted when a turn's slot is promoted
sentinel -> real agent.

Measured live 2026-09-19 21:11 (Aegis, mid-turn): ``gateway_state.json`` read
``active_agents=1, active_agent_keys=[]`` for the whole turn. Cause:
``_persist_active_agents`` ran at claim time (slot == ``_AGENT_PENDING_SENTINEL``,
which ``_snapshot_running_agents`` deliberately excludes from the key list) and
again only at release — never at the promotion in ``track_agent``. So every
consumer of the keys was blind for the entire turn:

* the safe-restart watcher's per-session quiescence gate could not tell whether
  the INITIATING session was still running (falls back to global idle, which a
  busy gateway never reaches), and
* the restart-continuity E2E had no session key to check ``boot_resume_scheduled``
  against.

``track_agent`` is a closure inside ``_handle_message_with_agent``, so this is a
source-contract test: it walks the AST and requires that, inside ``track_agent``,
the ``.turn.agent = agent_holder[0]`` promotion is followed by a
``self._persist_active_agents()`` call. A parity merge that drops the call raises
no import error and turns no other test red — this one does.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Upstream (parity 2026-10-01) extracted the ``track_agent`` closure out of
# ``gateway/run.py`` into the ``_run_agent_track_agent`` mixin method in
# ``gateway/run_turn.py``; the contract is unchanged, only the owner moved.
RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run_turn.py"
_TRACK_AGENT_NAMES = ("track_agent", "_run_agent_track_agent")


def _find_track_agent(tree: ast.AST) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in _TRACK_AGENT_NAMES:
            return node
    raise AssertionError("track_agent closure not found in gateway/run_turn.py")


def _is_promotion_assign(stmt: ast.stmt) -> bool:
    """``<expr>.turn.agent = agent_holder[0]``"""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    tgt = stmt.targets[0]
    val = stmt.value
    # Upstream form: ``turn_state.agent, turn_state.ctx = agent_holder[0], turn_ctx``
    if isinstance(tgt, ast.Tuple) and isinstance(val, ast.Tuple) and len(tgt.elts) == len(val.elts):
        return any(_is_promotion_pair(t, v) for t, v in zip(tgt.elts, val.elts))
    return _is_promotion_pair(tgt, val)


def _is_promotion_pair(tgt: ast.expr, val: ast.expr) -> bool:
    if not (isinstance(tgt, ast.Attribute) and tgt.attr == "agent"):
        return False
    owner = tgt.value
    owner_is_turn = (isinstance(owner, ast.Attribute) and owner.attr == "turn") or (
        isinstance(owner, ast.Name) and owner.id == "turn_state"
    )
    if not owner_is_turn:
        return False
    return (isinstance(val, ast.Subscript) and isinstance(val.value, ast.Name)
            and val.value.id == "agent_holder")


def _is_persist_call(stmt: ast.stmt) -> bool:
    """``self._persist_active_agents()`` as a bare statement."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    fn = stmt.value.func
    return (isinstance(fn, ast.Attribute) and fn.attr == "_persist_active_agents"
            and isinstance(fn.value, ast.Name) and fn.value.id == "self")


def test_promotion_is_followed_by_persist_active_agents():
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    fn = _find_track_agent(tree)
    body = fn.body
    promo_idx = [i for i, s in enumerate(body) if _is_promotion_assign(s)]
    assert len(promo_idx) == 1, (
        f"expected exactly one sentinel->agent promotion in track_agent, found {len(promo_idx)}"
    )
    after = body[promo_idx[0] + 1:]
    assert any(_is_persist_call(s) for s in after), (
        "track_agent promotes the slot to the real agent but never calls "
        "self._persist_active_agents() afterwards — gateway_state.json will publish "
        "active_agent_keys=[] for the whole turn (2026-09-19 regression)."
    )


def test_persist_is_unconditional_in_track_agent():
    """The persist must not be nested under ``if self._draining`` (or any other
    branch) — the quiescence gate needs the keys on a normal running gateway."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    fn = _find_track_agent(tree)
    top_level_persist = [s for s in fn.body if _is_persist_call(s)]
    assert top_level_persist, "self._persist_active_agents() must be a top-level statement of track_agent"
