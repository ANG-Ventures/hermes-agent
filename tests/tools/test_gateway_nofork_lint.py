"""Ratchet: no launch in the gateway's import tree may bypass the no-fork spawn path (t_aa2e2d4e).

On macOS ``gateway/run.py::start_gateway`` installs ``tools.spawn_nofork.install_process_hook``,
so every ``subprocess.Popen`` / ``run`` / ``check_output`` / ``call`` / ``check_call`` in the
gateway process starts its child with one ``posix_spawn`` (no fork of the multi-GB gateway
under the GIL). Plain ``subprocess.run(...)`` is therefore the RIGHT spelling, and this test does
not flag it. What still forks is a launch the hook cannot express and hands back to CPython's
``fork_exec``, plus the process primitives that never reach ``Popen`` at all:

* ``preexec_fn=`` (non-None), ``close_fds=False``, ``restore_signals=False``,
  ``user=`` / ``group=`` / ``extra_groups=`` / ``umask=`` on a ``subprocess`` call, the same
  keys in a dict literal (``popen_kwargs = {...}`` splatted later),
* ``os.fork`` / ``os.forkpty`` / ``pty.fork`` / ``pty.spawn`` / ``os.system`` / ``os.popen`` /
  ``os.spawn*``,
* imports of ``ptyprocess`` / ``pexpect`` (``pty.fork`` inside) and ``multiprocessing`` /
  ``concurrent.futures.ProcessPoolExecutor`` (fork start method on POSIX < 3.14).

A site is (file, enclosing function, kind). A NEW site fails until it is moved onto a
hook-eligible shape or added to ``ALLOWED`` with the reason it must fork; an ``ALLOWED`` entry
that no longer matches fails too, so the list cannot rot. Reading source is normally banned
(root AGENTS.md); it is the only tool here, as in ``tests/test_managed_runtime_resolution.py``:
the property is "no call site in the tree is spelt this way", and there is no runtime seam that
can observe a call site nobody has executed yet. The hook's own behaviour is covered by real
spawns in ``tests/tools/test_spawn_nofork_hook.py``.

Scope is every package the gateway process imports from (``SCANNED``), minus tests. Code that
only ever runs in another process (the CLI's own commands, the dashboard server, a child script)
is still scanned: the import graph is too dynamic to prove a module never loads in the gateway,
so such a site is allowlisted with the process it actually runs in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("agent", "cron", "gateway", "hermes_cli", "plugins", "tools", "tui_gateway",
                "acp_adapter", "providers", "hermes_platform")
SKIP_PARTS = {"tests", "node_modules", "__pycache__"}

_FORK_KWARGS_ANY_CALL = {"preexec_fn"}
_FORK_KWARGS_SUBPROCESS = {"user", "group", "extra_groups", "umask"}
_FALSE_KWARGS = {"close_fds", "restore_signals"}
_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "call", "check_call"}
_PRIMITIVES = {"os.fork", "os.forkpty", "pty.fork", "pty.spawn", "os.system", "os.popen",
               "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv", "os.spawnve",
               "os.spawnvp", "os.spawnvpe"}
_FORKING_MODULES = {"ptyprocess", "pexpect", "multiprocessing"}

# (path, enclosing qualname, kind) -> why this launch must keep forking.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("agent/shell_hook_dispatcher.py", "_start", "call:os.fork"):
        "runs inside the warm shell-hook dispatcher CHILD process (agent/shell_hooks_batch.py starts it "
        "with plain Popen, which the hook covers); forking that small interpreter is the design "
        "(one compiled hook per fork), never the gateway.",
    ("tools/process_registry.py", "ProcessRegistry._spawn_local_pty", "import:ptyprocess"):
        "terminal(pty=True): ptyprocess.PtyProcess.spawn forks to make the pty the child's "
        "controlling terminal (setsid + TIOCSCTTY between fork and exec). Follow-up card: posix_spawn "
        "with SETSID + addopen of the slave (macOS acquires the ctty on open).",
    ("hermes_cli/pty_bridge.py", "<module>", "import:ptyprocess"):
        "dashboard PTY bridge: runs in `hermes dashboard` (web_server_chat), not in the gateway; "
        "needs a controlling terminal like the pty terminal tool.",
    ("batch_runner.py", "<module>", "import:multiprocessing"):
        "batch_runner CLI only (`python batch_runner.py`); never imported by the gateway.",
}


def _files() -> list[Path]:
    out = sorted(REPO.glob("*.py"))
    for d in SCANNED_DIRS:
        out += sorted(p for p in (REPO / d).rglob("*.py") if not SKIP_PARTS.intersection(p.parts))
    return out


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _forking_value(node: ast.AST) -> bool:
    return not (isinstance(node, ast.Constant) and node.value is None)


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel, self.stack, self.sites = rel, [], []

    def _add(self, kind: str, node: ast.AST) -> None:
        self.sites.append(((self.rel, ".".join(self.stack) or "<module>", kind), node.lineno))

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split(".")[0] in _FORKING_MODULES:
                self._add(f"import:{alias.name.split('.')[0]}", node)

    def visit_ImportFrom(self, node):
        mod = (node.module or "").split(".")[0]
        if node.level == 0 and mod in _FORKING_MODULES:
            self._add(f"import:{mod}", node)
        if node.level == 0 and node.module == "concurrent.futures":
            if any(a.name == "ProcessPoolExecutor" for a in node.names):
                self._add("import:ProcessPoolExecutor", node)

    def visit_Attribute(self, node):
        if _dotted(node) == "concurrent.futures.ProcessPoolExecutor":
            self._add("import:ProcessPoolExecutor", node)
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _dotted(node.func) or ""
        if name in _PRIMITIVES:
            self._add(f"call:{name}", node)
        is_subprocess = name.startswith("subprocess.") and name.split(".", 1)[1] in _SUBPROCESS_CALLS
        for kw in node.keywords:
            if kw.arg in _FORK_KWARGS_ANY_CALL and _forking_value(kw.value):
                self._add(f"kwarg:{kw.arg}", kw.value)
            elif is_subprocess and kw.arg in _FORK_KWARGS_SUBPROCESS and _forking_value(kw.value):
                self._add(f"kwarg:{kw.arg}", kw.value)
            elif kw.arg in _FALSE_KWARGS and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                self._add(f"kwarg:{kw.arg}=False", kw.value)
        self.generic_visit(node)

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in _FORK_KWARGS_ANY_CALL and _forking_value(value):
                self._add(f"kwarg:{key.value}", key)
        self.generic_visit(node)


def scan() -> dict[tuple[str, str, str], list[int]]:
    sites: dict[tuple[str, str, str], list[int]] = {}
    for path in _files():
        rel = path.relative_to(REPO).as_posix()
        scanner = _Scan(rel)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        for key, line in scanner.sites:
            sites.setdefault(key, []).append(line)
    return sites


@pytest.fixture(scope="module")
def sites():
    return scan()


def test_no_new_forking_launch_in_gateway_import_tree(sites):
    new = {k: v for k, v in sites.items() if k not in ALLOWED}
    assert not new, (
        "launch sites that still fork the gateway on macOS (the no-fork hook cannot express them).\n"
        "Make the launch hook-eligible (absolute or PATH-resolvable argv, no preexec_fn, default "
        "close_fds/restore_signals, no user/group/umask) or add the key to ALLOWED with the reason:\n"
        + "\n".join(f"  {k!r}: lines {v}" for k, v in sorted(new.items())))


def test_allowlist_has_no_stale_entries(sites):
    stale = sorted(k for k in ALLOWED if k not in sites)
    assert not stale, f"ALLOWED entries that no longer match any site (delete them): {stale}"


def test_allowlist_reasons_are_written():
    assert all(len(reason.strip()) > 20 for reason in ALLOWED.values())


@pytest.mark.parametrize("source, kind", [
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], preexec_fn=g)\n", "kwarg:preexec_fn"),
    ("import subprocess\ndef f():\n    subprocess.run(['x'], close_fds=False)\n", "kwarg:close_fds=False"),
    ("import subprocess\ndef f():\n    subprocess.check_output(['x'], user='nobody')\n", "kwarg:user"),
    ("def f():\n    kw = {'preexec_fn': g}\n", "kwarg:preexec_fn"),
    ("import os\ndef f():\n    os.fork()\n", "call:os.fork"),
    ("import os\ndef f():\n    os.system('x')\n", "call:os.system"),
    ("def f():\n    import pexpect\n", "import:pexpect"),
    ("from concurrent.futures import ProcessPoolExecutor\n", "import:ProcessPoolExecutor"),
])
def test_scanner_flags_each_forking_shape(source, kind):
    scanner = _Scan("x.py")
    scanner.visit(ast.parse(source))
    assert [k[2] for k, _ in scanner.sites] == [kind]


@pytest.mark.parametrize("source", [
    "import subprocess\nsubprocess.run(['git', 'status'], capture_output=True, text=True, timeout=5, check=True)\n",
    "import subprocess\nsubprocess.Popen(['x'], preexec_fn=None, start_new_session=True, cwd='/tmp', env={})\n",
    "import subprocess\nsubprocess.Popen(['x'], pass_fds=(5,), process_group=0)\n",
    "import subprocess\nsubprocess.run('echo hi', shell=True)\n",
])
def test_scanner_passes_hook_eligible_shapes(source):
    scanner = _Scan("x.py")
    scanner.visit(ast.parse(source))
    assert scanner.sites == []
