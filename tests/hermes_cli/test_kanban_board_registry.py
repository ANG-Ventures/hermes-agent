"""S7: ``pinned_board()``, ``board_of_task()``, the ``locate`` verb, and the surface resolver.

There is no current-board file and no board env var; ``kanban.bare_verb`` (root config.yaml,
absent == ``refuse``) is the kill switch for step 5 only. Spec: hermes-home
plans/2026-10-21_sdlc-simplification/S7-boards-no-global-pointer-SPEC.md (D1, D2, D3, Q2, RC-7,
RC-11).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from hermes_cli import kanban
from hermes_cli import kanban_board_select as kbsel
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_board_registry as reg
from hermes_cli import kanban_db_connect as kbc

pytestmark = pytest.mark.kanban_bare_verb_refuse


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
    reg._PINNED_BOARD_ERRORS.clear()
    return h


def _policy(home: Path, value: str) -> None:
    (home / "config.yaml").write_text(f"kanban:\n  bare_verb: {value}\n", encoding="utf-8")


def _pin(monkeypatch, path: Path) -> None:
    """A dispatched worker's DB pin. The sandbox flag would neutralise it, so drop that."""
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — the pin under test is what the sandbox neutralises; HERMES_KANBAN_DB points at a tmp_path DB
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))


def _card(board: str, title: str = "t") -> str:
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, title=title, assignee="dev")


def _copy_card(tid: str, src: str, dst: str) -> None:
    with kbc.connect_closing(board=src) as s, kbc.connect_closing(board=dst) as d:
        row = s.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        cols = row.keys()
        d.execute(f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  tuple(row))
        d.commit()


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_registry_imports_first_in_a_fresh_interpreter():
    """The registry and the facade import each other; either may be imported first."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", "from hermes_cli import kanban_db_board_registry as r; "
                               "print(r.PINNED_SLUG)"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "override"


# --- the kill switch: refuse by default; nothing reads a file or an env slug -------------------

def test_policy_absent_is_refuse(home):
    assert kbsel.bare_verb_policy() == kbsel.BARE_VERB_REFUSE


@pytest.mark.parametrize("value", ["refuse", "default"])
def test_file_and_env_are_inert(home, monkeypatch, value):
    """Restore the file branch or the env branch in ``get_current_board`` -> RED."""
    kb.create_board("subs-ace")
    (home / "kanban").mkdir(exist_ok=True)
    (home / "kanban" / "current").write_text("subs-ace\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "subs-ace")
    _policy(home, value)
    assert kbsel.bare_verb_policy() == value
    assert kb.get_current_board() == kb.DEFAULT_BOARD
    with kb.scoped_current_board("subs-ace"):
        assert kb.get_current_board() == "subs-ace"


def test_unknown_policy_value_is_refuse(home):
    _policy(home, "maybe")
    assert kbsel.bare_verb_policy() == kbsel.BARE_VERB_REFUSE


def test_policy_is_root_only(home, monkeypatch):
    prof = home / "profiles" / "p1"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("kanban:\n  bare_verb: default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert kbsel.bare_verb_policy() == kbsel.BARE_VERB_REFUSE


# --- D1: pinned_board ---------------------------------------------------------------------------

def test_no_pin_is_none(home):
    assert kb.pinned_board() is None


def test_pin_names_its_board(home, monkeypatch):
    kb.create_board("subs-ace")
    _card("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    assert kb.pinned_board() == "subs-ace"
    _policy(home, "refuse")
    assert kb.get_current_board() == "subs-ace"


def test_pin_inverse_is_spelling_blind(home, monkeypatch):
    """RC-7: the same file spelled another way (symlinked parent) names the same board.
    Mutation: compare by string -> ``override``."""
    kb.create_board("subs-ace")
    _card("subs-ace")
    alias = home.parent / "alias-root"
    alias.symlink_to(home, target_is_directory=True)
    _pin(monkeypatch, alias / "kanban" / "boards" / "subs-ace" / "kanban.db")
    assert kb.pinned_board() == "subs-ace"


def test_layout_external_pin_is_override_and_routes_from_the_pin(home, monkeypatch, tmp_path):
    """RC-7: a pin outside every board's layout names ``override``; the verb runs off the pin."""
    db = tmp_path / "elsewhere" / "custom.db"
    db.parent.mkdir()
    conn = kbc.connect(db_path=db)
    tid = kb.create_task(conn, title="x", assignee="dev")
    conn.close()
    _pin(monkeypatch, db)
    _policy(home, "refuse")
    assert kb.pinned_board() == kb.PINNED_SLUG
    assert kb.get_current_board() == kb.DEFAULT_BOARD  # layout callers keep default's paths
    assert kbsel.resolve(_ns(kanban_action="show", board=None, task_id=tid)) == kb.PINNED_SLUG
    with kbc.connect_closing() as c:  # the pin is the DB; D3 never consulted
        assert kb.get_task(c, tid) is not None


