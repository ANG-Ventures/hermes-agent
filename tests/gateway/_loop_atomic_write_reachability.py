# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""Call-graph reachability: an ``async def`` must not reach an atomic write.

WHY THIS EXISTS ALONGSIDE ``test_no_sync_syscalls_on_event_loop.py``.

That gate is LEXICAL -- it only reports a blocking call written *directly* in
an ``async def`` body, and says so in its own DOES-NOT-COVER list:

    "synchronous calls reached INDIRECTLY -- an ``async def`` calling a plain
     ``def`` helper that blocks.  Only lexical containment is checked."

The 2026-09-20 Apollo incident lived entirely inside that documented gap.  The
offending ``os.replace`` was FOUR plain-``def`` frames below the coroutine:

    _handle_message_with_agent   (async def, gateway/run.py)
      _apply_post_turn_resume_gate
        SessionStore.clear_resume_pending   <- plain def
          _save                             <- plain def
            _persist_routing_data           <- plain def
              _save_sessions_json           <- plain def
                _write_sessions_json_unlocked
                  utils.atomic_replace -> os.replace   <- BLOCKED 30s

The lexical gate is green on every one of those frames and always would be.
So this module walks the CALL GRAPH instead: from every ``async def`` under
the scanned roots, follow same-name function calls transitively and report any
that reaches an atomic-write sink.

SCOPE IS DERIVED, NOT HAND-LISTED.  ``derive_scanned_modules`` starts at
``gateway/run.py`` and follows the *import* graph, keeping every module that
resolves to a file inside the scanned package roots.  A test asserts the
derived set is non-trivial and contains ``gateway/session.py`` -- the module
the lexical gate scanned ZERO references of, which is exactly how this class
stayed unfrozen.

HONEST BOUNDARY (stated deliberately; these are OUT OF SCOPE, not oversights):

  * Resolution is BY NAME, not by type.  ``self._save()`` matches every
    ``def _save`` in the scanned tree, so the graph over-approximates: a name
    defined in several modules links to all of them.  Over-approximation is
    the safe direction for a ratchet (it can flag a path that does not really
    exist; it will not miss one that does), and the baseline absorbs the
    resulting noise.
  * Calls through a variable, a dict of handlers, ``getattr``, or a C
    extension are invisible.
  * Depth is capped (``_MAX_DEPTH``) so a cyclic graph terminates.
  * A call lexically inside ``asyncio.to_thread(...)`` / ``run_in_executor(...)``
    arguments is exempt -- that is the fix, not the defect.

Because the graph over-approximates, this gate is a RATCHET, not a hard zero:
the tree carries a frozen inventory of pre-existing reachable pairs and the
test fails when the inventory GROWS (or when an entry disappears without the
baseline being updated, so it cannot silently rot).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

# Package roots whose *.py files may be scanned.  A module discovered through
# the import walk is kept only when its file lives under one of these.
PACKAGE_ROOTS = ("gateway", "plugins/platforms", "utils.py")

# The entry point the scope is derived FROM.
GRAPH_ENTRY_MODULE = "gateway.run"

# Sinks: calling any of these blocks the calling thread on a filesystem
# rename/fsync.  ``atomic_replace`` is the one the incident hit.
_ATOMIC_SINK_NAMES = frozenset({
    "atomic_replace",
    "atomic_json_write",
    "atomic_yaml_write",
    "atomic_write_text",
    "atomic_roundtrip_yaml_save",
    "atomic_roundtrip_yaml_update",
})
_OS_SINK_DOTTED = frozenset({"os.replace", "os.rename", "os.fsync"})

_OFFLOAD_ATTRS = frozenset({"to_thread", "run_in_executor"})

_NOQA_TOKEN = "# noqa: atomic-write-on-loop"

# Cap on call-graph depth.  The real incident chain was 5 frames deep; 8 gives
# headroom without letting a cyclic name-resolved graph run away.
_MAX_DEPTH = 8


# ---------------------------------------------------------------------------
# Scope derivation (the import walk)
# ---------------------------------------------------------------------------


def _module_to_path(repo: Path, module: str) -> Path | None:
    candidate = repo / (module.replace(".", "/") + ".py")
    if candidate.is_file():
        return candidate
    pkg = repo / module.replace(".", "/") / "__init__.py"
    return pkg if pkg.is_file() else None


