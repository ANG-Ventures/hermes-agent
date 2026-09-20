# fork-only: upstream AGENTS.md forbids source-reading tests; do not port
"""AST contract: no synchronous blocking syscall sits lexically in an ``async def``.

Symptom this locks against: the gateway event loop stalls for tens of seconds at a
time (platform clients log "heartbeat blocked for more than N seconds" with a
traceback pointing into a coroutine).  Every instance measured so far was the same
class of defect -- a synchronous, blocking call written directly in the body of an
``async def``, so the whole loop (every other platform adapter, every timer, every
inbound message) is frozen for the duration of that syscall.

The property is structural, so the gate is structural: walk every ``*.py`` under the
scanned roots, and for each ``ast.AsyncFunctionDef`` inspect the calls lexically in
its body -- WITHOUT descending into a nested ``def`` or ``lambda``, because those run
wherever they are later called, not necessarily on the loop.

COVERED call shapes (the ones that block the calling thread):

  * ``subprocess.run(...)``
  * ``subprocess.check_output(...)``
  * ``subprocess.check_call(...)``
  * ``subprocess.call(...)``
  * ``time.sleep(...)``
  * ``os.system(...)``
  * ``os.popen(...)``

The module qualifier is matched on the LAST component of the callee's dotted base,
so both ``subprocess.run(...)`` and ``foo.subprocess.run(...)`` are covered.

EXEMPTIONS (a covered call is NOT an offender when):

  * the ``ast.Call`` is lexically nested inside the arguments of
    ``asyncio.to_thread(...)``, ``<loop>.run_in_executor(...)``, or
    ``asyncio.get_running_loop().run_in_executor(...)``.  This covers both the
    "pass the callable, args follow" form (``asyncio.to_thread(subprocess.run, cmd)``
    -- the bare ``subprocess.run`` name is not a Call at all, so it never trips the
    gate) and the ``functools.partial``/lambda-wrapped form.
  * the offending line carries a ``# noqa: sync-on-loop`` comment WITH a reason
    (trailing text after the code).  A bare ``# noqa: sync-on-loop`` is NOT accepted
    -- an unexplained suppression is a gap, not an exemption.

DOES-NOT-COVER (stated deliberately; these are OUT OF SCOPE, not oversights):

  * ``subprocess.Popen(...)`` -- ``Popen`` only fork/execs and returns immediately;
    it does not block the loop.  The blocking half is the wait, which is the next
    bullet.
  * ``<proc>.communicate()`` / ``<proc>.wait()`` on a ``Popen`` result -- resolving
    whether the receiver is a ``Popen`` requires type inference this gate does not do.
  * aliased imports: ``from subprocess import run`` then a bare ``run(...)``;
    ``import time as t`` then ``t.sleep(...)``; any rebinding
    (``_run = subprocess.run``).  Only the dotted attribute form is matched.
  * attribute-chained *callers* whose base is an expression rather than a name,
    e.g. ``self._mod.subprocess.run(...)`` resolved through an object attribute, or
    ``get_mod().run(...)``.
  * blocking calls that are not in this shape list at all: ``socket.recv``,
    ``requests.get``, ``open().read()`` of a large file, ``os.waitpid``, heavy
    imports, ``hashlib`` over a big buffer, sqlite writes.
  * synchronous calls reached INDIRECTLY -- an ``async def`` calling a plain
    ``def`` helper that blocks.  Only lexical containment is checked.
  * nested ``def``/``lambda`` bodies inside an ``async def`` (by design: they run
    wherever they are invoked).

That list is the honest boundary of the lock.  It catches the accidental regression
(someone writes ``subprocess.run`` in a coroutine); it is not sound against an
adversary, and widening it to chase alias/dataflow escapes is explicitly not wanted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Roots scanned, relative to the repository root.
SCANNED_ROOTS = ("gateway", "plugins/platforms")

# Vacuity floor.  gateway/run.py alone carries hundreds of coroutines; if the walk
# enumerates fewer than this, the discovery step is broken and the gate is
# vacuously green.
MIN_ASYNC_DEFS = 200

_SUBPROCESS_BLOCKING = frozenset({"run", "check_output", "check_call", "call"})
_OS_BLOCKING = frozenset({"system", "popen"})

_OFFLOAD_ATTRS = frozenset({"to_thread", "run_in_executor"})

_NOQA_TOKEN = "# noqa: sync-on-loop"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dotted_base(func: ast.AST) -> str | None:
    """Return the dotted name of an attribute chain rooted at a plain Name.

    ``subprocess.run`` -> ``"subprocess.run"``;  ``a.b.c()`` -> ``"a.b.c"``.
    Returns None when the chain bottoms out on anything but an ``ast.Name``
    (a call, a subscript, ``self``-rooted expressions resolve to a Name too).
    """
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _offender_label(call: ast.Call) -> str | None:
    """Return ``"module.attr"`` when this Call is a covered blocking shape."""
    if not isinstance(call.func, ast.Attribute):
        return None
    dotted = _dotted_base(call.func)
    if dotted is None:
        return None
    *base_parts, attr = dotted.split(".")
    if not base_parts:
        return None
    module = base_parts[-1]
    if module == "subprocess" and attr in _SUBPROCESS_BLOCKING:
        return f"subprocess.{attr}"
    if module == "time" and attr == "sleep":
        return "time.sleep"
    if module == "os" and attr in _OS_BLOCKING:
        return f"os.{attr}"
    return None


def _is_offload_call(call: ast.Call) -> bool:
    """True for ``asyncio.to_thread(...)`` / ``<anything>.run_in_executor(...)``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _OFFLOAD_ATTRS:
        return False
    if func.attr == "run_in_executor":
        # Any receiver: loop.run_in_executor, asyncio.get_running_loop().run_in_executor,
        # self._loop.run_in_executor.
        return True
    # to_thread must be on asyncio (last component of the dotted base).
    dotted = _dotted_base(func)
    if dotted is None:
        return False
    parts = dotted.split(".")
    return len(parts) >= 2 and parts[-2] == "asyncio"


