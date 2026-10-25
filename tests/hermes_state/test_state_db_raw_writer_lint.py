"""Every write-capable connection to state.db goes through SessionDB (t_aeae3fe7, Apollo ruling on
Prism 7072ffc85b72).

The WAL lock guard closes its private ``-shm`` descriptor when the last guarded SessionDB handle on
the inode releases. A POSIX ``close()`` cancels every lock this process holds on that inode, so an
UNGUARDED in-process connection on the same ``-shm`` loses its locks at that moment. That window is
accepted only because such connections are read-only: a split WAL generation corrupts state.db
only when a WRITER keeps writing to it, and every SessionDB writer is guarded and counted in
``hermes_state_lockguard._HANDLES``. This lint keeps the premise true.

It walks the repo (``tests/`` and ``hermes_state*.py`` excluded) and fails on any
``sqlite3.connect(...)`` whose first argument names or resolves to state.db (a ``"state.db"``
literal anywhere in the expression, a ``*state_db*`` name/attribute/call, or a local variable
assigned from one) unless the call is read-only (``mode=ro`` in the expression, or ``uri=True``
with ``?mode=ro``) or is on :data:`ALLOWLIST` with a reason.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", "tests", "node_modules", "venv", ".venv", "__pycache__", ".worktrees", "build", "dist"}
_STATE_DB_NAME = re.compile(r"state_db", re.IGNORECASE)
_RO_HELPERS = {"read_only_db_uri"}  # hermes_state_holders: file: URI + ?mode=ro

# (repo-relative path, enclosing function name) -> why this raw connect is acceptable.
# Every entry must be read-only in practice or a one-shot offline tool that never runs beside a
# guarded gateway writer in the same process.
_EVAL = "offline eval harness on a throwaway HERMES_HOME; never runs in a process beside the live gateway's guarded writer"
ALLOWLIST: dict[tuple[str, str], str] = {
    ("hermes_state.py", "_open_writer_conn"): "SessionDB's own writer connection: guarded by hold() and counted",
    ("hermes_state_repair.py", "_connect_repair_durable"):
        "repair surgery under the cross-process repair lock, refused while a live writer holds state.db",
    ("gateway/lifecycle_ledger.py", "check_state_db_integrity"):
        "PRAGMA quick_check only, writes nothing; opened read-write because a WAL store needs its -shm to open",
    ("hermes_cli/doctor_state.py", "_write_health_reason"):
        "the write probe targets a snapshot copy in a TemporaryDirectory, never the live state.db",
    ("evals/acp_empty_session_wire.py", "rows"): _EVAL,
    ("evals/acp_empty_session_wire.py", "main"): _EVAL,
    ("evals/delivery_flood_wire.py", "rows"): _EVAL,
    ("evals/gateway_failure_ownership/probe.py", "rows"): _EVAL,
    ("evals/gateway_failure_ownership/probe.py", "main"): _EVAL,
    ("evals/desktop_bug_campaign/persistence_live.py", "main"): _EVAL,
    ("evals/desktop_bug_campaign/leases_live.py", "main"): _EVAL,
}

# (repo-relative path, enclosing function name) -> why a state.db connect outside registered_reader()
# is acceptable. The registration only matters in a process that also runs a guarded SessionDB (the
# lock guard's private descriptors live in that process); a script with no SessionDB has none.
_NO_SESSIONDB = "no SessionDB in this process, so the lock guard holds no private descriptor to close under it"
UNREGISTERED_OK: dict[tuple[str, str], str] = {
    ("scripts/e2e_internal_silence.py", "_db_rows"): _NO_SESSIONDB,
    ("evals/acp_empty_session_wire.py", "rows"): _NO_SESSIONDB + " (the gateway runs in an acp_adapter subprocess)",
    ("evals/acp_empty_session_wire.py", "main"): _NO_SESSIONDB + " (the gateway runs in an acp_adapter subprocess)",
    ("evals/delivery_flood_wire.py", "rows"): _NO_SESSIONDB + " (the gateway runs in a subprocess)",
    ("evals/desktop_bug_campaign/persistence_live.py", "main"): _NO_SESSIONDB + " (real backend subprocesses)",
    ("evals/desktop_bug_campaign/leases_live.py", "main"): _NO_SESSIONDB + " (real backend subprocesses)",
    # FINDING (t_aeae3fe7 PR body): this harness runs SessionDB + GatewayRunner in-process on a
    # throwaway HERMES_HOME and reads state.db with `with sqlite3.connect(...)`, which never closes.
    # A torn read there can only fail the eval run; left for the eval's owner.
    ("evals/gateway_failure_ownership/probe.py", "rows"): "eval harness on a throwaway home; finding, see comment",
    ("evals/gateway_failure_ownership/probe.py", "main"): "eval harness on a throwaway home; finding, see comment",
    # Found by the shape rules (``.as_uri()`` + ``mode=ro``, or a read-only connect in a module that
    # imports hermes_state*; Prism ad2e7f0783a5), and NOT state.db: the lock guard holds private
    # descriptors only on state.db inodes, so a connect on any other file cannot lose locks to it.
    **{site: "reads lcm.db (the LCM store), not state.db" for site in (
        ("plugins/context_engine/lcm/vector_store.py", "_load_binary_with_deadline"),
        ("plugins/context_engine/lcm/command.py", "_embedding_read_connection"),
        ("plugins/context_engine/lcm/retrieval_core.py", "hydrate_chunk_hits"),
        ("plugins/context_engine/lcm/retrieval_core.py", "hydrate_semantic_nodes"),
        ("plugins/context_engine/lcm/tools.py", "invoke"),
        ("plugins/context_engine/lcm/tools.py", "_lcm_recall_summary_source_hits"),
    )},
    **{site: "reads a kanban board db / the home index, not state.db" for site in (
        ("hermes_cli/kanban_session_closeout.py", "_ro"),
        ("hermes_cli/kanban_identity.py", "_worker_rows"),
        ("hermes_cli/kanban_db.py", "_board_db_is_empty"),
        ("hermes_cli/kanban_db.py", "connect_readonly"),
        ("hermes_cli/kanban_home_index.py", "_board_rows"),
        ("hermes_cli/kanban_home_index.py", "known_home"),
        ("hermes_cli/kanban_home_index.py", "open_cards"),
        ("hermes_cli/kanban_db_board_registry.py", "board_of_task"),
        ("hermes_cli/kanban_db_notify.py", "count_notify_subs"),
        ("agent/kanban_turn_recovery.py", "worker_claim_is_live"),
    )},
    ("hermes_cli/data_cleanup_holders.py", "_refuse_cron_writers"): "reads cron/executions.db, not state.db",
    ("hermes_cli/browser_connect.py", "_copy_auth_file"): "copies a browser profile's auth sqlite, not state.db",
    ("hermes_cli/backup_restore.py", "_safe_restore_db"): "reads the snapshot copy being restored, never the live state.db",
    ("tools/read_extract.py", "_extract_sqlite"):
        "immutable=1 user-file extraction: no -shm, no locks to lose (a state.db path reads the main file only)",
}


def _py_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            # hermes_state*.py is scanned too (Prism 230be3dd449d: its helpers open state.db beside live
            # SessionDB handles); SessionDB's own connects are registered in code, see _OWN_CONNECTS.
            if not name.endswith(".py") or name.startswith("test_") or name.endswith("_test.py"):
                continue
            path = Path(root, name)
            yield path, path.relative_to(REPO).as_posix()


# Helpers that open a sqlite3 connection on the path they are given (hermes_state_dbfile /
# hermes_cli.sqlite_safe_read): a state.db connect through them is a connect like any other.
_TRACKED_CONNECTS = {"_connect_tracked_db", "connect_tracked"}


def _is_sqlite_connect(call: ast.Call, connect_aliases: set[str]) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "connect" and isinstance(f.value, ast.Name):
        return f.value.id in {"sqlite3", "_sqlite3", "sqlite"}
    if isinstance(f, ast.Attribute) and f.attr in _TRACKED_CONNECTS:
        return True
    return isinstance(f, ast.Name) and (f.id in connect_aliases or f.id in _TRACKED_CONNECTS)


def _strings(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _names(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr


def _assignments(scope: ast.AST) -> dict[str, list[ast.AST]]:
    out: dict[str, list[ast.AST]] = {}
    for sub in ast.walk(scope):
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, []).append(sub.value)
        elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)) and isinstance(sub.target, ast.Name) and sub.value:
            out.setdefault(sub.target.id, []).append(sub.value)
    return out


# Modules whose functions receive the state.db path as a plain ``db_path`` (Prism 230be3dd449d): there,
# a ``db_path`` argument names state.db too. Elsewhere ``db_path`` is any database (kanban.db, ...).
_STATE_DB_PATH_MODULES = re.compile(
    r"^(hermes_state[a-z_]*\.py|tui_gateway/change_watcher\.py|hermes_cli/backup_sqlite\.py)$")
# generic path names that carry state.db in those modules (backup_sqlite copies state.db as ``src``,
# Prism 95ebc4a81ebe)
_DB_PATH_NAME = re.compile(r"^(db_path|tracking_path|src|source)$")


def _names_state_db(expr: ast.AST, assigns: dict[str, list[ast.AST]], seen: set | None = None,
                    db_path_names: bool = False) -> bool:
    seen = set() if seen is None else seen  # shared across the walk: linear, not per-path exponential
    if any("state.db" in s for s in _strings(expr)):
        return True
    for name in _names(expr):
        if _STATE_DB_NAME.search(name) or (db_path_names and _DB_PATH_NAME.search(name)):
            return True
        if name in assigns and name not in seen:
            seen.add(name)
            if any(_names_state_db(v, assigns, seen, db_path_names) for v in assigns[name]):
                return True
    return False


def _proven_ro(expr: ast.AST, assigns: dict[str, list[ast.AST]], seen: frozenset = frozenset()) -> bool:
    """True only when EVERY value *expr* can take is a read-only target (Prism 97721fca0cf2): a
    ``mode=ro`` literal in the string itself, or a ``read_only_db_uri(...)`` call. A conditional is
    read-only only if both branches are; a name only if every assignment is (a parameter, with no
    assignment, is unproven). Anything else counts as write-capable."""
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str) and "mode=ro" in expr.value
    if isinstance(expr, ast.JoinedStr):
        return any(isinstance(v, ast.Constant) and isinstance(v.value, str) and "mode=ro" in v.value
                   for v in expr.values)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):  # unconditional concatenation
        return _proven_ro(expr.left, assigns, seen) or _proven_ro(expr.right, assigns, seen)
    if isinstance(expr, ast.IfExp):
        return _proven_ro(expr.body, assigns, seen) and _proven_ro(expr.orelse, assigns, seen)
    if isinstance(expr, ast.Call):
        f = expr.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        return name in _RO_HELPERS
    if isinstance(expr, ast.Name):
        values = assigns.get(expr.id)
        if not values or expr.id in seen:
            return False
        return all(_proven_ro(v, assigns, seen | {expr.id}) for v in values)
    return False


def _read_only(call: ast.Call, assigns: dict[str, list[ast.AST]]) -> bool:
    target = call.args[0] if call.args else next(
        (kw.value for kw in call.keywords if kw.arg == "database"), None)
    return target is not None and _proven_ro(target, assigns)


# Connects registered in code rather than by a ``with registered_reader`` block: each function calls
# ``register_reader`` before the connect and drops the token only after the connection's close.
# Verified behaviourally by tests/hermes_state/test_lockguard_private_fd.py.
_REGISTERED_IN_CODE: dict[tuple[str, str], str] = {
    ("hermes_state.py", "_connect_read_only"): "token kept per connection, dropped by _unregister_reader after close",
    ("hermes_state.py", "_open_writer_conn"): "the writer: register_reader in _open_writer / reopen until hold() counts it",
    ("hermes_state_repair.py", "_connect_repair_durable"): "token bound to the tracked close()",
    ("tui_gateway/change_watcher.py", "_session_db_content_sig"): "register before the try, unregister after close in finally",
    ("hermes_state_dbfile.py", "collect_state_db_stats"): "register before the connect, unregister after close in finally",
    ("hermes_cli/backup_sqlite.py", "_safe_copy_db"): "_register_reader before the source connect, _unregister_reader after its close in finally",
}


def _is_registered_reader(expr: ast.AST) -> bool:
    """``registered_reader(...)`` / ``hermes_state_lockguard.registered_reader(...)``."""
    if not isinstance(expr, ast.Call):
        return False
    f = expr.func
    return (isinstance(f, ast.Name) and f.id == "registered_reader") or (
        isinstance(f, ast.Attribute) and f.attr == "registered_reader")


def _imports_hermes_state(tree: ast.AST) -> bool:
    """The module imports ``hermes_state`` or a ``hermes_state_*`` module (anywhere, incl. lazily)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0].startswith("hermes_state"):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[0].startswith("hermes_state") for a in node.names):
                return True
    return False


