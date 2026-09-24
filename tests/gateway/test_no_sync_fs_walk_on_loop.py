# fork-only: upstream AGENTS.md forbids source-reading tests; do not port
"""Source contract: no filesystem WALK / file READ reachable synchronously from a
coroutine in gateway/run.py.

2026-09-24 04:51 (card t_71ea46ce): an unknown Discord slash command reached
``_handle_message`` -> ``_check_unavailable_skill`` which did
``for skills_dir in get_all_skills_dirs(): for skill_md in skills_dir.rglob("SKILL.md"):
... read_text()`` over ~906 skills on a saturated disk, ON the event loop.
PHASE=event_loop_blocked 10 -> 20 -> 30 s, the loop-liveness watchdog tripped and
Apollo was restarted (reason=loop_liveness_watchdog planned=False).

Sibling of ``test_no_sync_db_on_loop.py`` (SessionDB reads) and
``test_no_sync_syscalls_on_event_loop.py`` (subprocess/sleep/config lock). Those
only check calls lexically inside an ``async def``; this incident was one hop
away, inside a plain ``def`` helper. So this gate also follows the SYNC call
graph inside run.py: a coroutine that directly calls a module-level function
(or ``self.<method>``) which transitively performs a walk/read is an offender.

COVERED shapes: ``<x>.rglob(...)``, ``<x>.glob(...)``, ``<x>.iterdir(...)``,
``<x>.read_text(...)``, ``<x>.read_bytes(...)``, ``os.walk(...)``,
``os.scandir(...)``, ``os.listdir(...)``, ``glob.glob(...)`` / ``glob.iglob(...)``.

EXEMPT: the call (or the helper call) sits inside ``asyncio.to_thread(...)`` /
``run_in_executor(...)`` args, or the helper is passed un-called to them
(``await asyncio.to_thread(helper, arg)`` -- a bare Name, not a Call). Nested
``def``/``lambda`` bodies inside a coroutine are not scanned (they run wherever
they are called). Pre-existing sites are ratcheted in ``KNOWN_OFFENDERS``:
the set may only shrink; a new site fails the build.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FS_WALK_ATTRS = {"rglob", "glob", "iglob", "iterdir", "read_text", "read_bytes", "walk", "scandir", "listdir"}
# ``walk``/``scandir``/``listdir`` only count on the ``os`` module; ``glob``/``iglob``
# count both as ``Path.glob`` and ``glob.glob``.
_OS_ONLY = {"walk", "scandir", "listdir"}
_OFFLOAD = {"to_thread", "run_in_executor"}

# Ratchet: pre-existing on-loop sites at the time the gate landed (2026-09-24).
# Each entry is "<coroutine>-><first hop>". These are single small-file reads
# (breadcrumbs, update-progress files), not tree walks; they are tracked for
# follow-up, not blessed. Shrink this set; never grow it.
KNOWN_OFFENDERS: frozenset[str] = frozenset({
    "start->self._suspend_stuck_loop_sessions",
    "start->self._sweep_restart_initiated_breadcrumbs",
    "_handle_message_with_agent_admitted->self._apply_post_turn_resume_gate",
    "_handle_message_with_agent_admitted->self._consume_restart_initiated_breadcrumb",
    "_watch_update_progress->read_text",
    "_send_update_notification->read_text",
    "_send_update_notification->read_bytes",
    "_send_restart_notification->read_text",
})


def _callee_name(call: ast.Call) -> tuple[str | None, str | None]:
    f = call.func
    if isinstance(f, ast.Attribute):
        base = f.value
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        return f.attr, base_name
    if isinstance(f, ast.Name):
        return f.id, None
    return None, None


def _is_fs_walk(call: ast.Call) -> bool:
    attr, base = _callee_name(call)
    if not isinstance(call.func, ast.Attribute) or attr not in FS_WALK_ATTRS:
        return False
    if attr in _OS_ONLY:
        return base == "os"
    return True


def _direct_calls(fn: ast.AST):
    """Calls lexically in *fn*'s body, skipping nested defs/lambdas and offload args."""
    out = []

    def visit(node, offloaded):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                name, _ = _callee_name(child)
                if name in _OFFLOAD:
                    visit(child, True)
                    continue
                if not offloaded:
                    out.append(child)
            visit(child, offloaded)

    visit(fn, False)
    return out


