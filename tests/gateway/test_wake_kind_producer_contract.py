"""Every wake producer in gateway/ names its kind (t_17ba87a6).

The progress header (``🔔 goal wake · check-in 1/3``, ``🔔 kanban wake · review · t_X``) reads
``wake_kind`` from its producer; it no longer guesses from the wake's prose. This contract fails any
``deliver_wake(`` call, any ``MessageEvent(..., internal=<not False>)`` construction and any
``_synthetic_prompt_event(`` call in gateway/ that omits ``wake_kind=``. That way an upstream
producer added by a parity merge cannot render as an unlabeled wake again.
"""

from __future__ import annotations

import ast
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[2] / "gateway"
#: Always wake producers, whatever their arguments.
_ALWAYS = frozenset({"deliver_wake", "_synthetic_prompt_event"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_wake_construction(node: ast.Call) -> bool:
    name = _call_name(node)
    if name in _ALWAYS:
        return True
    if name != "MessageEvent":
        return False
    internal = next((k.value for k in node.keywords if k.arg == "internal"), None)
    return internal is not None and not (isinstance(internal, ast.Constant) and internal.value is False)


def unlabeled_wake_producers(root: Path = GATEWAY) -> list[str]:
    """``path:line call`` for every wake producer in ``root`` that passes no ``wake_kind=``."""
    missing = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_wake_construction(node):
                if not any(k.arg == "wake_kind" for k in node.keywords):
                    missing.append(f"{path.relative_to(root.parent)}:{node.lineno} {_call_name(node)}(")
    return missing


def test_every_gateway_wake_producer_names_its_kind():
    missing = unlabeled_wake_producers()
    assert not missing, (
        "wake producers without wake_kind= (their progress header falls back to a guessed "
        "'🔔 wake · internal'; pass a kind from gateway/wake_kind.py):\n  " + "\n  ".join(missing))


def test_contract_sees_the_producers_it_guards():
    """The scan is not vacuous: it finds the known producers (a rename would silently pass it)."""
    found = {}
    for path in sorted(GATEWAY.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_wake_construction(node):
                found[_call_name(node)] = found.get(_call_name(node), 0) + 1
    assert found.get("deliver_wake", 0) >= 8, found
    assert found.get("MessageEvent", 0) >= 8, found
    assert found.get("_synthetic_prompt_event", 0) >= 4, found