def test_pinned_board_is_total_and_the_surface_refuses_with_the_next_command(home, monkeypatch):
    """RC-11: an error naming the pin -> ``override`` (never a raise), and the surface refuses
    the verb with the one next command instead of acting under an unnamed pin."""
    kb.create_board("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    _policy(home, "refuse")

    def boom():
        raise PermissionError("boards/ unreadable")

    monkeypatch.setattr(reg, "board_db_layout", boom)
    assert kb.pinned_board() == kb.PINNED_SLUG
    assert kb.pinned_board_error() == "PermissionError: boards/ unreadable"
    assert kb.get_current_board() == kb.DEFAULT_BOARD
    with pytest.raises(kbsel.BoardRequired,
                       match=r"could not name the board HERMES_KANBAN_DB=.*pass --board <the slug"):
        kbsel.resolve(_ns(kanban_action="list", board=None))
    rc = kanban.kanban_command(_ns(kanban_action="list", board=None))
    assert rc == 2


def test_pinned_board_error_clears_once_the_pin_is_named(home, monkeypatch):
    kb.create_board("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    real = reg.board_db_layout
    monkeypatch.setattr(reg, "board_db_layout", lambda: (_ for _ in ()).throw(OSError("x")))
    assert kb.pinned_board() == kb.PINNED_SLUG
    monkeypatch.setattr(reg, "board_db_layout", real)
    assert kb.pinned_board() == "subs-ace"
    assert kb.pinned_board_error() is None


def test_retargeted_symlink_pin_changes_the_answer_in_process(home, monkeypatch, tmp_path):
    """Prism 69c969208e42 (C1a #2202): a symlink pin retargeted from board a's DB to board b's
    names b in the same process. Mutation: memoise on the raw pin string -> the second answer
    stays ``alpha``."""
    kb.create_board("alpha")
    kb.create_board("beta")
    tid_a, tid_b = _card("alpha"), _card("beta")
    link = tmp_path / "pin.db"
    link.symlink_to(kb.board_dir("alpha") / "kanban.db")
    _pin(monkeypatch, link)
    assert reg.locate(tid_a)[0] == "alpha"
    link.unlink()
    link.symlink_to(kb.board_dir("beta") / "kanban.db")
    assert reg.locate(tid_b)[0] == "beta"
    assert kb.pinned_board() == "beta"


def test_pinned_board_rescans_on_every_call(home, monkeypatch):
    """Prism fde771bf9eaa: naming the pin is about which file it names NOW, so it is never memoised; a
    second call rescans the layout (one iterdir + one stat a board)."""
    kb.create_board("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    calls = []
    real = reg.board_db_layout
    monkeypatch.setattr(reg, "board_db_layout", lambda: calls.append(1) or real())
    assert kb.pinned_board() == "subs-ace"
    assert kb.pinned_board() == "subs-ace"
    assert len(calls) == 2


# --- D3: board_of_task never guesses -----------------------------------------------------------

def test_board_of_task_finds_the_one_board(home):
    kb.create_board("alpha")
    tid = _card("alpha")
    assert kb.board_of_task(tid) == "alpha"


def test_board_of_task_not_found(home):
    kb.create_board("alpha")
    with pytest.raises(kb.KanbanCardNotFound, match="is on no board"):
        kb.board_of_task("t_deadbeef")


def test_board_of_task_ambiguous_on_two_live_boards(home):
    """Mutation: ``return hits[0]`` -> no raise."""
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
    """Mutation: treat unreadable as no-hit -> returns ``alpha``."""
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
    """Under a pin to board A, a card on board B resolves to B. Mutation: probe via
    ``kanban_db_path(board=slug)`` -> every board is the pin -> wrong answer."""
    kb.create_board("alpha")
    kb.create_board("beta")
    _card("alpha")
    tid = _card("beta")
    _pin(monkeypatch, kb.board_dir("alpha") / "kanban.db")
    assert kb.board_of_task(tid) == "beta"


def test_board_of_task_writes_nothing(home):
    """The probe is read-only: a board dir with board.json and no DB stays DB-less."""
    kb.create_board("alpha")
    tid = _card("alpha")
    kb.create_board("empty")
    db = kb.board_dir("empty") / "kanban.db"
    db.unlink()
    kb.board_of_task(tid)
    assert not db.exists()


# --- locate (Q2): the contract hermes-home's kanban_board_resolve.py wraps --------------------

def _locate(tid: str) -> int:
    return kanban.kanban_command(_ns(kanban_action="locate", board=None, task_id=tid))


def test_locate_contract_hit(home, capsys):
    kb.create_board("beta")
    tid = _card("beta")
    assert _locate(tid) == 0
    out = capsys.readouterr().out
    assert out == f"beta\t{home / 'kanban' / 'boards' / 'beta' / 'kanban.db'}\n"


def test_locate_default_board(home, capsys):
    kb.init_db()
    tid = _card("default")
    assert _locate(tid) == 0
    assert capsys.readouterr().out == f"default\t{home / 'kanban.db'}\n"


@pytest.mark.parametrize("case,code", [("missing", "CARD_NOT_FOUND"), ("twice", "CARD_AMBIGUOUS"),
                                       ("broken", "BOARD_UNREADABLE")])
def test_locate_contract_refusals(home, capsys, case, code):
    kb.create_board("alpha")
    kb.create_board("beta")
    tid = _card("alpha")
    if case == "twice":
        _copy_card(tid, "alpha", "beta")
    if case == "broken":
        kb.create_board("broken")
        (kb.board_dir("broken") / "kanban.db").write_bytes(b"junk" * 256)
    assert _locate("t_00000000" if case == "missing" else tid) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{code}: kanban: ")
    assert captured.err.count("\n") == 1


def test_locate_under_a_pin_prints_the_pins_board_and_the_pin(home, monkeypatch, capsys, tmp_path):
    db = tmp_path / "x" / "custom.db"
    db.parent.mkdir()
    kbc.connect(db_path=db).close()
    _pin(monkeypatch, db)
    assert _locate("t_any") == 0
    assert capsys.readouterr().out == f"{kb.PINNED_SLUG}\t{db}\n"


def test_locate_ignores_a_stale_pointer_file(home, capsys):
    """``locate`` answers from the cards; a leftover ``kanban/current`` is never read."""
    kb.create_board("alpha")
    kb.create_board("beta")
    tid = _card("beta")
    (home / "kanban" / "current").write_text("alpha\n", encoding="utf-8")
    assert _locate(tid) == 0
    assert capsys.readouterr().out.startswith("beta\t")


def test_locate_parses_from_argv(home, capsys):
    from hermes_cli.kanban_parser import build_parser

    kb.create_board("beta")
    tid = _card("beta")
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["kanban", "locate", tid])
    assert kanban.kanban_command(args) == 0
    assert capsys.readouterr().out.startswith("beta\t")


# --- D2 behind the flag ------------------------------------------------------------------------

def test_flag_on_bare_non_derivable_verb_is_refused_with_the_next_command(home, capsys):
    _policy(home, "refuse")
    with pytest.raises(kbsel.BoardRequired, match=r"create needs a board.*or --parent <card>\.$"):
        kbsel.resolve(_ns(kanban_action="create", board=None, parent=[]))
    assert kanban.kanban_command(_ns(kanban_action="list", board=None)) == 2
    assert "list needs a board: pass --board <slug>" in capsys.readouterr().err


def test_flag_on_card_verb_derives_its_board_whatever_the_env_says(home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    tid = _card("beta")
    _policy(home, "refuse")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")
    assert kbsel.resolve(_ns(kanban_action="comment", board=None, task_id=tid)) == "beta"


def test_flag_on_create_derives_from_parent(home):
    kb.create_board("beta")
    parent = _card("beta")
    _policy(home, "refuse")
    assert kbsel.resolve(_ns(kanban_action="create", board=None, parent=[parent])) == "beta"


def test_flag_on_explicit_board_contradicting_the_pin_is_refused(home, monkeypatch):
    kb.create_board("subs-ace")
    _card("subs-ace")
    _pin(monkeypatch, kb.board_dir("subs-ace") / "kanban.db")
    _policy(home, "refuse")
    with pytest.raises(kb.KanbanPinDivergenceError, match="--board default contradicts"):
        kbsel.resolve(_ns(kanban_action="diagnostics", board="default"))
    assert kbsel.resolve(_ns(kanban_action="diagnostics", board="subs-ace")) == "subs-ace"


def test_flag_default_relaxes_only_the_bare_refusal(home):
    _policy(home, "default")
    assert kbsel.resolve(_ns(kanban_action="list", board=None)) == kb.DEFAULT_BOARD


def test_flag_on_derived_board_is_the_context_board_for_the_whole_verb(home, monkeypatch):
    kb.create_board("beta")
    parent = _card("beta")
    _policy(home, "refuse")
    seen = {}

    def spy(args):
        seen["ambient"] = kb.get_current_board()
        return 0

    monkeypatch.setitem(kanban._HANDLERS, "create", spy)
    assert kanban.kanban_command(_ns(kanban_action="create", board=None, parent=[parent])) == 0
    assert seen == {"ambient": "beta"}


# --- cost (D3 / RC-11): recorded, threshold 250 ms ---------------------------------------------

def test_board_of_task_opens_each_board_once(home, monkeypatch):
    """The cost contract: one read-only open + one SELECT per board, nothing else."""
    for i in range(5):
        kb.create_board(f"b{i}")
    tid = _card("b4")
    opened = []
    real = reg.sqlite3.connect

    def spy(database, *a, **k):
        opened.append(database)
        assert database.endswith("?mode=ro") and k.get("uri") is True
        return real(database, *a, **k)

    monkeypatch.setattr(reg.sqlite3, "connect", spy)
    assert kb.board_of_task(tid) == "b4"
    assert len(opened) == len([p for _, p in reg.board_db_layout() if p.exists()])


def test_board_of_task_and_pinned_board_cost_is_recorded(home, monkeypatch, capsys):
    """D3 / RC-11 cost, 250 ms threshold. Asserted on CPU time (process_time): wall time on a
    loaded host measures the host, not the probe. The wall number is printed for the record;
    the live-tree wall p95 is §7 step 5's measurement."""
    for i in range(20):
        kb.create_board(f"b{i:02d}")
        _card(f"b{i:02d}")
    tid = _card("b19")
    cpu, wall = [], []
    for _ in range(20):
        w0, c0 = time.perf_counter(), time.process_time()
        assert kb.board_of_task(tid) == "b19"
        wall.append(time.perf_counter() - w0)
        cpu.append(time.process_time() - c0)
    cpu.sort()
    wall.sort()
    p95 = int(0.95 * (len(cpu) - 1))
    _pin(monkeypatch, kb.board_dir("b10") / "kanban.db")
    c0 = time.process_time()
    assert kb.pinned_board() == "b10"
    pin_cpu_ms = (time.process_time() - c0) * 1000
    with capsys.disabled():
        print(f"\nboard_of_task over 21 boards: cpu p95 {cpu[p95] * 1000:.1f} ms, wall p95 "
              f"{wall[p95] * 1000:.1f} ms; pinned_board cold cpu {pin_cpu_ms:.1f} ms "
              f"(threshold 250 ms)")
    assert cpu[p95] * 1000 < 250
    assert pin_cpu_ms < 250



# --- Prism 39b758962810: the pin sentinel is never a board name -------------------------------

def test_sentinel_is_refused_as_a_board_name(home):
    with pytest.raises(ValueError, match="reserved"):
        kb._normalize_board_slug(kb.PINNED_SLUG)
    with pytest.raises(ValueError, match="reserved"):
        kb.create_board(kb.PINNED_SLUG)
    assert _boards_argv(["boards", "create", kb.PINNED_SLUG]) == 2


def test_pre_existing_override_board_is_not_found_and_never_opened_unscoped(home, capsys):
    """A board dir literally named ``override`` (made before the sentinel was reserved) is
    BoardNotFound-with-reason on an explicit --board, never derived, and a write never lands."""
    kb.init_db()
    legacy = kb.boards_root() / kb.PINNED_SLUG
    legacy.mkdir(parents=True)
    conn = kbc.connect(db_path=legacy / "kanban.db")
    tid = kb.create_task(conn, title="legacy", assignee="dev")
    conn.close()
    (legacy / "board.json").write_text('{"slug": "override"}', encoding="utf-8")
    _policy(home, "refuse")
    with pytest.raises(kbsel.BoardNotFound, match="reserved for a HERMES_KANBAN_DB pin"):
        kbsel.resolve_slug(kb.PINNED_SLUG, verb="create")
    with pytest.raises(kb.KanbanCardNotFound):
        kb.board_of_task(tid)  # never derived onto the sentinel
    assert kb.PINNED_SLUG not in [b["slug"] for b in kb.list_boards()]
    rc = kanban.kanban_command(_ns(kanban_action="create", board=kb.PINNED_SLUG, title="w",
                                   parent=[]))
    assert rc == 1
    assert "reserved" in capsys.readouterr().err
    with kbc.connect_closing(board="default") as c:
        assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_reserved_board_dir_never_breaks_discovery_or_tracebacks(home, capsys, caplog):
    """Prism c12f81da4e1f: a pre-existing ``boards/override/`` beside ``alpha`` is skipped by
    discovery with ONE warning line naming the migration; every other board still lists, and an
    explicit ``--board override`` is BoardNotFound (rc 1), never a ValueError traceback."""
    import logging

    kb.init_db()
    kb.create_board("alpha")
    legacy = kb.boards_root() / kb.PINNED_SLUG
    legacy.mkdir(parents=True)
    conn = kbc.connect(db_path=legacy / "kanban.db")
    kb.create_task(conn, title="legacy", assignee="dev")
    conn.close()
    (legacy / "board.json").write_text('{"slug": "override"}', encoding="utf-8")
    kb._RESERVED_BOARD_WARNED.clear()
    caplog.clear()
    _policy(home, "refuse")
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        assert _boards_argv(["boards", "list"]) == 0
        assert _boards_argv(["boards", "list"]) == 0
        assert [s for s, _ in reg.board_db_layout()] == ["default", "alpha"]
    out = capsys.readouterr().out
    assert "alpha" in out and " override " not in out
    warned = [r.getMessage() for r in caplog.records if "reserved" in r.getMessage()]
    assert len(warned) == 1 and str(legacy) in warned[0] and "mv " in warned[0]
    assert _boards_argv(["--board", kb.PINNED_SLUG, "boards", "show"]) == 1
    assert _boards_argv(["--board", kb.PINNED_SLUG, "list"]) == 1
    assert "reserved" in capsys.readouterr().err


# --- Prism 7b28513d644d: boards list|show resolve like every other verb ------------------------

def _boards_argv(argv: list[str]) -> int:
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="cmd"))
    return kanban.kanban_command(parser.parse_args(["kanban", *argv]))


def test_boards_list_malformed_board_is_a_clean_rc2(home, capsys):
    _policy(home, "refuse")
    assert _boards_argv(["boards", "list", "--board", "bad slug"]) == 2
    assert "invalid board slug" in capsys.readouterr().err


def test_boards_show_unknown_board_is_board_not_found(home, capsys):
    _policy(home, "refuse")
    assert _boards_argv(["--board", "typo", "boards", "show"]) == 1
    assert "board 'typo' does not exist" in capsys.readouterr().err


def test_boards_list_and_show_bare_still_work_under_refuse(home, capsys):
    kb.create_board("beta")
    _policy(home, "refuse")
    assert _boards_argv(["boards", "list"]) == 0
    assert "beta" in capsys.readouterr().out
    assert _boards_argv(["boards", "show"]) == 0
    assert "no board pinned" in capsys.readouterr().out
    assert _boards_argv(["--board", "beta", "boards", "show"]) == 0
    assert capsys.readouterr().out.startswith("Board: beta")


def test_boards_ls_and_current_aliases_resolve_the_board(home, capsys):
    """Prism ccd467f64b6a: argparse dispatches ``ls``/``current`` under the literal typed, so the
    aliases must resolve ``--board`` exactly like ``list``/``show``."""
    kb.create_board("alpha")
    _policy(home, "refuse")
    assert _boards_argv(["--board", "alpha", "boards", "current"]) == 0
    assert capsys.readouterr().out.startswith("Board: alpha")
    assert _boards_argv(["--board", "alpha", "boards", "list"]) == 0
    listed = capsys.readouterr().out
    assert _boards_argv(["--board", "alpha", "boards", "ls"]) == 0
    assert capsys.readouterr().out == listed
    assert _boards_argv(["--board", "typo", "boards", "current"]) == 1
    assert "board 'typo' does not exist" in capsys.readouterr().err


def test_boards_list_marks_only_the_resolved_board(home, capsys):
    """Prism ee7333d7b6ff: ``--board alpha boards list`` marks alpha (not the ambient default) as
    current, matching the footer; with no board resolved nothing is marked."""
    import json

    kb.create_board("alpha")
    assert _boards_argv(["--board", "alpha", "boards", "list", "--json"]) == 0
    current = {b["slug"]: b["is_current"] for b in json.loads(capsys.readouterr().out)}
    assert current == {"default": False, "alpha": True}
    assert _boards_argv(["--board", "alpha", "boards", "list"]) == 0
    marked = [ln.split()[1] for ln in capsys.readouterr().out.splitlines() if ln.startswith("●")]
    assert marked == ["alpha"]
    assert _boards_argv(["boards", "list", "--json"]) == 0
    assert not any(b["is_current"] for b in json.loads(capsys.readouterr().out))


# --- resolve_selector: one normalise-then-blank-is-omitted rule (round 4, Prism b68acd4edf81) ---

@pytest.mark.parametrize("raw", [None, "", " ", "\t\n "])
def test_resolve_selector_blank_is_omitted(home, raw):
    assert kbsel.resolve_selector(raw) is None


def test_resolve_selector_normalises_a_real_slug(home):
    assert kbsel.resolve_selector("  Alpha ") == "alpha"


def test_resolve_selector_refuses_malformed_and_the_pin_sentinel(home):
    with pytest.raises(ValueError):
        kbsel.resolve_selector("bad slug")
    with pytest.raises(kbsel.BoardNotFound, match="reserved"):
        kbsel.resolve_selector(" Override ")


def test_blank_board_flag_is_omitted_on_the_cli_path(home):
    """``--board ' '`` is the bare verb: refused under ``refuse`` (rc 2 BoardRequired), never an
    error about an empty slug and never a silent default."""
    with pytest.raises(kbsel.BoardRequired):
        kbsel.resolve(_ns(kanban_action="list", board=" "))
    _policy(home, "default")
    assert kbsel.resolve(_ns(kanban_action="list", board=" ")) == kb.DEFAULT_BOARD
