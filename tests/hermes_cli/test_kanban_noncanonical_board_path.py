"""connect()/init_db() refuse ``kanban/boards/default/kanban.db`` (card t_1462ab0d).

2026-10-02 09:02:00 a cron agent ran ``kb.connect(<root>/kanban/boards/default/
kanban.db)``; connect() created the dir and a full-schema, zero-card phantom
board there. The default board's DB is ``<root>/kanban.db``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_SANDBOX", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_connect_refuses_boards_default_and_creates_nothing(home):
    phantom = home / "kanban" / "boards" / "default" / "kanban.db"
    with pytest.raises(kb.KanbanNonCanonicalBoardPathError) as exc:
        kb.connect(phantom)
    assert str(phantom) in str(exc.value)
    assert not (home / "kanban" / "boards" / "default").exists()


def test_init_db_refuses_boards_default(home):
    phantom = home / "kanban" / "boards" / "default" / "kanban.db"
    with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
        kb.init_db(phantom)
    assert not (home / "kanban" / "boards" / "default").exists()


def test_canonical_paths_still_open(home):
    with kb.connect_closing() as conn:  # default board -> <root>/kanban.db
        conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert (home / "kanban.db").is_file()
    with kb.connect_closing(board="alpha") as conn:
        conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert (home / "kanban" / "boards" / "alpha" / "kanban.db").is_file()
    assert not (home / "kanban" / "boards" / "default").exists()


# Prism P1 d0894d0cbe77 (card t_d5ac4145): the guard must judge the resolved
# filesystem target, not the spelling. Relative, ``..`` and symlinked paths all
# reached ``boards/default/kanban.db`` past the literal component check.


def test_relative_path_from_boards_default_cwd_refused(home, monkeypatch):
    meta = home / "kanban" / "boards" / "default"
    meta.mkdir(parents=True)  # legitimate metadata dir (board_dir("default"))
    monkeypatch.chdir(meta)
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(Path("kanban.db"))
    assert not (meta / "kanban.db").exists()


def test_dotdot_path_refused(home):
    sneaky = home / "kanban" / "boards" / "default" / ".." / "default" / "kanban.db"
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(sneaky)
    assert not (home / "kanban" / "boards" / "default").exists()


def test_symlinked_dir_into_boards_default_refused(home):
    meta = home / "kanban" / "boards" / "default"
    meta.mkdir(parents=True)
    link = home / "elsewhere"
    link.symlink_to(meta, target_is_directory=True)
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(link / "kanban.db")
    assert not (meta / "kanban.db").exists()


def test_dotdot_that_leaves_boards_default_still_opens(home):
    # Spelled with boards/default but resolves to a named board: allowed.
    ok = home / "kanban" / "boards" / "default" / ".." / "alpha" / "kanban.db"
    with kb.connect_closing(ok) as conn:
        conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert (home / "kanban" / "boards" / "alpha" / "kanban.db").is_file()
