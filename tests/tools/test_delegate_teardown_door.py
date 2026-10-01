"""Source contract for I2 (docs/dev/delegate-child-lifecycle.md): one door.

Every close of a delegated child goes through ``tools.delegate_tool._teardown``
(and its deferred half ``_release_hold``), which refuses while the child's run
or any of its turns is live. Runtime tests cover the paths that exist today
(tests/tools/test_delegate_round5_findings.py, the interleaving property in
test_delegate_child_lifecycle.py). This file fails when a NEW call site
closes a child outside the door, which no runtime test of today's paths can
see. Sites are discovered from the source, never listed:

1. ``_close_child_persistence`` is referenced only inside the door.
2. In tools/delegate_tool.py every ``.close()`` / ``.release_clients()`` call
   is inside ``_close_child_persistence``, except on a DB/connection handle
   (receiver name ending in ``_db``, or ``conn``).
3. In every scanned module, a loop over an agent's ``_active_children``
   (directly, or over a name assigned from an expression that reads it) that
   calls ``<child>.close()`` or ``<child>.release_clients()`` must first hand
   the child to ``_close_delegated_child(<child>, ...)`` and ``continue``.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOOR = {"_teardown", "_release_hold"}
CLOSERS = {"close", "release_clients"}
# Modules that hold AIAgent instances and could close a child.
SCANNED = ["run_agent.py", "cli.py", "agent", "tools", "gateway", "tui_gateway", "acp_adapter"]


def _functions(tree):
    """Map id(node) -> innermost enclosing function name."""
    owner = {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = fn
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            owner[id(child)] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def door_reference_offenders(src: str):
    tree = ast.parse(src)
    owner = _functions(tree)
    refs, bad = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == "_close_child_persistence":
            refs.append(n)
            if owner.get(id(n)) not in DOOR:
                bad.append(f"line {n.lineno} in {owner.get(id(n))}")
    return refs, bad


def delegate_close_offenders(src: str):
    tree = ast.parse(src)
    owner = _functions(tree)
    sites, bad = [], []
    for n in ast.walk(tree):
        if not (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in CLOSERS
        ):
            continue
        recv = n.func.value
        recv_name = recv.id if isinstance(recv, ast.Name) else ast.unparse(recv)
        if recv_name.endswith("_db") or recv_name == "conn":
            continue
        sites.append(n)
        if owner.get(id(n)) != "_close_child_persistence":
            bad.append(f"{recv_name}.{n.func.attr}() line {n.lineno} in {owner.get(id(n))}")
    return sites, bad


def _reads_active_children(expr) -> bool:
    return any(
        isinstance(x, ast.Attribute) and x.attr == "_active_children" for x in ast.walk(expr)
    )


def child_loop_offenders(src: str, label: str):
    """(loops that close children, offenders) for one module's source."""
    tree = ast.parse(src)
    loops, bad = [], []
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in scopes:
        child_lists = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and _reads_active_children(n.value):
                child_lists |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        for loop in ast.walk(fn):
            if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
                continue
            it = loop.iter
            over_children = _reads_active_children(it) or (
                isinstance(it, ast.Name) and it.id in child_lists
            )
            if not over_children:
                continue
            var = loop.target.id
            closes = [
                c
                for stmt in loop.body
                for c in ast.walk(stmt)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr in CLOSERS
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == var
            ]
            if not closes:
                continue
            loops.append(f"{label}:{loop.lineno}")
            first = loop.body[0]
            guarded = (
                isinstance(first, ast.If)
                and isinstance(first.test, ast.Call)
                and isinstance(first.test.func, ast.Name)
                and first.test.func.id == "_close_delegated_child"
                and first.test.args
                and isinstance(first.test.args[0], ast.Name)
                and first.test.args[0].id == var
                and len(first.body) == 1
                and isinstance(first.body[0], ast.Continue)
            )
            if not guarded:
                bad.append(f"{label}:{loop.lineno} closes {var} outside the door")
    return loops, bad


def _scanned_files():
    out = []
    for entry in SCANNED:
        p = REPO / entry
        out.extend([p] if p.is_file() else sorted(p.rglob("*.py")))
    return out


def _delegate_src():
    return (REPO / "tools" / "delegate_tool.py").read_text(encoding="utf-8")


def test_close_child_persistence_is_only_reached_through_the_door():
    refs, bad = door_reference_offenders(_delegate_src())
    assert len(refs) >= 2, "enumerated too few references; the discovery is broken"
    assert not bad, bad


def test_delegate_tool_closes_agents_only_in_close_child_persistence():
    sites, bad = delegate_close_offenders(_delegate_src())
    assert sites, "enumerated ZERO close sites; this contract would be vacuous"
    assert not bad, bad


def test_every_loop_that_closes_active_children_goes_through_the_door():
    loops, bad = [], []
    for path in _scanned_files():
        try:
            src = path.read_text(encoding="utf-8")
            l, b = child_loop_offenders(src, str(path.relative_to(REPO)))
        except (SyntaxError, UnicodeDecodeError):
            continue
        loops += l
        bad += b
    # AIAgent.close() and AIAgent.release_clients() today.
    assert sum(1 for l in loops if l.startswith("run_agent.py:")) >= 2, loops
    assert not bad, bad


# Killer mutations: each contract must catch a NEW bypass, not only re-read
# the sites it already knows.
def test_contracts_catch_a_new_bypass():
    src = _delegate_src()
    mutant = src + (
        "\n\ndef _new_cleanup(child):\n    _close_child_persistence(child)\n"
        "\n\ndef _other_cleanup(child):\n    child.close()\n"
    )
    _, bad = door_reference_offenders(mutant)
    assert any("_new_cleanup" in b for b in bad), bad
    _, bad = delegate_close_offenders(mutant)
    assert any("_other_cleanup" in b for b in bad), bad

    loop_src = (
        "class A:\n"
        "    def close(self):\n"
        "        with self._active_children_lock:\n"
        "            kids = list(self._active_children)\n"
        "        for k in kids:\n"
        "            k.close()\n"
        "    def reset(self):\n"
        "        for k in self._active_children:\n"
        "            k.release_clients()\n"
    )
    loops, bad = child_loop_offenders(loop_src, "mutant")
    assert len(loops) == 2 and len(bad) == 2, (loops, bad)
    guarded = loop_src.replace(
        "        for k in kids:\n",
        "        for k in kids:\n            if _close_delegated_child(k, 'x'):\n                continue\n",
    )
    loops, bad = child_loop_offenders(guarded, "mutant")
    assert len(loops) == 2 and len(bad) == 1, (loops, bad)