def _builds_as_uri(expr: ast.AST, assigns: dict[str, list[ast.AST]], seen: frozenset = frozenset()) -> bool:
    """The target expression (or a local it is assigned from) calls ``.as_uri()``."""
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "as_uri":
            return True
        if isinstance(sub, ast.Name) and sub.id in assigns and sub.id not in seen:
            if any(_builds_as_uri(v, assigns, seen | {sub.id}) for v in assigns[sub.id]):
                return True
    return False


def _scan_all(path: Path, rel: str):
    """Every state.db ``sqlite3.connect`` in *path*: ``(rel, fn, line, read_only, registered)``.
    ``registered`` = the call sits lexically inside a ``with registered_reader(...)`` block, or in a
    later item of the same ``with`` statement."""
    try:
        text = path.read_text(encoding="utf-8")
        if "connect" not in text or ("sqlite" not in text and "_connect_tracked_db" not in text):
            return []  # cheap pre-filter: most files never open sqlite
        tree = ast.parse(text, filename=rel)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "sqlite3"
               for a in n.names if a.name == "connect"}
    hits = []
    state_mod = bool(_STATE_DB_PATH_MODULES.match(rel))
    imports_state = _imports_hermes_state(tree)

    def visit(scope: ast.AST, fn_name: str, assigns, registered: bool):
        if isinstance(scope, (ast.With, ast.AsyncWith)):
            covered = registered
            for item in scope.items:
                visit(item, fn_name, assigns, covered)
                covered = covered or _is_registered_reader(item.context_expr)
            for stmt in scope.body:
                visit_node(stmt, fn_name, assigns, covered)
            return
        for child in ast.iter_child_nodes(scope):
            visit_node(child, fn_name, assigns, registered)

    def visit_node(child: ast.AST, fn_name: str, assigns, registered: bool):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A nested def runs when called, not where it is defined: registration does not carry in.
            visit(child, child.name, {**assigns, **_assignments(child)}, False)
            return
        if isinstance(child, ast.Call) and _is_sqlite_connect(child, aliases):
            target = child.args[0] if child.args else next(
                (kw.value for kw in child.keywords if kw.arg == "database"), None)
            if target is None:
                pass
            elif _names_state_db(target, assigns, db_path_names=state_mod):
                hits.append((rel, fn_name, child.lineno, _read_only(child, assigns), registered))
            elif _read_only(child, assigns) and (imports_state or _builds_as_uri(target, assigns)):
                # Shape, not name (Prism ad2e7f0783a5): a ``?mode=ro`` connect on ``path.as_uri()``,
                # or any read-only connect in a module that imports hermes_state*, may be state.db
                # under a generic variable name. Read-only only: the raw-writer check stays by name.
                hits.append((rel, fn_name, child.lineno, True, registered))
        visit(child, fn_name, assigns, registered)

    visit(tree, "<module>", _assignments(tree), False)
    return hits


