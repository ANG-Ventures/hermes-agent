"""Ratchet: no NEW plain ``sqlite3.connect`` in a gateway/ or hermes_cli/ module that reaches kanban
boards (t_3200d600).

A board connection opened outside ``sqlite_safe_read.connect_tracked`` is invisible to the registry
the raw-read guards consult. A raw ``close()`` of the board in the same process then cancels that
connection's POSIX locks (t_54b8e6cc, t_e87a3e20). Open board connections through
``connect_tracked`` (``kanban_db_connect._sqlite_connect`` does), or add the site below with the
reason it is safe. Calls that pass ``connect_fn=sqlite3.connect`` into ``connect_tracked`` are not
calls and are not counted.

A module is in scope when its name starts with ``kanban`` or its source names a board path
(markers below). Sites are keyed by file and enclosing function, so moving lines is free.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("gateway", "hermes_cli")
BOARD_MARKERS = ("kanban.db", "kanban_db_path", "_board_db_path", "list_boards", "board_dbs")

# (file, enclosing function) -> count, as of 06c14765df (hermes-agent#421) + t_3200d600.
# B = opens a kanban board file. o = a state.db / ledger / archive / scratch copy in a module that
# also touches boards. Every B entry is read-only (mode=ro) unless noted.
KNOWN_UNTRACKED: dict[tuple[str, str], int] = {
    ("gateway/kanban_wake_defer.py", "_session_source"): 1,  # o: profile state.db
    ("gateway/kanban_watchers_home.py", "session_home_target"): 1,  # o: state.db
    ("hermes_cli/backup.py", "_query_ro_sqlite"): 1,  # B: backup verify reads every listed DB
    ("hermes_cli/kanban_archive_read.py", "_connect_ro"): 1,  # o: archive db
    ("hermes_cli/kanban_archive_read.py", "archived_rows"): 1,  # o: archive db
    ("hermes_cli/kanban_budget.py", "_board_task_ids"): 1,  # B
    ("hermes_cli/kanban_budget.py", "_ledger_card_costs"): 1,  # o: cost ledger
    ("hermes_cli/kanban_db.py", "_board_db_is_empty._read"): 1,  # B (list_boards)
    ("hermes_cli/kanban_db.py", "_home_lineage_query"): 1,  # o: state.db
    ("hermes_cli/kanban_db.py", "connect_readonly"): 1,  # B
    ("hermes_cli/kanban_db.py", "session_owner_profile"): 1,  # o: state.db
    ("hermes_cli/kanban_db_board_registry.py", "board_of_task"): 1,  # B
    ("hermes_cli/kanban_db_connect.py", "_schema_sql_columns"): 1,  # o: :memory:
    ("hermes_cli/kanban_db_notify.py", "_chat_last_active"): 1,  # o: state.db
    ("hermes_cli/kanban_db_notify.py", "card_home_chat"): 1,  # o: state.db
    ("hermes_cli/kanban_db_notify.py", "count_notify_subs._count"): 1,  # B
    ("hermes_cli/kanban_evidence_bundle.py", "state_db_lookup"): 1,  # o: state.db
    ("hermes_cli/kanban_home_index.py", "_board_rows"): 1,  # B
    ("hermes_cli/kanban_home_index.py", "_open_rw"): 1,  # o: home index db (rw)
    ("hermes_cli/kanban_home_index.py", "known_home"): 1,  # o: home index db
    ("hermes_cli/kanban_home_index.py", "open_cards"): 1,  # o: home index db
    ("hermes_cli/kanban_identity.py", "_worker_rows"): 1,  # B
    ("hermes_cli/kanban_journal_replay.py", "replay_into"): 1,  # o: private snapshot copy (rw)
    ("hermes_cli/kanban_lifecycle_repair.py", "main"): 1,  # B (operator CLI, mode=rw, own process)
    ("hermes_cli/kanban_placement_gates.py", "_board_conn"): 2,  # B (CLI)
    ("hermes_cli/kanban_receipt.py", "_main"): 1,  # B (CLI)
    ("hermes_cli/kanban_session_closeout.py", "_ro"): 1,  # B
    ("hermes_cli/kanban_transfer.py", "_snapshot_db"): 2,  # B source (rw, backup API) + target
    ("hermes_cli/kanban_transfer.py", "export_board"): 1,  # o: private snapshot
    ("hermes_cli/kanban_work_obligations.py", "_live_run_rows"): 1,  # B
    ("hermes_cli/kanban_work_obligations.py", "session_in_operator_state"): 1,  # o: state.db
}


def _is_sqlite_connect(call: ast.Call, bare_names: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return isinstance(func.value, ast.Name) and func.value.id in {"sqlite3", "_sqlite3", "sqlite"}
    return isinstance(func, ast.Name) and func.id in bare_names


def untracked_connect_sites(root: Path) -> Counter:
    """(relpath, qualname) -> number of plain sqlite3.connect calls, for in-scope modules."""
    sites: Counter = Counter()
    for top in ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            if "connect" not in source:
                continue
            if not (path.name.startswith("kanban") or any(m in source for m in BOARD_MARKERS)):
                continue
            tree = ast.parse(source, filename=str(path))
            bare = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    and n.module in {"sqlite3", "_sqlite3"} for a in n.names if a.name == "connect"}
            rel = path.relative_to(root).as_posix()

            def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        visit(child, scope + (child.name,))
                        continue
                    if isinstance(child, ast.Call) and _is_sqlite_connect(child, bare):
                        sites[(rel, ".".join(scope) or "<module>")] += 1
                    visit(child, scope)

            visit(tree, ())
    return sites


def test_no_new_untracked_sqlite_connect_in_board_modules():
    found = untracked_connect_sites(REPO)
    new = {k: v for k, v in found.items() if v > KNOWN_UNTRACKED.get(k, 0)}
    assert not new, (
        "new plain sqlite3.connect in a module that reaches kanban boards: "
        f"{sorted(new.items())}. Open board connections with "
        "hermes_cli.sqlite_safe_read.connect_tracked (an untracked board fd lets a raw "
        "close() cancel its POSIX locks, t_54b8e6cc), or add the site to KNOWN_UNTRACKED "
        "with the reason it is safe."
    )


def test_known_untracked_list_has_no_stale_entries():
    found = untracked_connect_sites(REPO)
    stale = {k: v for k, v in KNOWN_UNTRACKED.items() if found.get(k, 0) < v}
    assert not stale, f"sites gone or moved (lower the count / drop the entry): {sorted(stale.items())}"


def test_lint_flags_a_planted_board_connect(tmp_path):
    (tmp_path / "gateway").mkdir()
    (tmp_path / "hermes_cli").mkdir()
    (tmp_path / "gateway" / "kanban_new.py").write_text(
        "import sqlite3\nfrom sqlite3 import connect as c\n"
        "def f(p):\n    return sqlite3.connect(p)\n"
        "def g(p):\n    return c(p)\n"
        "def ok(p):\n    from hermes_cli.sqlite_safe_read import connect_tracked\n"
        "    return connect_tracked(p, connect_fn=sqlite3.connect)\n",
        encoding="utf-8")
    (tmp_path / "hermes_cli" / "notes.py").write_text(
        "import sqlite3\ndef h():\n    return sqlite3.connect(':memory:')\n", encoding="utf-8")
    assert untracked_connect_sites(tmp_path) == Counter({
        ("gateway/kanban_new.py", "f"): 1, ("gateway/kanban_new.py", "g"): 1})
