"""S7: boards have no global pointer.

``kanban/current`` and ``HERMES_KANBAN_BOARD`` are deleted. A verb names its board (``--board``),
or the DB pin does, or its card id does (``board_of_task``, never guessing); otherwise it is
refused with the next command. Spec: hermes-home
plans/2026-10-21_sdlc-simplification/S7-boards-no-global-pointer-SPEC.md (D1-D7, I1-I4).
"""

from __future__ import annotations

import argparse
import ast
import time
import warnings
from pathlib import Path

import pytest

from hermes_cli import kanban_board_select as kbsel
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb._PINNED_BOARD_MEMO.clear()
    return h


def _pin(monkeypatch, path: Path) -> None:
    """A dispatched worker's DB pin. The sandbox flag would neutralise it, so drop that."""
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    kb._PINNED_BOARD_MEMO.clear()


def _card(board: str, title: str = "t") -> str:
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, title=title, assignee="dev")


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# --- D6 rule 3: the pointer cannot come back -------------------------------------------------

def test_pointer_functions_are_deleted():
    for name in ("set_current_board", "clear_current_board", "current_board_path"):
        assert not hasattr(kb, name), name


def test_get_current_board_reads_no_file_and_no_environ():
    src = (REPO / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "get_current_board")
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("read_text", "exists", "environ"), node.attr
        if isinstance(node, ast.Name):
            assert node.id != "environ"


def test_no_runtime_module_names_the_board_env_var():
    """I4(c): every .py under the runtime root except tests/ and website/ (any API form)."""
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).parts
        if rel[0] in ("tests", "website", ".venv", "node_modules") or "node_modules" in rel:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "HERMES_KANBAN_BOARD":
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == []


def test_cli_board_help_teaches_neither_deleted_mechanism():
    """RC-12: the --board help no longer names the pointer or the env var."""
    from hermes_cli import kanban_parser

    text = kanban_parser._BOARD_HELP
    for gone in ("boards switch", "HERMES_KANBAN_BOARD", "current board", "kanban/current"):
        assert gone not in text


# --- D1: the resolver --------------------------------------------------------------------------

