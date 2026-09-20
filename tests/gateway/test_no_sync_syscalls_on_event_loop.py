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

PER-MESSAGE shapes (added 2026-09-20 after the second measured episode).  These
are individually cheap -- microseconds to a couple of milliseconds -- so they do
NOT read as blocking in isolation.  They became a chronic stall because they sit
on the per-inbound-message path and because one of them (``_CONFIG_LOCK``)
serializes against background writers that hold it across file I/O.  py-spy on
the live Apollo gateway (2026-09-20, episode 10:33:34) caught each of these as
the MainThread top frame:

  * ``<mod>.load_config(...)`` / ``load_config_readonly(...)`` /
    ``read_raw_config(...)`` / ``read_raw_config_readonly(...)`` /
    ``save_config(...)`` -- every one of these takes ``_CONFIG_LOCK``.  Measured
    hold times: rebuild median 5.5ms, ``save_config`` p95 33ms / max 55ms; a
    CACHED on-loop read measured max 4032ms while a writer held the lock.
  * ``<path>.resolve(...)`` / ``os.path.realpath(...)`` -- ``_joinrealpath``
    walks every path component.
  * ``atomic_json_write(...)`` / ``atomic_yaml_write(...)`` /
    ``atomic_replace(...)`` -- mkstemp + write + fsync + rename.
  * ``psutil.Process(...)`` / ``<x>.create_time(...)`` -- ``_get_kinfo_proc``
    is a kernel call per invocation.

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
import re
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

# --- per-message shapes (2026-09-20) ---------------------------------------
#
# Bare function names: matched on the LAST component of the dotted callee, so
# both `config.load_config(...)` and a module-level `load_config(...)` imported
# into scope are caught.  These are matched by NAME because that is how they
# are called across the tree; the names are distinctive enough that a false
# positive is unlikely, and a genuine false positive has the documented
# `# noqa: sync-on-loop <reason>` escape.
_CONFIG_LOCK_CALLS = frozenset({
    "load_config",
    "load_config_readonly",
    "read_raw_config",
    "read_raw_config_readonly",
    "save_config",
    "_load_config_impl",
})

_ATOMIC_WRITE_CALLS = frozenset({
    "atomic_json_write",
    "atomic_yaml_write",
    "atomic_write",
    "atomic_replace",
})

# `os.path.realpath(...)` and `<pathlib.Path>.resolve(...)`.
_REALPATH_ATTRS = frozenset({"realpath"})
_RESOLVE_ATTR = "resolve"

# psutil: constructing a Process and reading create_time both hit the kernel.
_PSUTIL_ATTRS = frozenset({"Process", "process_iter", "create_time", "boot_time"})

_OFFLOAD_ATTRS = frozenset({"to_thread", "run_in_executor"})

_NOQA_TOKEN = "# noqa: sync-on-loop"

# Labels for the per-message shapes, which are RATCHETED rather than hard-zero.
_RATCHET_PREFIXES = ("config-lock:", "atomic-write:", "realpath:", "psutil")


def _is_ratcheted(offender: str) -> bool:
    return any(f"-> {p}" in offender for p in _RATCHET_PREFIXES)


def _ratchet_key(offender: str) -> str:
    """Drop the line number: ``path:LINE fn -> label`` -> ``path fn -> label``.

    Line numbers churn on every unrelated edit; the (file, function, shape)
    triple is the stable identity of a site.
    """
    return re.sub(r"^([^:]+):\d+ ", r"\1 ", offender)


