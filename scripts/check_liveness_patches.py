#!/usr/bin/env python3
"""Refuse a test that replaces a LIVENESS primitive with a callable that cannot raise.

The incident (t_69572750, 2026-10-12): ``tests/hermes_cli/test_kanban_pr_owner.py`` at
tree a0fe4e9410 did ``monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(...))``.
The file's own ``_dead_pid()`` is ``while True: try: os.kill(pid, 0) except ProcessLookupError:
return pid``; under a recorder that never raises it never returns, the recorder list grew to
34.6 GiB and ACE-AI hard-reset. Every poll-until-exception loop in the tree has the same
shape, so a non-raising stand-in for one of these primitives is a latent hang, not a stub:

* ``os.kill``            - ``kill(pid, 0)`` raising ``ProcessLookupError`` is how the tree
                           answers "is that pid dead"
* ``os.waitpid``         - ``ChildProcessError`` ends reap loops
* ``socket.connect``     - ``ConnectionRefusedError`` / ``TimeoutError`` end connect-retry loops
* ``psutil.pid_exists``  - return-based: a replacement pinned to ``True`` makes every
                           wait-for-exit loop unbounded, so it is flagged on a constant True

``time.sleep`` is deliberately NOT in the list: a no-op sleep makes a paced loop hot, it does
not make a bounded loop unbounded, and the per-test wall clock in ``tests/_fixtures/bounds.py``
now ends a hot loop by name. Flagging the ~38 ``time.sleep`` no-ops in the tree would be a
guard people disable.

A replacement is "honest" when it can end the loop: a ``def`` containing a ``raise``, a
``def``/``lambda`` that forwards to the saved original (``real = os.kill`` ... ``real(pid, 0)``),
a ``side_effect`` that is an exception class/instance or a list (``StopIteration`` ends it), or a
generator ``.throw``. A bare ``patch("os.kill")`` (MagicMock), a ``return_value=``, a recorder
``lambda`` or a ``def`` with neither a ``raise`` nor a forward is refused.

Suppress one line with ``# liveness-patch: ok — <why the loop under test is bounded>``.

``_BASELINE`` maps files that already carried such recorders when this lint landed to their
hit count: a burn-down list, not a policy. A baseline file GAINING hits fails; a new file
with any hit fails; an entry that overstates a file is an advisory (``--strict-baseline``
makes it a failure). Refresh with ``--print-baseline``; ``--all`` ignores it.

Usage: ``python scripts/check_liveness_patches.py [--all|--print-baseline|--strict-baseline]
[paths...]`` (default: tests/). Exit 1 on any finding, 0 when clean.
"""
from __future__ import annotations

import ast
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPRESS = "# liveness-patch: ok"

# target dotted name -> (module attribute spellings that reach it, kind)
#   "raise"  the primitive bounds loops by RAISING; a replacement must be able to raise
#   "true"   the primitive bounds loops by RETURNING False; a replacement pinned True is refused
PRIMITIVES: dict[str, str] = {
    "os.kill": "raise",
    "os.waitpid": "raise",
    "socket.connect": "raise",
    "socket.socket.connect": "raise",
    "psutil.pid_exists": "true",
}
_OWNERS = {"os": {"kill", "waitpid"}, "socket": {"connect"}, "psutil": {"pid_exists"}}
_PATCHERS = frozenset({"setattr", "patch", "object"})  # monkeypatch.setattr / mock.patch / patch.object


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _target_of(call: ast.Call) -> str | None:
    """The primitive this call replaces, or None."""
    fname = call.func.attr if isinstance(call.func, ast.Attribute) else (
        call.func.id if isinstance(call.func, ast.Name) else None)
    if fname not in _PATCHERS or not call.args:
        return None
    first = call.args[0]
    # setattr("os.kill", x) / patch("os.kill", ...)
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        name = first.value
        tail = name.rsplit(".", 1)
        if name in PRIMITIVES:
            return name
        if len(tail) == 2 and tail[1] in {a for s in _OWNERS.values() for a in s}:
            owner = tail[0].rsplit(".", 1)[-1]
            if tail[1] in _OWNERS.get(owner, set()):
                return f"{owner}.{tail[1]}"
        return None
    # setattr(os, "kill", x) / patch.object(socket.socket, "connect", x)
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        owner = _dotted(first)
        attr = call.args[1].value
        if owner is None:
            return None
        root = owner.rsplit(".", 1)[-1]
        if attr in _OWNERS.get(root, set()):
            return f"{root}.{attr}"
        if owner == "socket.socket" and attr == "connect":
            return "socket.connect"
    return None