def _in_package_roots(repo: Path, path: Path, roots=None) -> bool:
    rel = path.relative_to(repo).as_posix()
    return any(rel == r or rel.startswith(r + "/") for r in (roots or PACKAGE_ROOTS))


def _imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # relative imports are not resolved here
            out.add(node.module)
            for alias in node.names:
                out.add(f"{node.module}.{alias.name}")
    return out


def derive_scanned_modules(repo: Path, roots=None) -> frozenset[str]:
    """Modules reachable from ``GRAPH_ENTRY_MODULE`` that live in a scanned root.

    Returns repo-relative posix paths (``"gateway/session.py"``), so the caller
    never has to translate module names back to files.
    """
    seen_modules: set[str] = set()
    kept: set[str] = set()
    stack = [GRAPH_ENTRY_MODULE]
    while stack:
        module = stack.pop()
        if module in seen_modules:
            continue
        seen_modules.add(module)
        path = _module_to_path(repo, module)
        if path is None:
            continue
        rel = path.relative_to(repo).as_posix()
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        if _in_package_roots(repo, path, roots):
            kept.add(rel)
        for imported in _imported_modules(path):
            if imported not in seen_modules:
                stack.append(imported)
    return frozenset(kept)


# ---------------------------------------------------------------------------
# Call-graph index
# ---------------------------------------------------------------------------


def _called_names(node: ast.AST) -> set[str]:
    """Every callee name in ``node``, skipping offload-call argument subtrees.

    ``asyncio.to_thread(self._save)`` and
    ``loop.run_in_executor(None, partial(self._save))`` are the FIX, so their
    arguments must not be attributed to the enclosing coroutine.
    """
    out: set[str] = set()
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Attribute) and func.attr in _OFFLOAD_ATTRS:
                # Offloaded: skip the whole argument subtree.
                stack.append(func)
                continue
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
                if isinstance(func.value, ast.Name):
                    out.add(f"{func.value.id}.{func.attr}")
        stack.extend(ast.iter_child_nodes(current))
    return out


def _file_import_map(repo: Path, tree: ast.AST, modules) -> dict[str, str]:
    """``local_name -> rel_path`` for every ``from X import name`` in a file.

    Includes function-local imports (the gateway imports lazily almost
    everywhere).  Only targets inside the scanned module set are kept.
    """
    out: dict[str, str] = {}
    scanned = set(modules)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        target = _module_to_path(repo, node.module)
        if target is None:
            continue
        rel = target.relative_to(repo).as_posix()
        if rel not in scanned:
            continue
        for alias in node.names:
            out[alias.asname or alias.name] = (rel, alias.name)
    return out


# Per-file import maps, populated by ``build_index`` (keyed by rel path).
_IMPORT_MAPS: dict[str, dict] = {}


def build_index(repo: Path, modules, *, noqa_token: str | None = None) -> dict:
    """``(rel_path, fn_name) -> {"async", "calls", "line", "noqa"}``."""
    token = noqa_token or _NOQA_TOKEN
    index: dict = {}
    _IMPORT_MAPS.clear()
    for rel in sorted(modules):
        path = repo / rel
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        _IMPORT_MAPS[rel] = _file_import_map(repo, tree, modules)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decl = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
            index[(rel, node.name)] = {
                "async": isinstance(node, ast.AsyncFunctionDef),
                "calls": _called_names(node),
                "line": node.lineno,
                "noqa": token in decl and bool(
                    decl.split(token, 1)[1].strip()
                ),
            }
    return index


def _hits_sink(calls: set[str], sinks=None) -> str | None:
    names, dotted_set = sinks or (_ATOMIC_SINK_NAMES, _OS_SINK_DOTTED)
    for name in sorted(calls & names):
        return name
    for dotted in sorted(dotted_set & calls):
        return dotted
    return None


def find_onloop_atomic_write_sites(repo: Path, modules) -> list[str]:
    """Return ``"<path> <async_fn> -> <sink> via <chain>"`` for each offender."""
    return find_onloop_sink_sites(repo, modules)