def _scan_registered(path: Path, rel: str):
    """:func:`_scan_all` with the :data:`_REGISTERED_IN_CODE` sites marked registered."""
    return [(r, fn, line, ro, reg or (r, fn) in _REGISTERED_IN_CODE) for r, fn, line, ro, reg in _scan_all(path, rel)]


def _scan(path: Path, rel: str):
    """Raw write-capable state.db connects: ``(rel, fn, line)``."""
    return [(rel, fn, line) for rel, fn, line, ro, _reg in _scan_all(path, rel) if not ro]


_ALL_SITES = None


def _all_state_db_connects():
    global _ALL_SITES
    if _ALL_SITES is None:  # one repo walk (~14 s) shared by the tests below
        _ALL_SITES = [hit for path, rel in _py_files() for hit in _scan_registered(path, rel)]
    return _ALL_SITES


def _raw_state_db_writers():
    return [(rel, fn, line) for rel, fn, line, ro, _reg in _all_state_db_connects() if not ro]


def _unregistered_readers():
    """Read-only or allowlisted state.db connects outside a ``registered_reader`` block."""
    return [(rel, fn, line) for rel, fn, line, ro, reg in _all_state_db_connects()
            if not reg and (ro or (rel, fn) in ALLOWLIST) and (rel, fn) not in UNREGISTERED_OK]