def _replacement(call: ast.Call, fname: str) -> ast.expr | None:
    """The expression installed as the replacement (None = bare MagicMock)."""
    kw = {k.arg: k.value for k in call.keywords}
    if fname == "setattr":
        pos = 2 if (len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str) and not isinstance(call.args[0], ast.Constant)) else 1
        if len(call.args) > pos:
            return call.args[pos]
        return kw.get("value")
    # mock.patch / patch.object
    if "side_effect" in kw:
        return kw["side_effect"]
    if "new" in kw:
        return kw["new"]
    pos = 2 if fname == "object" else 1
    if len(call.args) > pos:
        return call.args[pos]
    if "return_value" in kw:
        return kw["return_value"]  # a value: returns it, never raises
    return None  # bare patch -> MagicMock: returns, never raises


class _File:
    def __init__(self, tree: ast.AST) -> None:
        self.defs: dict[str, ast.AST] = {}
        self.originals: set[str] = set()  # names bound to a real primitive: real = os.kill
        self.raisers: set[str] = set()  # names bound to Mock(side_effect=<exception>)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(node.name, node)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                src = _dotted(node.value)
                if src in PRIMITIVES or (src and src.rsplit(".", 1)[-1] in {"kill", "waitpid", "connect", "pid_exists"}):
                    self.originals.add(node.targets[0].id)
                elif isinstance(node.value, ast.Call) and any(k.arg == "side_effect" for k in node.value.keywords):
                    self.raisers.add(node.targets[0].id)

    def _forwards(self, node: ast.AST) -> bool:
        """The body can END a poll loop: it forwards to the real primitive or raises
        through a helper (``pytest.fail``/``skip``/``exit``, a generator ``.throw``)."""
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "throw":
                return True  # (_ for _ in ()).throw(Exc) — the lambda-raise idiom
            name = _dotted(call.func)
            if name is None:
                continue
            if name in self.originals or name in PRIMITIVES or name in self.raisers:
                return True
            if name.rsplit(".", 1)[-1] in {"throw", "fail", "skip", "exit", "_exit"}:
                return True
            helper = self.defs.get(name)
            if helper is not None and helper is not node and self._raises(helper):
                return True
            if name.rsplit(".", 1)[-1] in {"kill", "waitpid", "connect", "pid_exists"} and "." in name:
                return True
        return False

    def _raises(self, fn: ast.AST) -> bool:
        return any(isinstance(n, ast.Raise) for n in ast.walk(fn)) or self._forwards(fn)

    def can_end_loop(self, repl: ast.expr | None, kind: str) -> bool:
        if repl is None:
            return False
        if kind == "true":
            if isinstance(repl, ast.Lambda):
                return not (isinstance(repl.body, ast.Constant) and repl.body.value is True)
            if isinstance(repl, ast.Constant):
                return repl.value is not True
            return True
        if isinstance(repl, ast.Constant):
            return False  # return_value=None / a literal: returns, never raises
        if isinstance(repl, ast.Lambda):
            return self._forwards(repl.body)
        if isinstance(repl, (ast.List, ast.Tuple)):
            return True  # a side_effect iterable raises StopIteration when exhausted
        if isinstance(repl, ast.Name):
            fn = self.defs.get(repl.id)
            if fn is not None:
                return self._raises(fn)
            if repl.id in self.originals or repl.id in self.raisers:
                return True
            return repl.id[:1].isupper()  # an exception class, e.g. side_effect=ProcessLookupError
        if isinstance(repl, ast.Call):
            name = _dotted(repl.func) or ""
            if name.rsplit(".", 1)[-1] in {"MagicMock", "Mock", "AsyncMock"}:
                return "side_effect" in {k.arg for k in repl.keywords}
            if name.rsplit(".", 1)[-1][:1].isupper():
                return True  # an exception instance, or a factory we cannot see into
            fn = self.defs.get(name)
            if fn is not None:  # factory: _recording_kill(signals) -> inner def
                return self._raises(fn)
            return True
        if isinstance(repl, ast.Attribute):
            fn = self.defs.get(repl.attr)
            if fn is not None:
                return self._raises(fn)
            return True
        return True


def check_source(src: str, filename: str = "<src>") -> list[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src, filename=filename)
    info = _File(tree)
    lines = src.splitlines()
    problems: list[str] = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        target = _target_of(call)
        if target is None:
            continue
        line = lines[call.lineno - 1] if call.lineno <= len(lines) else ""
        if SUPPRESS in line:
            continue
        fname = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id  # type: ignore[union-attr]
        repl = _replacement(call, fname)
        kind = PRIMITIVES[target]
        if info.can_end_loop(repl, kind):
            continue
        how = ("is pinned to True, so a wait-for-exit loop never ends" if kind == "true"
               else "can never raise, so a poll-until-exception loop never ends")
        problems.append(
            f"{filename}:{call.lineno}: replaces `{target}` with a callable that {how} "
            f"(t_69572750: 34.6 GiB, host reset). Forward signal-0 probes to the real primitive, "
            f"raise ProcessLookupError for dead pids, or mark the line `{SUPPRESS} — <why bounded>`.")
    return problems


