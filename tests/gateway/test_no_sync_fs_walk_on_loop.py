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
# Upstream (parity 2026-10-01) split both god-files into mixin modules composing ONE class
# (gateway/run_*.py -> GatewayRunner, gateway/slash_commands_*.py -> GatewaySlashCommandsMixin).
# Each family is scanned as one unit so the same-module call graph still sees a coroutine in
# run_turn.py calling a sync helper defined in run.py (or vice versa). Offender keys carry the
# file the coroutine actually lives in.
SCANNED_FAMILIES = (("gateway/run.py", "run_*.py"), ("gateway/slash_commands.py", "slash_commands_*.py"))
_RUNNER_CLASSES = frozenset({"GatewayRunner", "GatewaySlashCommandsMixin"})

_WALK_ATTRS = frozenset({"rglob", "glob", "iglob"})
_READ_ATTRS = frozenset({"read_text", "read_bytes"})

# (file, coroutine, label) -> number of read sites, captured 2026-09-24
# (counts added 2026-09-28, FleetReview #113: a set of keys hid a NEW read at
# an already-baselined coroutine). Update-/restart-notification paths reading
# one small state file. To fix one: offload it, then lower/delete its count
# here (a stale count fails).
READ_BASELINE = {
    # Re-keyed 2026-10-02 (parity sync 2026-10-01, CI6-M1-gatewayb): upstream split run.py into
    # run_*.py, so the (file, coroutine) keys moved; counts are the same sites. Net 12 -> 10:
    # two of _stop_impl_body's three reads left the loop in upstream's shutdown split, and the
    # upstream-new _replay_pending_planned_restart_notification read was offloaded here.
    # via one-hop sync helpers (breadcrumb / resume-pending / stuck-loop state files)
    "gateway/run_turn.py _handle_message_with_agent_admitted -> read:.read_text": 1,
    "gateway/run_shutdown.py _stop_persist_exit_state -> read:.read_text": 1,
    "gateway/run_startup.py _start_recover_previous_run -> read:.read_text": 1,
    "gateway/slash_commands.py _handle_reset_command -> read:.read_text": 1,
    # direct
    "gateway/run_notifications.py _watch_update_progress -> read:.read_text": 3,
    "gateway/run_notifications.py _send_update_notification -> read:.read_text": 2,
    "gateway/run_notifications.py _send_update_notification -> read:.read_bytes": 1,
    "gateway/run_notifications.py _send_restart_notification -> read:.read_text": 1,
}


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


def _class_key(name: str) -> str:
    """``*Mixin`` classes composing the runner collapse onto one namespace so ``self.<name>``
    resolves across the split modules (a mixin method is a GatewayRunner method at runtime)."""
    return "GatewayRunner" if name.endswith("Mixin") or name in _RUNNER_CLASSES else name


def _walkers(trees: list[tuple[ast.Module, list[str]]]) -> dict[str, str]:
    """Map helper key -> worst shape label ("walk:*" beats "read:*") over a module family."""
    sync: dict[str, tuple[ast.FunctionDef, str | None, list[str]]] = {}
    for tree, lines in trees:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                sync[node.name] = (node, None, lines)
            elif isinstance(node, ast.ClassDef):
                cls = _class_key(node.name)
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        sync[f"{cls}.{item.name}"] = (item, cls, lines)
    labels: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for key, (fn, cls, lines) in sync.items():
            best = labels.get(key)
            for call in _body_calls(fn):
                if _noqa_exempt(lines[call.lineno - 1]):
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


def _offenders(path: Path, rel: str) -> list[str]:
    """Single-file form (the mutation arm uses it)."""
    return _family_offenders([(path, rel)])


def _family_offenders(files: list[tuple[Path, str]]) -> list[str]:
    parsed: list[tuple[str, ast.Module, list[str]]] = []
    for path, rel in files:
        src = path.read_text(encoding="utf-8")
        parsed.append((rel, ast.parse(src), src.splitlines()))
    walkers = _walkers([(tree, lines) for _rel, tree, lines in parsed])
    found: list[str] = []

    def scan(rel: str, lines: list[str], fn: ast.AsyncFunctionDef, cls: str | None) -> None:
        for call in _iter_loop_calls(fn):
            lab = _shape(call)
            if lab is None:
                callee = _callee_key(call, cls)
                if callee in walkers:
                    lab = walkers[callee].split(" via ")[0] + f" via {callee}"
            if lab and not _noqa_exempt(lines[call.lineno - 1]):
                found.append(f"{rel}:{call.lineno} {fn.name} -> {lab}")

    def visit(rel: str, lines: list[str], node: ast.AST, cls: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(rel, lines, child, _class_key(child.name))
            elif isinstance(child, ast.AsyncFunctionDef):
                scan(rel, lines, child, cls)
                visit(rel, lines, child, cls)
            else:
                visit(rel, lines, child, cls)

    for rel, tree, lines in parsed:
        visit(rel, lines, tree, None)
    return found


def _key(offender: str) -> str:
    key = re.sub(r"^([^:]+):\d+ ", r"\1 ", offender)
    return key.split(" via ")[0]


def _family_files(anchor: str, sibling_glob: str) -> list[tuple[Path, str]]:
    anchor_path = ROOT / anchor
    siblings = sorted(anchor_path.parent.glob(sibling_glob))
    return [(anchor_path, anchor)] + [(p, p.relative_to(ROOT).as_posix()) for p in siblings]


def _all_offenders() -> list[str]:
    out: list[str] = []
    for anchor, sibling_glob in SCANNED_FAMILIES:
        out.extend(_family_offenders(_family_files(anchor, sibling_glob)))
    return out


def test_no_tree_walk_reaches_the_event_loop():
    bad = [o for o in _all_offenders() if "-> walk:" in o]
    assert not bad, (
        "filesystem tree walk reachable on the gateway event loop (the 2026-09-24 "
        "loop_liveness_watchdog class) -- offload with asyncio.to_thread or cache:\n  "
        + "\n  ".join(bad)
    )


def test_single_file_reads_on_loop_do_not_grow():
    from collections import Counter

    reads = Counter(_key(o) for o in _all_offenders() if "-> read:" in o)
    new = sorted(f"{k} (x{n}, baseline x{READ_BASELINE.get(k, 0)})"
                 for k, n in reads.items() if n > READ_BASELINE.get(k, 0))
    gone = sorted(f"{k} (x{reads.get(k, 0)}, baseline x{n})"
                  for k, n in READ_BASELINE.items() if reads.get(k, 0) < n)
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
    n = 0
    for path, _rel in _family_files(*SCANNED_FAMILIES[0]):
        src = path.read_text(encoding="utf-8")
        n += sum(isinstance(n_, ast.AsyncFunctionDef) for n_ in ast.walk(ast.parse(src)))
    assert n >= 200, n