def test_lint_flags_a_raw_writer_and_passes_read_only(tmp_path):
    src = tmp_path / "probe.py"
    src.write_text(
        "import sqlite3, os\n"
        "from hermes_constants import get_hermes_home\n"
        "def writer():\n"
        "    p = os.path.join(get_hermes_home(), 'state.db')\n"
        "    return sqlite3.connect(p)\n"
        "def writer2():\n"
        "    return sqlite3.connect(state_db_path())\n"
        "def reader():\n"
        "    return sqlite3.connect(f'file:{state_db_path()}?mode=ro', uri=True)\n"
        "def reader2(state_db_path):\n"
        "    return sqlite3.connect(read_only_db_uri(state_db_path), uri=True)\n"
        "def other():\n"
        "    return sqlite3.connect('kanban.db')\n"
    )
    hits = _scan(src, "probe.py")
    assert [(fn, line) for _rel, fn, line in hits] == [("writer", 5), ("writer2", 7)]


def test_lint_requires_registered_reader_around_read_only_and_allowlisted(tmp_path):
    """Prism f73fc9c5f6cc / 5ac4f2a27277: the lock guard closes a private -shm fd once no guarded
    writer or REGISTERED reader uses the inode, so an unregistered in-process reader would lose its
    WAL read-mark at that close. Every read-only / allowlisted connect sits in registered_reader()."""
    src = tmp_path / "probe.py"
    src.write_text(
        "import sqlite3, contextlib\n"
        "from hermes_state_lockguard import registered_reader\n"
        "def bare():\n"
        "    return sqlite3.connect(f'file:{state_db_path()}?mode=ro', uri=True)\n"
        "def wrapped():\n"
        "    with registered_reader(state_db_path()):\n"
        "        con = sqlite3.connect(f'file:{state_db_path()}?mode=ro', uri=True)\n"
        "        con.close()\n"
        "def same_with():\n"
        "    with registered_reader(p), contextlib.closing(sqlite3.connect(read_only_db_uri(state_db), uri=True)):\n"
        "        pass\n"
        "def wrong_order():\n"
        "    with contextlib.closing(sqlite3.connect(read_only_db_uri(state_db), uri=True)), registered_reader(p):\n"
        "        pass\n"
        "def nested_def():\n"
        "    with registered_reader(p):\n"
        "        def later():\n"
        "            return sqlite3.connect(read_only_db_uri(state_db), uri=True)\n"
        "        return later\n"
    )
    hits = _scan_all(src, "probe.py")
    unregistered = [(fn, line) for _rel, fn, line, ro, reg in hits if ro and not reg]
    assert unregistered == [("bare", 4), ("wrong_order", 13), ("later", 18)], hits
    assert sorted(fn for _r, fn, _l, ro, reg in hits if reg) == ["same_with", "wrapped"]


