"""Source contract: no synchronous network I/O may be reachable from a slash-command coroutine.

2026-09-24 (card t_515b7fce): Discord ``/model <name>`` answered "The application did not
respond". The event-loop watchdog (PHASE=event_loop_blocked seconds=10) caught the stack:

    slash_commands._handle_model_command -> _finish_switch (async)
      -> GatewayRunner._set_session_model_override            (sync, called inline)
      -> _model_override_is_persistable -> _reresolve_model_override_credentials
      -> hermes_cli.model_switch.switch_model -> models.validate_requested_model
      -> fetch_api_models -> urllib.request.urlopen(timeout=5.0) -> socket.connect

That GET of ``<bridge base_url>/v1/models`` ran ON the asyncio loop, so the Discord
interaction missed its 3 s ack deadline ("interaction expired before defer" x33 in one
night) and every other conversation stalled for the probe duration.

Unlike ``test_no_sync_db_on_loop.py`` (a fixed accessor list), this lint computes
*reachability*: it indexes every function defined in the modules on the slash path, marks
the ones whose body touches a network primitive (urllib / requests / httpx / http.client /
socket), propagates "blocking" through the call graph by bare callee name, and then fails
if any coroutine in ``gateway/slash_commands.py`` calls a blocking function *directly*.
Passing the function to ``asyncio.to_thread`` / ``run_in_executor`` is a reference, not a
call, so it is (correctly) not flagged.

Name resolution is by bare name, so it over-approximates; that is the safe direction for a
lint. Nested *sync* defs inside a coroutine are not scanned as loop code — in this file
they exist to be shipped to a worker thread.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Coroutines in these files are "the slash-command coroutine path".
SLASH_FILES = [ROOT / "gateway" / "slash_commands.py"]

# Every module the slash path calls into synchronously; functions defined here are
# indexed for the reachability pass.
INDEXED_FILES = [
    ROOT / "gateway" / "run.py",
    ROOT / "gateway" / "slash_commands.py",
    ROOT / "hermes_cli" / "model_switch.py",
    ROOT / "hermes_cli" / "models.py",
]

# Direct network primitives (callee attr/name).
NET_NAMES = {
    "urlopen",
    "create_connection",
    "getaddrinfo",
    "HTTPConnection",
    "HTTPSConnection",
}
# ``<module>.<attr>`` calls that are network I/O.
NET_MODULE_ATTRS = {
    "requests": {"get", "post", "put", "patch", "delete", "head", "request", "Session"},
    "httpx": {"get", "post", "put", "patch", "delete", "head", "request", "Client"},
    "socket": {"socket", "create_connection", "getaddrinfo"},
}

# Bare names that collide with unrelated builtins/dict methods; never treat as edges.
_GENERIC = {
    "get", "set", "pop", "update", "items", "keys", "values", "append", "extend",
    "join", "split", "strip", "lower", "upper", "format", "copy", "close", "open",
    "read", "write", "run", "main", "load", "save", "send", "start", "stop", "__init__",
}


def _callee_name(call: ast.Call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _is_net_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute):
        if f.attr in NET_NAMES:
            return True
        base = f.value
        if isinstance(base, ast.Name) and f.attr in NET_MODULE_ATTRS.get(base.id, ()):
            return True
        return False
    return isinstance(f, ast.Name) and f.id in NET_NAMES


def _own_calls(fn):
    """Calls made by *fn*'s own body — not by nested defs/lambdas inside it."""
    out = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _awaited_calls(fn):
    ids = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            ids.add(id(node.value))
    return ids


def build_blocking_index(files):
    """Return {bare_name: witness_chain} for every sync function that reaches network I/O."""
    edges: dict[str, set[str]] = {}
    direct: dict[str, str] = {}
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):  # coroutines are never "blocking callees"
                continue
            name = node.name
            if name in _GENERIC:
                continue
            for call in _own_calls(node):
                if _is_net_call(call):
                    direct.setdefault(name, f"{path.name}:{call.lineno} {_callee_name(call)}()")
                callee = _callee_name(call)
                if callee and callee not in _GENERIC and callee != name:
                    edges.setdefault(name, set()).add(callee)
    blocking = {n: [n, w] for n, w in direct.items()}
    changed = True
    while changed:
        changed = False
        for caller, callees in edges.items():
            if caller in blocking:
                continue
            for c in sorted(callees):
                if c in blocking:
                    blocking[caller] = [caller] + blocking[c]
                    changed = True
                    break
    return blocking


def offenders(slash_files, indexed_files):
    blocking = build_blocking_index(indexed_files)
    found = []
    for path in slash_files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            awaited = _awaited_calls(node)
            for call in _own_calls(node):
                if id(call) in awaited:
                    continue  # awaiting a coroutine (e.g. asyncio.to_thread(...)) is fine
                if _is_net_call(call):
                    found.append(f"{path.name}:{call.lineno} {node.name}() -> {_callee_name(call)}() [network]")
                    continue
                name = _callee_name(call)
                if name in blocking:
                    found.append(
                        f"{path.name}:{call.lineno} {node.name}() -> " + " -> ".join(blocking[name])
                    )
    return found


def test_slash_command_coroutines_never_reach_sync_network_io():
    bad = offenders(SLASH_FILES, INDEXED_FILES)
    assert not bad, (
        "synchronous network I/O reachable from a slash-command coroutine (blocks the event "
        "loop; Discord misses its 3 s interaction ack). Wrap with `await asyncio.to_thread(...)`:\n  "
        + "\n  ".join(bad)
    )


def test_lint_catches_the_2026_09_24_shape(tmp_path):
    lib = tmp_path / "lib.py"
    lib.write_text(
        "import urllib.request\n"
        "def fetch_api_models(u):\n"
        "    return urllib.request.urlopen(u, timeout=5.0)\n"
        "def validate_requested_model(m):\n"
        "    return fetch_api_models(m)\n"
        "class R:\n"
        "    def _set_session_model_override(self, k, o):\n"
        "        validate_requested_model(o)\n"
    )
    slash = tmp_path / "slash.py"
    slash.write_text(
        "async def _handle_model_command(self):\n"
        "    async def _finish_switch():\n"
        "        self._set_session_model_override('k', {})\n"
        "    return await _finish_switch()\n"
    )
    assert offenders([slash], [lib, slash]) == [
        "slash.py:3 _finish_switch() -> _set_session_model_override -> "
        "validate_requested_model -> fetch_api_models -> lib.py:3 urlopen()"
    ]
    slash.write_text(
        "import asyncio\n"
        "async def _handle_model_command(self):\n"
        "    async def _finish_switch():\n"
        "        await asyncio.to_thread(self._set_session_model_override, 'k', {})\n"
        "    def _worker():\n"
        "        self._set_session_model_override('k', {})\n"
        "    await asyncio.to_thread(_worker)\n"
        "    return await _finish_switch()\n"
    )
    assert offenders([slash], [lib, slash]) == []


def test_lint_flags_direct_requests_call_in_coroutine(tmp_path):
    slash = tmp_path / "slash.py"
    slash.write_text("import requests\nasync def h(self):\n    requests.get('http://x')\n")
    assert offenders([slash], [slash]) == ["slash.py:3 h() -> get() [network]"]
