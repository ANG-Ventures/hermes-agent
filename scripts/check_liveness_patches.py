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
generator ``.throw``. A bare ``patch("os.kill")`` (MagicMock), any ``return_value=`` (returned on
every call, whatever it is), ``Mock(side_effect=None)`` or a side_effect callable that cannot
raise, a recorder ``lambda`` or a ``def`` with neither a ``raise`` nor a forward is refused. For
``psutil.pid_exists`` a ``def``/``lambda`` whose every return is the literal ``True`` is refused.
Names resolve lexically (the binding visible at the patch site), never file-wide by bare name.

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
import re
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


# How the replacement is installed. A ``side_effect`` is called/raised/iterated; a
# ``return_value`` is only ever returned (Prism 3f238f646a10, t_4a3eba50).
_CALLABLE, _SIDE_EFFECT, _RETURN_VALUE = "callable", "side_effect", "return_value"
_MOCKS = frozenset({"MagicMock", "Mock", "AsyncMock", "NonCallableMock", "create_autospec"})
_PRIM_TAILS = frozenset({"kill", "waitpid", "connect", "pid_exists"})
# ``_target_of`` only matches a str constant that IS a tail (``setattr(os, "kill", ...)``) or
# ends in ``.<tail>`` (``patch("os.kill")``); a file without such a literal has no patch site.
# The bare-substring test it replaces parsed every file that says "connect" (t_44dcc531).
_PRIM_LITERAL = re.compile(r"""['"](?:[\w.]*\.)?(?:%s)['"]""" % "|".join(sorted(_PRIM_TAILS)))


def _replacement(call: ast.Call, fname: str) -> tuple[ast.expr | None, str]:
    """(expression installed, mode). ``(None, _CALLABLE)`` = a bare MagicMock."""
    kw = {k.arg: k.value for k in call.keywords}
    if fname == "setattr":
        pos = 2 if (len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str) and not isinstance(call.args[0], ast.Constant)) else 1
        if len(call.args) > pos:
            return call.args[pos], _CALLABLE
        return kw.get("value"), _CALLABLE
    # mock.patch / patch.object: ``new`` replaces the attribute outright
    pos = 2 if fname == "object" else 1
    if "new" in kw:
        return kw["new"], _CALLABLE
    if len(call.args) > pos:
        return call.args[pos], _CALLABLE
    if "side_effect" in kw and not _is_none(kw["side_effect"]):
        return kw["side_effect"], _SIDE_EFFECT
    if "return_value" in kw:
        return kw["return_value"], _RETURN_VALUE
    return None, _CALLABLE  # bare patch -> MagicMock: returns a MagicMock, never raises


