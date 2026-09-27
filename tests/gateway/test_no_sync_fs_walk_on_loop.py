# fork-only: upstream AGENTS.md forbids source-reading tests; do not port
"""AST contract: no filesystem TREE WALK runs on the gateway event loop.

2026-09-24 04:51 Apollo was hard-killed by the loop-liveness watchdog
(restart_notice reason=loop_liveness_watchdog planned=False).  gateway.error.log
named the site: ``PHASE=event_loop_blocked seconds=10/20/30`` at
``_check_unavailable_skill`` -> ``skills_dir.rglob("SKILL.md")`` +
``_skill_slug_from_frontmatter`` -> ``read_text()``.  An unknown ``/command``
made ``_handle_message`` walk ~906 SKILL.md files synchronously, on a disk
saturated by kanban workers, until the loop stalled past the 90 s watchdog.

The sibling gate (``test_no_sync_syscalls_on_event_loop.py``) only looks at
calls LEXICALLY inside an ``async def``.  This incident was one hop away: the
coroutine called a plain ``def`` helper that did the walk.  So this gate adds
ONE thing the sibling lacks -- a same-module call graph:

  * A sync function is a WALKER if its body (not nested defs/lambdas) calls a
    tree-walk shape, or calls another WALKER in the same module (fixed point).
    Resolution is by bare name for module-level functions and by
    ``self.<name>`` for sync methods of the enclosing class.
  * Inside every ``async def`` (not descending into nested def/lambda, skipping
    the argument subtrees of ``asyncio.to_thread`` / ``run_in_executor``, and
    skipping awaited calls -- reusing the sibling's walker), a call to a walk
    shape or to a WALKER is an offender.

SHAPES
  walk (hard zero):  ``<x>.rglob(...)``, ``<x>.glob(...)``, ``<x>.iglob(...)``,
                     ``os.walk(...)``
  read (ratcheted):  ``<x>.read_text(...)``, ``<x>.read_bytes(...)`` -- a single
                     small-file read.  Pre-existing sites are frozen in
                     ``READ_BASELINE`` (may shrink, must not grow).

EXEMPTION: ``# noqa: sync-on-loop <reason>`` on the call line (reason required).

DOES-NOT-COVER: cross-module helpers, aliased callables (``f = helper``),
``getattr`` dispatch, ``open().read()``, ``os.listdir``/``scandir`` (single
directory, not a tree).  This is a regression gate, not a proof.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.gateway.test_no_sync_syscalls_on_event_loop import (
    _iter_loop_calls,
    _noqa_exempt,
)

ROOT = Path(__file__).resolve().parents[2]
SCANNED_FILES = ("gateway/run.py", "gateway/slash_commands.py")

_WALK_ATTRS = frozenset({"rglob", "glob", "iglob"})
_READ_ATTRS = frozenset({"read_text", "read_bytes"})

# (file, coroutine, label) of pre-existing single-file reads, captured
# 2026-09-24.  Update-/restart-notification paths reading one small state file.
# To fix one: offload it, then DELETE its line here (a stale entry fails).
READ_BASELINE = frozenset({
    # via one-hop sync helpers (breadcrumb / resume-pending / stuck-loop state files)
    "gateway/run.py _handle_message_with_agent_admitted -> read:.read_text",
    "gateway/run.py _stop_impl_body -> read:.read_text",
    "gateway/run.py start -> read:.read_text",
    "gateway/slash_commands.py _handle_reset_command -> read:.read_text",
    # direct
    "gateway/run.py _watch_update_progress -> read:.read_text",
    "gateway/run.py _send_update_notification -> read:.read_text",
    "gateway/run.py _send_update_notification -> read:.read_bytes",
    "gateway/run.py _send_restart_notification -> read:.read_text",
})


def _shape(call: ast.Call) -> str | None:
    f = call.func
    if not isinstance(f, ast.Attribute):
        return None
    if f.attr in _WALK_ATTRS:
        return f"walk:.{f.attr}"
    if f.attr == "walk" and isinstance(f.value, ast.Name) and f.value.id == "os":
        return "walk:os.walk"
    if f.attr in _READ_ATTRS:
        return f"read:.{f.attr}"
    return None


def _body_calls(fn: ast.AST):
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _callee_key(call: ast.Call, cls: str | None) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if (
        cls is not None
        and isinstance(f, ast.Attribute)
        and isinstance(f.value, ast.Name)
        and f.value.id == "self"
    ):
        return f"{cls}.{f.attr}"
    return None


def _walkers(tree: ast.Module) -> dict[str, str]:
    """Map helper key -> worst shape label ("walk:*" beats "read:*")."""
    sync: dict[str, tuple[ast.FunctionDef, str | None]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            sync[node.name] = (node, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    sync[f"{node.name}.{item.name}"] = (item, node.name)
    labels: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for key, (fn, cls) in sync.items():
            best = labels.get(key)
            for call in _body_calls(fn):
                if _noqa_exempt(_LINES[call.lineno - 1]):
                    continue
                lab = _shape(call)
                if lab is None:
                    callee = _callee_key(call, cls)
                    if callee in labels and callee != key:
                        lab = labels[callee].split(" via ")[0] + f" via {callee}"
                if lab and (best is None or (lab.startswith("walk:") and not best.startswith("walk:"))):
                    best = lab
            if best is not None and best != labels.get(key):
                labels[key] = best
                changed = True
    return labels


_LINES: list[str] = []


def _offenders(path: Path, rel: str) -> list[str]:
    global _LINES
    src = path.read_text(encoding="utf-8")
    _LINES = src.splitlines()
    tree = ast.parse(src)
    walkers = _walkers(tree)
    found: list[str] = []

    def scan(fn: ast.AsyncFunctionDef, cls: str | None) -> None:
        for call in _iter_loop_calls(fn):
            lab = _shape(call)
            if lab is None:
                callee = _callee_key(call, cls)
                if callee in walkers:
                    lab = walkers[callee].split(" via ")[0] + f" via {callee}"
            if lab and not _noqa_exempt(_LINES[call.lineno - 1]):
                found.append(f"{rel}:{call.lineno} {fn.name} -> {lab}")

    def visit(node: ast.AST, cls: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, ast.AsyncFunctionDef):
                scan(child, cls)
                visit(child, cls)
            else:
                visit(child, cls)

    visit(tree, None)
    return found


def _key(offender: str) -> str:
    key = re.sub(r"^([^:]+):\d+ ", r"\1 ", offender)
    return key.split(" via ")[0]


def _all_offenders() -> list[str]:
    out: list[str] = []
    for rel in SCANNED_FILES:
        out.extend(_offenders(ROOT / rel, rel))
    return out


def test_no_tree_walk_reaches_the_event_loop():
    bad = [o for o in _all_offenders() if "-> walk:" in o]
    assert not bad, (
        "filesystem tree walk reachable on the gateway event loop (the 2026-09-24 "
        "loop_liveness_watchdog class) -- offload with asyncio.to_thread or cache:\n  "
        + "\n  ".join(bad)
    )


def test_single_file_reads_on_loop_do_not_grow():
    reads = {_key(o) for o in _all_offenders() if "-> read:" in o}
    new = sorted(reads - READ_BASELINE)
    gone = sorted(READ_BASELINE - reads)
    assert not new, "new synchronous file read on the event loop:\n  " + "\n  ".join(new)
    assert not gone, (
        "baseline entry no longer present -- delete it from READ_BASELINE:\n  "
        + "\n  ".join(gone)
    )


def test_gate_catches_the_2026_09_24_shape(tmp_path):
    p = tmp_path / "run.py"
    p.write_text(
        "def _slug(md):\n"
        "    return md.read_text()\n"
        "def _check(name):\n"
        "    for d in dirs():\n"
        "        for md in d.rglob('SKILL.md'):\n"
        "            _slug(md)\n"
        "class R:\n"
        "    def _helper(self):\n"
        "        return _check('x')\n"
        "    async def _handle_message(self, ev):\n"
        "        a = _check(ev)\n"
        "        b = self._helper()\n"
        "        c = await asyncio.to_thread(_check, ev)\n"
        "        d = _check(ev)  # noqa: sync-on-loop fixture exemption\n"
    )
    got = sorted(_offenders(p, "run.py"))
    assert got == [
        "run.py:11 _handle_message -> walk:.rglob via _check",
        "run.py:12 _handle_message -> walk:.rglob via R._helper",
    ], got


def test_gate_is_not_vacuous():
    src = (ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    n = sum(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(ast.parse(src)))
    assert n >= 200, n