def test_lint_flags_read_only_connects_by_shape_not_only_by_name(tmp_path):
    """Prism ad2e7f0783a5: ``kanban_db._home_lineage_query(path, sid)`` and the dashboard's
    ``_viewer_home_ids`` read state.db as ``path.resolve().as_uri() + '?mode=ro'``; the name rule
    (``state_db``) missed them. A read-only connect on ``.as_uri()``, or any read-only connect in a
    module that imports hermes_state*, is a reader site regardless of the variable name."""
    plain = tmp_path / "plain.py"
    plain.write_text(
        "import sqlite3\n"
        "def lineage(path):\n"
        "    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)\n"
        "    conn.close()\n"
        "def vianame(path):\n"
        "    uri = path.resolve().as_uri() + '?mode=ro'\n"
        "    return sqlite3.connect(uri, uri=True)\n"
        "def other(path):\n"
        "    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)\n"
        "def writer(path):\n"
        "    return sqlite3.connect(path.resolve().as_uri(), uri=True)\n"
    )
    assert [(fn, ro, reg) for _r, fn, _l, ro, reg in _scan_all(plain, "plain.py")] == [
        ("lineage", True, False), ("vianame", True, False)]
    importer = tmp_path / "importer.py"
    importer.write_text(
        "import sqlite3\n"
        "from hermes_state_lockguard import registered_reader\n"
        "def other(path):\n"
        "    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)\n"
        "def wrapped(path):\n"
        "    with registered_reader(path):\n"
        "        sqlite3.connect(f'file:{path}?mode=ro', uri=True).close()\n"
    )
    assert [(fn, reg) for _r, fn, _l, _ro, reg in _scan_all(importer, "importer.py")] == [
        ("other", False), ("wrapped", True)]