# Frozen inventory of PRE-EXISTING per-message sites, captured 2026-09-20 on
# the commit that introduced these shapes into the gate.
#
# This is an incident-to-lint RATCHET, not an endorsement: each of these is a
# real (if smaller) instance of the same class. Fixing them is out of scope for
# the incident that added the gate -- most are on slash-command / startup /
# media-upload paths rather than the per-inbound-message hot path, and several
# would need their own behavioural tests. The ratchet's job is to stop the
# inventory GROWING while allowing it to shrink.
#
# To fix one: move the call off-loop or cache it, then DELETE its line here.
# The test fails if a baseline entry disappears without the baseline being
# updated, so the list cannot silently rot.
PER_MESSAGE_BASELINE = frozenset({
    "gateway/platforms/api_server.py _handle_cron_fire -> config-lock:load_config",
    "gateway/platforms/api_server.py _handle_toolsets -> config-lock:load_config",
    "gateway/platforms/qqbot/adapter.py _load_media -> realpath:.resolve",
    "gateway/platforms/qqbot/adapter.py _upload_local_file -> realpath:.resolve",
    # /moa slash-command branch, not the general message path.
    "gateway/run.py _handle_message -> config-lock:load_config",
    "gateway/run.py _launch_detached_restart_command -> realpath:.resolve",
    "gateway/run.py _send_telegram_topic_setup_image -> realpath:.resolve",
    "gateway/run.py _stop_impl_body -> atomic-write:atomic_json_write",
    "gateway/run.py start -> config-lock:load_config",
    "gateway/run.py start_gateway -> config-lock:read_raw_config",
    "gateway/slash_commands.py _finish_switch -> config-lock:save_config",
    "gateway/slash_commands.py _handle_codex_runtime_command -> config-lock:load_config",
    "gateway/slash_commands.py _handle_update_command -> realpath:.resolve",
    "gateway/slash_commands.py _on_model_selected -> config-lock:save_config",
    "plugins/platforms/feishu/feishu_comment.py handle_drive_comment_event -> config-lock:load_config",
    "plugins/platforms/line/adapter.py _handle_media -> realpath:.resolve",
    "plugins/platforms/line/adapter.py send_image_file -> realpath:.resolve",
    "plugins/platforms/line/adapter.py send_video -> realpath:.resolve",
    "plugins/platforms/line/adapter.py send_voice -> realpath:.resolve",
    "plugins/platforms/wecom/adapter.py _load_outbound_media -> realpath:.resolve",
})


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
    func = call.func

    # --- bare-name shapes (per-message, 2026-09-20) --------------------
    # `load_config()`, `atomic_json_write(...)` imported directly into scope.
    if isinstance(func, ast.Name):
        if func.id in _CONFIG_LOCK_CALLS:
            return f"config-lock:{func.id}"
        if func.id in _ATOMIC_WRITE_CALLS:
            return f"atomic-write:{func.id}"
        return None

    if not isinstance(func, ast.Attribute):
        return None
    dotted = _dotted_base(func)
    if dotted is None:
        # `<expr>.resolve()` / `<expr>.create_time()` -- the base is not a plain
        # Name (e.g. `Path(x).resolve()`), but the ATTRIBUTE alone is decisive
        # for these two shapes.
        if func.attr == _RESOLVE_ATTR:
            return "realpath:.resolve"
        if func.attr in _PSUTIL_ATTRS and func.attr != "Process":
            return f"psutil:.{func.attr}"
        return None
    *base_parts, attr = dotted.split(".")
    if not base_parts:
        return None
    module = base_parts[-1]

    # --- original syscall shapes --------------------------------------
    if module == "subprocess" and attr in _SUBPROCESS_BLOCKING:
        return f"subprocess.{attr}"
    if module == "time" and attr == "sleep":
        return "time.sleep"
    if module == "os" and attr in _OS_BLOCKING:
        return f"os.{attr}"

    # --- per-message shapes (2026-09-20) ------------------------------
    if attr in _CONFIG_LOCK_CALLS:
        return f"config-lock:{attr}"
    if attr in _ATOMIC_WRITE_CALLS:
        return f"atomic-write:{attr}"
    if module == "path" and attr in _REALPATH_ATTRS:
        return f"realpath:os.path.{attr}"
    if attr == _RESOLVE_ATTR:
        return "realpath:.resolve"
    if module == "psutil" and attr in _PSUTIL_ATTRS:
        return f"psutil.{attr}"
    if attr in _PSUTIL_ATTRS and attr != "Process":
        return f"psutil:.{attr}"
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

    Also skips the callee of an ``await`` expression: ``await x.resolve(...)``
    is an awaited coroutine, not ``pathlib.Path.resolve`` (2026-09-20 -- the
    ``.resolve`` shape collides with the very common async ``resolve()``
    helper name, and an awaited call cannot block the loop by definition).
    """
    stack: list[ast.AST] = list(fn.body)
    awaited: set[int] = set()
    # Pre-pass: record every Call that is the direct operand of an `await`.
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))
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
            if id(node) not in awaited:
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

    # The original syscall shapes are a HARD zero -- nothing in the tree may
    # call them on the loop.
    hard = [o for o in all_offenders if not _is_ratcheted(o)]
    assert not hard, (
        "Synchronous blocking call(s) found lexically inside an `async def`. "
        "These freeze the whole event loop for the duration of the syscall.\n"
        "Move each off-loop with `await asyncio.to_thread(fn, ...)` or "
        "`loop.run_in_executor(...)`, or annotate the line with "
        "`# noqa: sync-on-loop <reason>` if it provably cannot block a live loop.\n"
        + "\n".join(f"  {o}" for o in hard)
    )

    # The per-message shapes are RATCHETED: the tree carries a frozen
    # inventory of pre-existing sites (see PER_MESSAGE_BASELINE).  New ones
    # fail; removing one and not updating the baseline also fails, so the
    # inventory can only shrink.
    current = {_ratchet_key(o) for o in all_offenders if _is_ratcheted(o)}
    added = sorted(current - PER_MESSAGE_BASELINE)
    removed = sorted(PER_MESSAGE_BASELINE - current)

    assert not added, (
        "NEW per-message blocking call(s) on the event loop. These are each "
        "individually cheap, which is exactly why they accumulate unnoticed "
        "until the gateway stalls for seconds (measured on Apollo 2026-09-20: "
        "a CACHED config read took 4032ms because a background thread held "
        "_CONFIG_LOCK).\n"
        "Resolve the value once (at load / on a config-change event), cache it "
        "against an mtime or TTL, or move the work off-loop with "
        "`asyncio.to_thread` / `run_in_executor`.\n"
        + "\n".join(f"  {o}" for o in added)
    )

    assert not removed, (
        "Per-message offender(s) in PER_MESSAGE_BASELINE no longer exist -- "
        "good! Delete these entries from the baseline so the ratchet keeps "
        "them gone:\n" + "\n".join(f"  {o}" for o in removed)
    )


def test_the_sites_fixed_by_this_change_are_not_in_the_baseline():
    """The four sites this change fixed must be absent from the tree AND from
    the baseline -- otherwise a regression could reappear and be silently
    absorbed as 'pre-existing'."""
    fixed = {
        "gateway/run.py _persist_active_agents -> atomic-write:atomic_json_write",
        "gateway/run.py _persist_active_agents -> realpath:.resolve",
        "gateway/run.py _persist_active_agents -> psutil.Process",
        "gateway/run.py _handle_message -> config-lock:load_config_readonly",
    }
    assert not (fixed & PER_MESSAGE_BASELINE), (
        "a site this change fixed is listed in the baseline; the ratchet would "
        "let it come back"
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


@pytest.mark.parametrize(
    "src,expected_label",
    [
        # config lock, dotted and bare
        (
            "from hermes_cli import config\n\nasync def h():\n    config.load_config()\n",
            "config-lock:load_config",
        ),
        (
            "from hermes_cli.config import load_config_readonly\n\n"
            "async def h():\n    load_config_readonly()\n",
            "config-lock:load_config_readonly",
        ),
        (
            "from hermes_cli import config\n\nasync def h():\n    config.save_config({})\n",
            "config-lock:save_config",
        ),
        # atomic writes
        (
            "from utils import atomic_json_write\n\n"
            "async def h():\n    atomic_json_write('p', {})\n",
            "atomic-write:atomic_json_write",
        ),
        (
            "import utils\n\nasync def h():\n    utils.atomic_yaml_write('p', {})\n",
            "atomic-write:atomic_yaml_write",
        ),
        # realpath
        (
            "import os\n\nasync def h():\n    os.path.realpath('/x')\n",
            "realpath:os.path.realpath",
        ),
        (
            "from pathlib import Path\n\nasync def h():\n    Path('/x').resolve()\n",
            "realpath:.resolve",
        ),
        # psutil
        (
            "import psutil\n\nasync def h():\n    psutil.Process(1)\n",
            "psutil.Process",
        ),
        (
            "import psutil\n\nasync def h():\n    psutil.Process(1).create_time()\n",
            "psutil:.create_time",
        ),
    ],
)
def test_arm_per_message_shapes_are_red(tmp_path, src, expected_label):
    """RED arm: each per-message shape is detected with its label."""
    root = _write(tmp_path, "m.py", src)
    offenders, _ = find_sync_calls_in_async_defs(root)
    assert any(o.endswith(f"-> {expected_label}") for o in offenders), offenders


def test_arm_per_message_shapes_are_exempt_when_offloaded(tmp_path):
    """GREEN arm: the same calls, offloaded, are not offenders."""
    src = (
        "import asyncio\nimport functools\nfrom hermes_cli import config\n\n"
        "async def h():\n"
        "    await asyncio.to_thread(config.load_config)\n"
        "    await asyncio.to_thread(functools.partial(config.save_config, {}))\n"
        "    loop = asyncio.get_running_loop()\n"
        "    await loop.run_in_executor(None, config.load_config)\n"
    )
    root = _write(tmp_path, "m.py", src)
    offenders, count = find_sync_calls_in_async_defs(root)
    assert offenders == [], offenders
    assert count == 1


def test_arm_awaited_resolve_is_not_a_realpath_offender(tmp_path):
    """``await x.resolve(...)`` is an awaited coroutine, not Path.resolve.

    Without this exemption the gate produced 6 false positives across the real
    tree (slash-confirm resolvers, media resolvers) -- a gate that cries wolf
    gets suppressed wholesale, which is worse than no gate.
    """
    src = (
        "async def h(mod, key):\n"
        "    r = await mod.resolve(key, 'once')\n"
        "    return r\n"
    )
    root = _write(tmp_path, "m.py", src)
    offenders, count = find_sync_calls_in_async_defs(root)
    assert offenders == [], offenders
    assert count == 1


def test_arm_per_message_noqa_requires_a_reason(tmp_path):
    bare = (
        "from hermes_cli import config\n\nasync def h():\n"
        "    config.load_config()  # noqa: sync-on-loop\n"
    )
    offenders, _ = find_sync_calls_in_async_defs(_mk(tmp_path, "pa", bare))
    assert offenders == ["m.py:4 h -> config-lock:load_config"], offenders

    with_reason = (
        "from hermes_cli import config\n\nasync def h():\n"
        "    config.load_config()  # noqa: sync-on-loop startup only, no loop yet\n"
    )
    offenders2, _ = find_sync_calls_in_async_defs(_mk(tmp_path, "pb", with_reason))
    assert offenders2 == [], offenders2


def test_baseline_entries_are_well_formed():
    """A typo in the baseline would silently exempt nothing (or everything)."""
    assert PER_MESSAGE_BASELINE, "baseline is empty -- the ratchet is vacuous"
    for entry in PER_MESSAGE_BASELINE:
        assert " -> " in entry, entry
        path, _, label = entry.partition(" -> ")
        assert any(label.startswith(p) for p in _RATCHET_PREFIXES), entry
        assert not re.search(r":\d+ ", path), (
            f"baseline entry carries a line number ({entry!r}); the ratchet key "
            "is (file, function, shape) so line churn does not break it"
        )
        assert _is_ratcheted(entry), entry


def test_arm_does_not_cover_list_is_documented():
    """The stated boundary is part of the contract -- deleting it turns this red."""
    doc = __doc__ or ""
    assert "DOES-NOT-COVER" in doc
    for token in ("subprocess.Popen", "communicate", "from subprocess import run", "INDIRECTLY"):
        assert token in doc, f"DOES-NOT-COVER section no longer mentions {token!r}"