def _is_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _scope_children(node: ast.AST):
    """Statements/expressions in ``node``'s own scope: nested function and class bodies are
    their own scopes and are not entered (their ``def`` statement itself is yielded)."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _scope_children(child)


_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


class _File:
    """Names resolved LEXICALLY: the binding visible at the use site, innermost scope first,
    latest binding at or above the use line (Prism cc5475624092: two tests that each define
    ``fake_kill`` no longer share the first one's verdict)."""

    def __init__(self, tree: ast.AST) -> None:
        self._resolving: set[ast.AST] = set()
        self.parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        # scope -> name -> [(lineno, kind, node)]; kind: "def" | "original" | "mock" | "other"
        self.bindings: dict[ast.AST, dict[str, list[tuple[int, str, ast.AST]]]] = {}
        for scope in (n for n in ast.walk(tree) if isinstance(n, _SCOPES)):
            table: dict[str, list[tuple[int, str, ast.AST]]] = {}
            for node in _scope_children(scope):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    table.setdefault(node.name, []).append((node.lineno, "def", node))
                elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    table.setdefault(node.targets[0].id, []).append((node.lineno, self._assign_kind(node.value), node.value))
            self.bindings[scope] = table

    @staticmethod
    def _assign_kind(value: ast.expr) -> str:
        src = _dotted(value)
        if src in PRIMITIVES or (src and src.rsplit(".", 1)[-1] in _PRIM_TAILS):
            return "original"
        if isinstance(value, ast.Call) and (_dotted(value.func) or "").rsplit(".", 1)[-1] in _MOCKS:
            return "mock"
        return "other"

    def _scopes_of(self, node: ast.AST):
        """Enclosing scopes, innermost first. A class body is visible only to code directly in
        it, never to its methods (Python's rule)."""
        cur, first = self.parent.get(node), True
        while cur is not None:
            if isinstance(cur, _SCOPES):
                if first or not isinstance(cur, ast.ClassDef):
                    yield cur
                first = False
            cur = self.parent.get(cur)

    def lookup(self, name: str, at: ast.AST) -> tuple[str, ast.AST] | None:
        line = getattr(at, "lineno", 10 ** 9)
        for scope in self._scopes_of(at):
            hits = self.bindings.get(scope, {}).get(name)
            if not hits:
                continue
            before = [h for h in hits if h[0] <= line]
            # a module/enclosing-function binding after the use line is still the one a
            # nested function sees when it runs; prefer the latest one before the use
            _, kind, node = (before or hits)[-1]
            return kind, node
        return None

    def method(self, attr: str, at: ast.AST) -> ast.AST | None:
        """``self.<attr>`` / ``cls.<attr>``: the def in the enclosing class, else None."""
        for scope in self._scopes_of(at):
            if isinstance(scope, ast.ClassDef):
                hits = self.bindings[scope].get(attr)
                if hits and hits[-1][1] == "def":
                    return hits[-1][2]
        return None

    def _calls_original(self, name: str, at: ast.AST) -> bool:
        hit = self.lookup(name, at)
        return hit is not None and hit[0] == "original"

    def _forwards(self, node: ast.AST) -> bool:
        """The body can END a poll loop: it forwards to the real primitive or raises
        through a helper (``pytest.fail``/``skip``/``exit``, a generator ``.throw``)."""
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "throw":
                return True  # (_ for _ in ()).throw(Exc) — the lambda-raise idiom
            name = _dotted(call.func)
            if name is None:
                continue
            if name in PRIMITIVES or self._calls_original(name, call):
                return True
            if name.rsplit(".", 1)[-1] in {"throw", "fail", "skip", "exit", "_exit"}:
                return True
            if "." not in name:
                hit = self.lookup(name, call)
                if hit is not None and hit[0] == "def" and hit[1] is not node and self._raises(hit[1]):
                    return True
                if hit is not None and hit[0] == "mock" and self._mock_can_end(hit[1], "raise"):
                    return True
            if name.rsplit(".", 1)[-1] in _PRIM_TAILS and "." in name:
                return True
        return False

    def _raises(self, fn: ast.AST) -> bool:
        """``fn`` raises itself or forwards through a helper that does. Guarded against
        mutually recursive helpers (Prism 16935077eaee): a def already on the resolution
        path cannot be the reason the loop ends."""
        if fn in self._resolving:
            return False
        if any(isinstance(n, ast.Raise) for n in ast.walk(fn)):
            return True
        self._resolving.add(fn)
        try:
            return self._forwards(fn)
        finally:
            self._resolving.discard(fn)

    @staticmethod
    def _constant_true(fn: ast.AST) -> bool:
        """Every ``return`` in the def's own body returns the literal True (and nothing raises)."""
        if isinstance(fn, ast.Lambda):
            return isinstance(fn.body, ast.Constant) and fn.body.value is True
        rets = [n for n in _scope_children(fn) if isinstance(n, ast.Return)]
        raises = any(isinstance(n, ast.Raise) for n in _scope_children(fn))
        return bool(rets) and not raises and all(
            isinstance(r.value, ast.Constant) and r.value.value is True for r in rets)

    def _mock_can_end(self, mock: ast.Call, kind: str) -> bool:
        """A ``Mock(...)`` call: judged by WHAT its side_effect / return_value is, not by the
        keyword being present (Prism 24f441782ae3)."""
        kw = {k.arg: k.value for k in mock.keywords}
        se = kw.get("side_effect")
        if se is not None and not _is_none(se):
            return self.can_end_loop(se, kind, _SIDE_EFFECT)
        if kind == "raise":
            return False  # returns its return_value, never raises
        rv = kw.get("return_value")
        return rv is not None and self.can_end_loop(rv, kind, _RETURN_VALUE)  # absent: a truthy MagicMock

    def _fn_can_end(self, fn: ast.AST, kind: str) -> bool:
        return not self._constant_true(fn) if kind == "true" else self._raises(fn)

    def can_end_loop(self, repl: ast.expr | None, kind: str, mode: str = _CALLABLE) -> bool:
        if repl is None:
            return False
        if mode == _RETURN_VALUE:
            # returned as-is on every call: never raises, whatever the expression is
            if kind == "raise":
                return False
            return not (isinstance(repl, ast.Constant) and repl.value is True)
        if isinstance(repl, ast.Constant):
            if kind == "true":
                return mode == _SIDE_EFFECT or repl.value is not True
            return False  # a literal: returns, never raises
        if isinstance(repl, (ast.List, ast.Tuple, ast.Set)):
            return mode == _SIDE_EFFECT  # a side_effect iterable raises StopIteration when exhausted
        if isinstance(repl, ast.Lambda):
            if kind == "true":
                return not self._constant_true(repl)
            return self._forwards(repl.body)
        if isinstance(repl, ast.Name):
            hit = self.lookup(repl.id, repl)
            if hit is not None:
                bkind, node = hit
                if bkind == "def":
                    return self._fn_can_end(node, kind)
                if bkind == "original":
                    return True
                if bkind == "mock":
                    return self._mock_can_end(node, kind)  # type: ignore[arg-type]
                if node in self._resolving:  # x = y; y = x
                    return True
                self._resolving.add(node)
                try:
                    return self.can_end_loop(node, kind, mode)  # type: ignore[arg-type]
                finally:
                    self._resolving.discard(node)
            if kind == "true":
                return True
            return repl.id[:1].isupper()  # an exception class, e.g. side_effect=ProcessLookupError
        if isinstance(repl, ast.Call):
            name = _dotted(repl.func) or ""
            tail = name.rsplit(".", 1)[-1]
            if tail in _MOCKS:
                return self._mock_can_end(repl, kind)
            if tail[:1].isupper():
                return True  # an exception instance, or a factory we cannot see into
            if "." not in name:
                hit = self.lookup(name, repl)
                if hit is not None and hit[0] == "def":  # factory: _recording_kill(signals) -> inner def
                    return self._fn_can_end(hit[1], kind)
            return True
        if isinstance(repl, ast.Attribute):
            fn = self.method(repl.attr, repl) if _dotted(repl.value) in {"self", "cls"} else None
            if fn is not None:
                return self._fn_can_end(fn, kind)
            return True
        return True


def check_source(src: str, filename: str = "<src>") -> list[str]:
    if not _PRIM_LITERAL.search(src):
        return []  # no quoted primitive name for _target_of to match: skip the parse (~98% of tests/)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src, filename=filename)
    patches = [(c, t) for c in ast.walk(tree) if isinstance(c, ast.Call) for t in [_target_of(c)] if t]
    if not patches:
        return []
    info = _File(tree)  # scope tables only for files that patch a primitive
    lines = src.splitlines()
    problems: list[str] = []
    for call, target in patches:
        line = lines[call.lineno - 1] if call.lineno <= len(lines) else ""
        if SUPPRESS in line:
            continue
        fname = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id  # type: ignore[union-attr]
        repl, mode = _replacement(call, fname)
        kind = PRIMITIVES[target]
        if info.can_end_loop(repl, kind, mode):
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
    "tests/hermes_cli/test_orphan_desktop_serve_reap.py": 4,
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
    "tests/tools/test_windows_native_support.py": 2,
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
            src = f.read_text(encoding="utf-8-sig")
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