# Files carrying non-raising recorders when the lint landed (2026-10-12, t_6274f5cb), with
# their hit counts. Each is a single-shot ``terminate``/``kill`` assertion today, not a poll
# loop; the per-test watchdog in tests/_fixtures/bounds.py bounds them regardless. Burn down:
# forward ``sig == 0`` to the real primitive (``_recording_kill`` in test_kanban_pr_owner.py
# is the shape) and drop the entry.
_BASELINE: dict[str, int] = {
    "tests/gateway/test_runner_startup_failures.py": 1,
    "tests/gateway/test_whatsapp_connect.py": 2,
    "tests/hermes_cli/test_gateway.py": 8,
    "tests/hermes_cli/test_local_runtime_legacy_state.py": 1,
    "tests/hermes_cli/test_local_runtime_recovery.py": 1,
    "tests/hermes_cli/test_orphan_desktop_serve_reap.py": 2,
    "tests/hermes_cli/test_update_cron_deadlock_guard.py": 2,
    "tests/hermes_cli/test_update_fleet_home_scope.py": 1,
    "tests/hermes_cli/test_update_serve_generation_recovery.py": 1,
    "tests/hermes_cli/test_update_stale_dashboard.py": 2,
    "tests/plugins/platforms/photon/test_sidecar_lifecycle.py": 1,
    "tests/plugins/test_google_meet_plugin.py": 1,
    "tests/tools/test_file_sync_sigint.py": 1,
    "tests/tools/test_mcp_stability.py": 4,
    "tests/tools/test_mcp_stdio_children_dead.py": 2,
    "tests/tools/test_process_registry.py": 1,
    "tests/tools/test_windows_native_support.py": 4,
}


def apply_baseline(hits: dict[str, list[str]], baseline: dict[str, int], strict: bool) -> tuple[list[str], list[str]]:
    """Split per-file findings into (failures, advisories) against the burn-down list."""
    failures: list[str] = []
    advisories: list[str] = []
    for rel, probs in sorted(hits.items()):
        allowed = baseline.get(rel, 0)
        if len(probs) > allowed:
            failures.extend(probs)
            if allowed:
                failures.append(f"{rel}: {len(probs)} hit(s), baseline allows {allowed}: a new recorder was added")
        elif len(probs) < allowed:
            advisories.append(f"{rel}: baseline says {allowed}, now {len(probs)}: shrink the _BASELINE entry")
    for rel, allowed in sorted(baseline.items()):
        if rel not in hits and allowed:
            advisories.append(f"{rel}: baseline says {allowed}, now 0: drop the _BASELINE entry")
    if strict:
        failures.extend(advisories)
        advisories = []
    return failures, advisories


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    targets = [Path(p) for p in argv if not p.startswith("--")] or [ROOT / "tests"]
    files: list[Path] = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])
    hits: dict[str, list[str]] = {}
    unparsable: list[str] = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(f.resolve().relative_to(ROOT))
        except ValueError:
            rel = str(f)
        try:
            probs = check_source(src, rel)
        except SyntaxError as exc:
            unparsable.append(f"{rel}: unparsable: {exc}")
            continue
        if probs:
            hits[rel] = probs
    if "--print-baseline" in flags:
        print("_BASELINE: dict[str, int] = {")
        for rel, probs in sorted(hits.items()):
            print(f'    "{rel}": {len(probs)},')
        print("}")
        return 0
    baseline = {} if "--all" in flags else _BASELINE
    if targets != [ROOT / "tests"]:  # a partial scan cannot judge files it did not read
        baseline = {k: v for k, v in baseline.items() if k in hits}
    failures, advisories = apply_baseline(hits, baseline, "--strict-baseline" in flags)
    failures = unparsable + failures
    for p in failures:
        print(p)
    for a in advisories:
        print(f"advisory: {a}")
    if failures:
        print(f"\n{len(failures)} liveness-primitive patch(es) that cannot end a poll loop "
              f"(t_69572750). Scanned {len(files)} file(s).", file=sys.stderr)
        return 1
    print(f"liveness-patch lint: clean ({len(files)} file(s), {sum(map(len, hits.values()))} baseline hit(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