def test_file_and_env_are_inert(home, monkeypatch):
    kb.create_board("subs-ace")
    (home / "kanban" / "current").write_text("subs-ace\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "subs-ace")
    assert kb.get_current_board() == kb.DEFAULT_BOARD
    with kb.scoped_current_board("subs-ace"):
        assert kb.get_current_board() == "subs-ace"


def test_pin_names_its_board(home, monkeypatch):
    kb.create_board("subs-ace")
    _card("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    assert kb.pinned_board() == "subs-ace"
    assert kb.get_current_board() == "subs-ace"


def test_pin_inverse_is_spelling_blind(home, monkeypatch):
    """RC-7: the same file spelled another way (symlinked parent) names the same board."""
    kb.create_board("subs-ace")
    _card("subs-ace")
    alias = home.parent / "alias-root"
    alias.symlink_to(home, target_is_directory=True)
    _pin(monkeypatch, alias / "kanban" / "boards" / "subs-ace" / "kanban.db")
    assert kb.pinned_board() == "subs-ace"


def test_layout_external_pin_is_override_and_routes_from_the_pin(home, monkeypatch, tmp_path):
    """RC-7: a pin outside every board's layout -> ``override``; the verb still works off the pin."""
    db = tmp_path / "elsewhere" / "custom.db"
    db.parent.mkdir()
    conn = kbc.connect(db_path=db)
    tid = kb.create_task(conn, title="x", assignee="dev")
    conn.close()
    _pin(monkeypatch, db)
    assert kb.pinned_board() == kb.PINNED_SLUG
    assert kb.get_current_board() == kb.DEFAULT_BOARD  # layout callers keep default's paths
    assert kbsel.resolve(_ns(kanban_action="show", board=None, task_id=tid)) == kb.PINNED_SLUG
    with kbc.connect_closing() as c:  # the pin is the DB, D3 never consulted
        assert kb.get_task(c, tid) is not None


def test_pinned_board_is_total_on_an_unreadable_boards_dir(home, monkeypatch):
    """RC-11: any error naming the pin -> ``override``, never a raise."""
    kb.create_board("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")

    def boom(*a, **k):
        raise PermissionError("boards/ unreadable")

    monkeypatch.setattr(kb, "_board_db_layout", boom)
    assert kb.pinned_board() == kb.PINNED_SLUG
    assert kb.get_current_board() == kb.DEFAULT_BOARD


def test_pinned_board_cost_is_paid_once_per_pin(home, monkeypatch):
    """RC-11: memoised; a second call does not rescan boards/."""
    kb.create_board("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    calls = []
    real = kb._board_db_layout
    monkeypatch.setattr(kb, "_board_db_layout", lambda: calls.append(1) or real())
    assert kb.pinned_board() == "subs-ace"
    assert kb.pinned_board() == "subs-ace"
    assert len(calls) == 1


# --- D3: the registry never guesses ------------------------------------------------------------

def test_board_of_task_finds_the_one_board(home):
    kb.create_board("alpha")
    tid = _card("alpha")
    assert kb.board_of_task(tid) == "alpha"


def test_board_of_task_not_found(home):
    kb.create_board("alpha")
    with pytest.raises(kb.KanbanCardNotFound, match="is on no board"):
        kb.board_of_task("t_deadbeef")


def _copy_card(tid: str, src: str, dst: str) -> None:
    with kbc.connect_closing(board=src) as s, kbc.connect_closing(board=dst) as d:
        row = s.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        cols = row.keys()
        d.execute(f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  tuple(row))
        d.commit()


def test_board_of_task_ambiguous_on_two_live_boards(home):
    kb.create_board("alpha")
    kb.create_board("beta")
    tid = _card("alpha")
    _copy_card(tid, "alpha", "beta")
    with pytest.raises(kb.KanbanCardAmbiguous, match="alpha, beta"):
        kb.board_of_task(tid)


def test_board_of_task_ambiguous_live_plus_archived_marks_the_archived(home):
    kb.create_board("alpha")
    kb.create_board("old")
    tid = _card("alpha")
    _copy_card(tid, "alpha", "old")
    kb.write_board_metadata("old", archived=True)
    with pytest.raises(kb.KanbanCardAmbiguous, match=r"old \(archived\)"):
        kb.board_of_task(tid)


def test_board_of_task_fails_closed_on_an_unreadable_board(home):
    kb.create_board("alpha")
    tid = _card("alpha")
    kb.create_board("broken")
    (kb.board_dir("broken") / "kanban.db").write_bytes(b"not a sqlite file" * 64)
    with pytest.raises(kb.KanbanBoardUnreadable, match="board broken is unreadable"):
        kb.board_of_task(tid)


def test_board_of_task_board_without_db_is_no_hit(home):
    kb.create_board("alpha")
    tid = _card("alpha")
    kb.create_board("empty")
    (kb.board_dir("empty") / "kanban.db").unlink()  # board.json, no kanban.db
    assert (kb.board_dir("empty") / "board.json").exists()
    assert kb.board_of_task(tid) == "alpha"


def test_board_of_task_probes_the_layout_not_the_pin(home, monkeypatch):
    """Under a pin to board A, a card on board B resolves to B (never AMBIGUOUS)."""
    kb.create_board("alpha")
    kb.create_board("beta")
    _card("alpha")
    tid = _card("beta")
    _pin(monkeypatch, kb.board_dir("alpha") / "kanban.db")
    assert kb.board_of_task(tid) == "beta"


# --- D2: the surface rule ----------------------------------------------------------------------

def test_bare_non_derivable_verb_is_refused(home):
    with pytest.raises(kbsel.BoardRequired, match="create needs a board.*--parent <card>"):
        kbsel.resolve(_ns(kanban_action="create", board=None, parent=[]))


def test_card_verb_derives_its_board_whatever_the_env_says(home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    tid = _card("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")
    assert kbsel.resolve(_ns(kanban_action="comment", board=None, task_id=tid)) == "beta"


def test_create_derives_from_parent(home):
    kb.create_board("beta")
    parent = _card("beta")
    assert kbsel.resolve(_ns(kanban_action="create", board=None, parent=[parent])) == "beta"


def test_explicit_board_contradicting_the_pin_is_refused(home, monkeypatch):
    """I2 / D2 step 1: before S7 the CLI silently followed the pin."""
    kb.create_board("subs-ace")
    _card("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    with pytest.raises(kb.KanbanPinDivergenceError, match="--board default contradicts"):
        kbsel.resolve(_ns(kanban_action="diagnostics", board="default"))
    assert kbsel.resolve(_ns(kanban_action="diagnostics", board="subs-ace")) == "subs-ace"


def test_kill_switch_default_relaxes_only_the_bare_refusal(home, monkeypatch):
    (home / "config.yaml").write_text("kanban:\n  bare_verb: default\n", encoding="utf-8")
    assert kbsel.bare_verb_policy() == "default"
    assert kbsel.resolve(_ns(kanban_action="list", board=None)) == kb.DEFAULT_BOARD
    kb.create_board("subs-ace")
    _card("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    with pytest.raises(kb.KanbanPinDivergenceError):
        kbsel.resolve(_ns(kanban_action="list", board="default"))


def test_kill_switch_is_root_only(home, monkeypatch):
    prof = home / "profiles" / "p1"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("kanban:\n  bare_verb: default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert kbsel.bare_verb_policy() == "refuse"


def test_derived_board_is_the_context_board_for_the_whole_verb(home, monkeypatch, tmp_path):
    """RC-1: a verb derived onto board B runs scoped to B, so library callers (worktree anchor,
    hooks, journal) read B's metadata, not ``default``'s. Mutation: drop the scope -> ``default``."""
    from hermes_cli import kanban
    from hermes_cli import kanban_worktree_port as kwp

    kb.create_board("beta", default_workdir=str(tmp_path))
    parent = _card("beta")
    seen = {}

    def spy(args):
        seen["ambient"] = kb.get_current_board()
        seen["workdir"] = kwp.board_default_workdir(None)
        return 0

    monkeypatch.setitem(kanban._HANDLERS, "create", spy)
    rc = kanban.kanban_command(_ns(kanban_action="create", board=None, parent=[parent]))
    assert rc == 0
    assert seen == {"ambient": "beta", "workdir": str(tmp_path)}


# --- tools surface ----------------------------------------------------------------------------

def test_tool_board_scopes_the_resolved_board(home):
    from tools import kanban_tools

    kb.create_board("beta")
    tid = _card("beta")
    with kanban_tools._board(None, cards=[tid], verb="kanban_show") as (_kb, conn):
        assert kb.get_current_board() == "beta"
        assert kb.get_task(conn, tid) is not None
    with pytest.raises(kbsel.BoardRequired):
        with kanban_tools._board(None, verb="kanban_list"):
            pass


# --- identity ---------------------------------------------------------------------------------

def test_worker_chat_identity_derives_the_board_from_the_pin(home):
    from hermes_cli.kanban_identity import resolve_kanban_worker_chat_identity

    kb.create_board("subs-ace")
    env = {"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_DB": str(kb.board_dir("subs-ace") / "kanban.db"),
           "HERMES_KANBAN_BOARD": "lies"}
    assert resolve_kanban_worker_chat_identity(env) == ("t_1", "kanban / subs-ace / t_1")


# --- D3 cost (RC-2 / RC-11): measured, not asserted against a CI box ---------------------------

def test_board_of_task_cost_is_recorded(home, capsys):
    """RC-2: p95 at 21 boards under 250 ms (Studio, 65 boards, measured on the card)."""
    for i in range(20):
        kb.create_board(f"b{i:02d}")
        _card(f"b{i:02d}")
    tid = _card("b19")
    samples = []
    for _ in range(20):
        t0 = time.perf_counter()
        assert kb.board_of_task(tid) == "b19"
        samples.append(time.perf_counter() - t0)
    samples.sort()
    p95_ms = samples[int(0.95 * (len(samples) - 1))] * 1000
    print(f"board_of_task p95 over 21 boards: {p95_ms:.1f} ms (threshold 250 ms)")
    assert p95_ms < 250