def _noqa_exempt(line: str) -> bool:
    """``# noqa: sync-on-loop <reason>`` -- a bare marker with no reason does NOT exempt."""
    idx = line.find(_NOQA_TOKEN)
    if idx < 0:
        return False
    return bool(line[idx + len(_NOQA_TOKEN):].strip())


def _iter_loop_calls(fn: ast.AsyncFunctionDef):
    """Yield every ast.Call lexically in ``fn``'s body, not descending into
    nested ``def``/``lambda`` bodies, and skipping the argument subtrees of
    offload calls (``asyncio.to_thread`` / ``run_in_executor``).
    """
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            # A nested coroutine is itself enumerated by the outer walk; do not
            # attribute its body to the enclosing function.
            continue
        if isinstance(node, ast.Call):
            if _is_offload_call(node):
                # The callee expression may still be interesting, but every
                # argument subtree is running off-loop by construction.
                stack.append(node.func)
                continue
            yield node
        stack.extend(ast.iter_child_nodes(node))


def find_sync_calls_in_async_defs(root: Path) -> tuple[list[str], int]:
    """Scan ``root`` recursively.

    Returns ``(offenders, async_def_count)`` where each offender is formatted
    ``"<path>:<line> <function> -> <module.attr>"`` with ``<path>`` relative to
    ``root``.  Pure function: no repo assumptions, so mutation arms can point it
    at a temporary tree.
    """
    offenders: list[str] = []
    async_defs = 0
    for py in sorted(root.rglob("*.py")):
        posix = py.as_posix()
        if "/tests/" in posix or posix.endswith("/tests"):
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            # A module that does not parse under this interpreter cannot be
            # ruled out; surface it rather than silently shrinking the scan.
            offenders.append(f"{py.relative_to(root)}:0 <module> -> UNPARSEABLE")
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            async_defs += 1
            for call in _iter_loop_calls(node):
                label = _offender_label(call)
                if label is None:
                    continue
                line = lines[call.lineno - 1] if 0 < call.lineno <= len(lines) else ""
                if _noqa_exempt(line):
                    continue
                offenders.append(
                    f"{py.relative_to(root).as_posix()}:{call.lineno} {node.name} -> {label}"
                )
    return sorted(offenders), async_defs


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_no_sync_syscalls_lexically_inside_async_defs():
    repo = _repo_root()
    all_offenders: list[str] = []
    total_async_defs = 0
    for rel in SCANNED_ROOTS:
        root = repo / rel
        assert root.is_dir(), f"scanned root missing: {rel} (the scan scope is wrong)"
        offenders, count = find_sync_calls_in_async_defs(root)
        total_async_defs += count
        all_offenders.extend(f"{rel}/{o}" for o in offenders)

    # Vacuity floor: a broken walk must fail loudly, not report a clean tree.
    assert total_async_defs >= MIN_ASYNC_DEFS, (
        f"enumerated only {total_async_defs} async defs across {SCANNED_ROOTS}; "
        f"expected >= {MIN_ASYNC_DEFS}. The discovery step is broken and this "
        "gate is now vacuously green."
    )

    assert not all_offenders, (
        "Synchronous blocking call(s) found lexically inside an `async def`. "
        "These freeze the whole event loop for the duration of the syscall.\n"
        "Move each off-loop with `await asyncio.to_thread(fn, ...)` or "
        "`loop.run_in_executor(...)`, or annotate the line with "
        "`# noqa: sync-on-loop <reason>` if it provably cannot block a live loop.\n"
        + "\n".join(f"  {o}" for o in all_offenders)
    )


