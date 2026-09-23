"""Source contract: no synchronous SessionDB read may run on the gateway event loop.

2026-09-23: gateway/run.py::_loop_wakeup_watcher called hermes_cli.loops.list_active_loops()
directly inside a coroutine. That read goes through SessionDB._read_ctx, which beyond
_READ_POOL_MAX degrades to the shared writer lock, so under load the event loop blocked
10-50 s per tick (PHASE=event_loop_blocked, site=hermes_state.py list_meta_prefix, 16/19
stalls) — every Discord interaction missed its 3 s ack ("The application did not respond")
and the gateway read as frozen. The fix is `await asyncio.to_thread(...)`; this test makes
the class un-shippable for the accessors listed below.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNC_DB_ACCESSORS = {
    "list_active_loops", "list_meta_prefix", "get_meta", "set_meta",
    "load_session", "get_session_messages", "search_content",
}


def _offenders(path: Path):
    src = path.read_text()
    lines = src.splitlines()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name not in SYNC_DB_ACCESSORS:
                continue
            line = lines[sub.lineno - 1]
            if "to_thread" in line or "run_in_executor" in line:
                continue
            found.append(f"{path.name}:{sub.lineno} {node.name}() calls {name}() on the event loop")
    return found


def test_gateway_run_has_no_sync_session_db_call_on_the_event_loop():
    bad = _offenders(ROOT / "gateway" / "run.py")
    assert not bad, (
        "synchronous SessionDB read inside a coroutine (this is the event-loop-block class):\n  "
        + "\n  ".join(bad)
    )


def test_lint_catches_the_2026_09_23_shape(tmp_path):
    p = tmp_path / "run.py"
    p.write_text("async def w(self):\n    for sid, st in list_active_loops():\n        pass\n")
    assert _offenders(p) == ["run.py:2 w() calls list_active_loops() on the event loop"]
    p.write_text("async def w(self):\n    for sid, st in await asyncio.to_thread(list_active_loops):\n        pass\n")
    assert _offenders(p) == []