def _helper_key(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
        return "self." + f.attr
    return None


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sync_fns: dict[str, ast.FunctionDef] = {}
    async_fns: list[ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            sync_fns[node.name] = node
        elif isinstance(node, ast.AsyncFunctionDef):
            async_fns.append(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    sync_fns.setdefault("self." + item.name, item)
                elif isinstance(item, ast.AsyncFunctionDef):
                    async_fns.append(item)

    # Fixed point: which sync helpers transitively walk/read the filesystem?
    walks: dict[str, str] = {}
    for key, fn in sync_fns.items():
        for c in _direct_calls(fn):
            if _is_fs_walk(c):
                walks[key] = f"{_callee_name(c)[0]}() at line {c.lineno}"
                break
    changed = True
    while changed:
        changed = False
        for key, fn in sync_fns.items():
            if key in walks:
                continue
            for c in _direct_calls(fn):
                hk = _helper_key(c)
                if hk in walks:
                    walks[key] = f"{hk}() -> {walks[hk]}"
                    changed = True
                    break

    found = []
    for afn in async_fns:
        for c in _direct_calls(afn):
            if _is_fs_walk(c):
                if f"{afn.name}->{_callee_name(c)[0]}" in KNOWN_OFFENDERS:
                    continue
                found.append(f"{path.name}:{c.lineno} {afn.name}() calls {_callee_name(c)[0]}() on the event loop")
                continue
            hk = _helper_key(c)
            if hk in walks and f"{afn.name}->{hk}" not in KNOWN_OFFENDERS:
                found.append(
                    f"{path.name}:{c.lineno} {afn.name}() calls {hk}() on the event loop, "
                    f"which reaches {walks[hk]}"
                )
    return found


def test_gateway_run_has_no_sync_fs_walk_reachable_from_a_coroutine():
    bad = _offenders(ROOT / "gateway" / "run.py")
    assert not bad, (
        "filesystem walk/read reachable synchronously from a coroutine "
        "(event-loop-block class; wrap with `await asyncio.to_thread(...)`):\n  "
        + "\n  ".join(bad)
    )


def test_known_offenders_ratchet_only_shrinks():
    """Every ratchet entry must still be live; delete it when its site is fixed."""
    tree = ast.parse((ROOT / "gateway" / "run.py").read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for entry in KNOWN_OFFENDERS:
        coro, hop = entry.split("->", 1)
        assert coro in names, f"stale ratchet entry {entry!r}: coroutine is gone, remove it"
    assert not any("_check_unavailable_skill" in e for e in KNOWN_OFFENDERS)


def test_lint_catches_the_2026_09_24_shape(tmp_path):
    p = tmp_path / "run.py"
    p.write_text(
        "def _slug(md):\n"
        "    return md.read_text()\n"
        "def _check(cmd):\n"
        "    for d in dirs():\n"
        "        for md in d.rglob('SKILL.md'):\n"
        "            _slug(md)\n"
        "class R:\n"
        "    async def _handle_message(self, event):\n"
        "        return _check(event.text)\n"
    )
    bad = _offenders(p)
    assert len(bad) == 1 and "run.py:9 _handle_message() calls _check()" in bad[0], bad

    # indirect via a sync method on self, two hops
    p.write_text(
        "import os\n"
        "class R:\n"
        "    def _a(self):\n"
        "        return list(os.walk('.'))\n"
        "    def _b(self):\n"
        "        return self._a()\n"
        "    async def h(self):\n"
        "        self._b()\n"
    )
    assert len(_offenders(p)) == 1

    # direct, lexically in the coroutine
    p.write_text("async def h(p):\n    return p.read_text()\n")
    assert _offenders(p) == ["run.py:2 h() calls read_text() on the event loop"]


def test_lint_accepts_offloaded_forms(tmp_path):
    p = tmp_path / "run.py"
    p.write_text(
        "import asyncio\n"
        "def _check(cmd):\n"
        "    return [m for m in root.rglob('SKILL.md')]\n"
        "async def a(cmd):\n"
        "    return await asyncio.to_thread(_check, cmd)\n"
        "async def b(cmd):\n"
        "    return await asyncio.to_thread(lambda: _check(cmd))\n"
        "async def c(p, loop):\n"
        "    return await loop.run_in_executor(None, p.read_text)\n"
        "async def d(p):\n"
        "    def inner():\n"
        "        return p.read_text()\n"
        "    return await asyncio.to_thread(inner)\n"
        "async def e(p):\n"
        "    return os.path.join(p, 'x')  # not a walk\n"
    )
    assert _offenders(p) == []