def test_scan_roots_are_populated():
    """Positively assert scope, by name and by count -- 'clean' must be
    distinguishable from 'clean because nothing was scanned'."""
    repo = _repo_root()
    per_root = {}
    for rel in SCANNED_ROOTS:
        _, count = find_sync_calls_in_async_defs(repo / rel)
        per_root[rel] = count
    assert per_root["gateway"] >= 150, per_root
    assert per_root["plugins/platforms"] >= 20, per_root


# ---------------------------------------------------------------------------
# Mutation arms -- the gate must bite, and must not bite the exempt forms.
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return tmp_path


CONTROL_CLEAN = '''
import asyncio
import subprocess

async def connect():
    await asyncio.sleep(0)
    return True
'''


def test_arm_control_clean_tree_is_green(tmp_path):
    root = _write(tmp_path, "m.py", CONTROL_CLEAN)
    offenders, count = find_sync_calls_in_async_defs(root)
    assert offenders == []
    assert count == 1, "harness itself is broken: the control coroutine was not enumerated"


@pytest.mark.parametrize(
    "snippet,expected_label",
    [
        ("    subprocess.run(['true'])", "subprocess.run"),
        ("    subprocess.check_output(['scutil', '--proxy'])", "subprocess.check_output"),
        ("    subprocess.check_call(['true'])", "subprocess.check_call"),
        ("    subprocess.call(['true'])", "subprocess.call"),
        ("    time.sleep(5)", "time.sleep"),
        ("    os.system('true')", "os.system"),
        ("    os.popen('true')", "os.popen"),
    ],
)
def test_arm_injected_offender_is_red(tmp_path, snippet, expected_label):
    """RED arm: each covered shape injected into an async def must be reported
    with file:line function -> module.attr."""
    src = "import os\nimport subprocess\nimport time\n\nasync def connect():\n" + snippet + "\n"
    root = _write(tmp_path, "m.py", src)
    offenders, _ = find_sync_calls_in_async_defs(root)
    assert offenders == [f"m.py:6 connect -> {expected_label}"], offenders