def test_every_read_only_state_db_connect_is_a_registered_reader():
    offenders = _unregistered_readers()
    assert not offenders, (
        "in-process state.db connection outside hermes_state_lockguard.registered_reader(): the WAL "
        "lock guard can close its private -shm descriptor under it and cancel the connection's POSIX "
        f"read locks (a torn read). Wrap the connect AND its close in `with registered_reader(path):`: {offenders}"
    )


def test_conditional_target_is_not_read_only(tmp_path):
    """Prism 97721fca0cf2: a ``mode=ro`` string somewhere in the target does not make it read-only."""
    src = tmp_path / "probe.py"
    src.write_text(
        "import sqlite3\n"
        "def cond(readonly):\n"
        "    uri = f'file:{state_db_path()}?mode=ro' if readonly else str(state_db_path())\n"
        "    return sqlite3.connect(uri, uri=True)\n"
        "def inline(readonly):\n"
        "    return sqlite3.connect(f'file:{state_db_path()}?mode=ro' if readonly else state_db_path())\n"
        "def both(readonly):\n"
        "    return sqlite3.connect(f'file:{state_db_path()}?mode=ro' if readonly else read_only_db_uri(state_db_path()), uri=True)\n"
        "def reassigned():\n"
        "    uri = f'file:{state_db_path()}?mode=ro'\n"
        "    uri = str(state_db_path())\n"
        "    return sqlite3.connect(uri, uri=True)\n"
    )
    hits = _scan(src, "probe.py")
    assert [fn for _rel, fn, _line in hits] == ["cond", "inline", "reassigned"], hits


def test_no_raw_write_capable_state_db_connections():
    offenders = [h for h in _raw_state_db_writers() if (h[0], h[1]) not in ALLOWLIST]
    assert not offenders, (
        "raw write-capable sqlite3.connect() on state.db outside SessionDB (the WAL lock guard's "
        "residual close race is accepted only for read-only in-process connections): open it with "
        "f'file:{path}?mode=ro', uri=True, route writes through SessionDB, or add an ALLOWLIST entry "
        f"with the reason: {offenders}"
    )


def test_allowlist_has_no_stale_entries():
    live = {(rel, fn) for rel, fn, _line in _raw_state_db_writers()}
    stale = sorted(set(ALLOWLIST) - live)
    assert not stale, f"ALLOWLIST entries that no longer match a raw connect: {stale}"
    unreg = {(rel, fn) for rel, fn, _l, _ro, reg in _all_state_db_connects() if not reg}
    stale = sorted(set(UNREGISTERED_OK) - unreg)
    assert not stale, f"UNREGISTERED_OK entries that no longer match an unregistered connect: {stale}"
    sites = {(rel, fn) for rel, fn, *_ in _all_state_db_connects()}
    stale = sorted(set(_REGISTERED_IN_CODE) - sites)
    assert not stale, f"_REGISTERED_IN_CODE entries that no longer match a connect: {stale}"