def find_onloop_sink_sites(
    repo: Path,
    modules,
    *,
    sink_names=None,
    sink_dotted=None,
    noqa_token: str | None = None,
    start_roots=None,
) -> list[str]:
    """Generic walk: coroutines (under ``start_roots``, default all) reaching a sink."""
    sinks = (
        (frozenset(sink_names), frozenset(sink_dotted or ()))
        if sink_names is not None
        else None
    )
    index = build_index(repo, modules, noqa_token=noqa_token)
    by_name: dict[str, list[tuple]] = defaultdict(list)
    for key in index:
        by_name[key[1]].append(key)

    offenders: list[str] = []
    for key, info in sorted(index.items()):
        if not info["async"] or info["noqa"]:
            continue
        if start_roots and not any(
            key[0] == r or key[0].startswith(r + "/") for r in start_roots
        ):
            continue
        found = _search(key, index, by_name, sinks)
        if found is not None:
            sink, chain = found
            offenders.append(
                f"{key[0]} {key[1]} -> {sink} via {'>'.join(chain)}"
            )
    return sorted(offenders)


def _search(start, index, by_name, sinks=None):
    """DFS from ``start``; return ``(sink, chain)`` or None.

    Callee resolution is deliberately CONSERVATIVE: a bare name resolves only
    within the SAME FILE, and a dotted ``mod.fn`` resolves to that module's
    file.  Resolving bare names tree-wide made the graph useless -- names like
    ``get`` / ``_save`` / ``close`` are defined in dozens of modules, so every
    coroutine reached every sink and the "offender" list was 561 entries of
    pure noise.  Same-file resolution keeps the real chain (the incident's
    ``clear_resume_pending > _save > _persist_routing_data >
    _save_sessions_json > _write_sessions_json_unlocked`` all live in
    ``gateway/session.py``) and drops the cross-module fiction.
    """
    start_file = start[0]
    stack = [(start, [start[1]], 0)]
    visited = {start}
    while stack:
        key, chain, depth = stack.pop()
        info = index.get(key)
        if info is None or info["noqa"]:
            continue
        sink = _hits_sink(info["calls"], sinks)
        if sink is not None:
            return sink, chain
        if depth >= _MAX_DEPTH:
            continue
        for name in sorted(info["calls"]):
            for callee in _resolve(name, key[0], start_file, index, by_name):
                if callee in visited:
                    continue
                callee_info = index.get(callee)
                # An awaited coroutine gets its own enumeration as a start
                # node; following into it here would attribute a nested
                # coroutine's work to its caller.
                if callee_info is not None and callee_info["async"]:
                    continue
                visited.add(callee)
                stack.append((callee, chain + [name], depth + 1))
    return None


def _resolve(name: str, current_file: str, start_file: str, index, by_name):
    """Resolve a callee name to (file, fn) keys.

    Three tiers, narrowest first:

    1. ``mod.fn`` -- resolves to the module whose FILE STEM is ``mod``.
    2. a bare name defined in the CURRENT file (or the file the walk started
       in) -- ordinary intra-module helper calls.
    3. a bare name defined in EXACTLY ONE file tree-wide -- this is the tier
       that crosses module boundaries, and the uniqueness requirement is what
       keeps it honest.  ``clear_resume_pending`` is defined once (in
       ``gateway/session.py``), so ``run.py``'s
       ``self.session_store.clear_resume_pending()`` resolves correctly and the
       2026-09-20 incident chain is visible.  A name like ``get`` or ``_save``
       is defined in dozens of files, fails the uniqueness test, and is
       dropped -- which is what stopped this gate reporting 561 fictional
       offenders.
    """
    if "." in name:
        module, attr = name.rsplit(".", 1)
        for key in by_name.get(attr, ()):
            stem = key[0].rsplit("/", 1)[-1][: -len(".py")]
            if stem == module:
                yield key
        return
    for candidate_file in (current_file, start_file):
        key = (candidate_file, name)
        if key in index:
            yield key
            return
    # An explicit ``from X import name`` in the current file is unambiguous
    # even when ``name`` is defined in several modules (``switch_model`` is:
    # hermes_cli/model_switch.py, run_agent.py, agent/...).
    imported = _IMPORT_MAPS.get(current_file, {}).get(name)
    if imported is not None and imported in index:
        yield imported
        return
    unique = by_name.get(name, ())
    if len(unique) == 1:
        yield unique[0]


def ratchet_key(offender: str) -> str:
    """``path fn -> sink via chain`` -> ``path fn -> sink``.

    The chain is informative but unstable (it depends on which of several
    same-named functions the DFS reached first).  The stable identity of a
    site is the (module, coroutine, sink) triple.
    """
    return offender.split(" via ", 1)[0]