def test_arm_offender_wrapped_in_to_thread_is_green(tmp_path):
    """GREEN arm: the same call, offloaded, is exempt in both spellings."""
    src = (
        "import asyncio\nimport functools\nimport subprocess\n\n"
        "async def connect():\n"
        "    await asyncio.to_thread(subprocess.run, ['true'])\n"
        "    await asyncio.to_thread(functools.partial(subprocess.run, ['true']))\n"
        "    loop = asyncio.get_running_loop()\n"
        "    await loop.run_in_executor(None, functools.partial(subprocess.run, ['true']))\n"
        "    await asyncio.get_running_loop().run_in_executor(\n"
        "        None, functools.partial(subprocess.check_output, ['true'])\n"
        "    )\n"
    )
    root = _write(tmp_path, "m.py", src)
    offenders, count = find_sync_calls_in_async_defs(root)
    assert offenders == [], offenders
    assert count == 1


def test_arm_nested_def_and_lambda_are_not_attributed_to_the_coroutine(tmp_path):
    src = (
        "import subprocess\n\n"
        "async def connect():\n"
        "    def _blocking():\n"
        "        subprocess.run(['true'])\n"
        "    f = lambda: subprocess.run(['true'])\n"
        "    return _blocking, f\n"
    )
    root = _write(tmp_path, "m.py", src)
    offenders, _ = find_sync_calls_in_async_defs(root)
    assert offenders == [], offenders


def test_arm_plain_def_is_not_scanned(tmp_path):
    src = "import subprocess\n\ndef helper():\n    subprocess.run(['true'])\n"
    root = _write(tmp_path, "m.py", src)
    offenders, count = find_sync_calls_in_async_defs(root)
    assert offenders == []
    assert count == 0


def test_arm_noqa_requires_a_reason(tmp_path):
    """A bare marker must NOT exempt; a marker with a reason must."""
    bare = (
        "import subprocess\n\nasync def connect():\n"
        "    subprocess.run(['true'])  # noqa: sync-on-loop\n"
    )
    offenders, _ = find_sync_calls_in_async_defs(_mk(tmp_path, "a", bare))
    assert offenders == ["m.py:4 connect -> subprocess.run"], offenders

    with_reason = (
        "import subprocess\n\nasync def connect():\n"
        "    subprocess.run(['true'])  # noqa: sync-on-loop setup-only, no loop running\n"
    )
    offenders2, _ = find_sync_calls_in_async_defs(_mk(tmp_path, "b", with_reason))
    assert offenders2 == [], offenders2


def _mk(tmp_path: Path, sub: str, body: str) -> Path:
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.py").write_text(body, encoding="utf-8")
    return d


def test_arm_tests_dirs_are_skipped(tmp_path):
    d = tmp_path / "pkg" / "tests"
    d.mkdir(parents=True)
    (d / "m.py").write_text(
        "import subprocess\n\nasync def connect():\n    subprocess.run(['true'])\n",
        encoding="utf-8",
    )
    offenders, count = find_sync_calls_in_async_defs(tmp_path)
    assert offenders == []
    assert count == 0


def test_arm_vacuity_floor_would_fire_on_an_empty_tree(tmp_path):
    """The floor is the thing that turns 'found nothing' into a failure."""
    _, count = find_sync_calls_in_async_defs(tmp_path)
    assert count == 0
    assert count < MIN_ASYNC_DEFS


def test_arm_does_not_cover_list_is_documented():
    """The stated boundary is part of the contract -- deleting it turns this red."""
    doc = __doc__ or ""
    assert "DOES-NOT-COVER" in doc
    for token in ("subprocess.Popen", "communicate", "from subprocess import run", "INDIRECTLY"):
        assert token in doc, f"DOES-NOT-COVER section no longer mentions {token!r}"
