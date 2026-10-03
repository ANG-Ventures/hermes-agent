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


# Prism P1 f836c7bad244 (card t_a9a00731): resolving BEFORE the shape check lost
# the refusal when a structural ancestor (``kanban/`` or ``boards/``) is itself a
# symlink -- the resolved target no longer carries the ``kanban/boards/default``
# names. The guard must refuse the logical spelling AND the resolved target of
# this home's ``board_dir("default")/kanban.db``.


@pytest.mark.parametrize("linked", ["kanban", "boards"])
def test_symlinked_structural_ancestor_logical_path_refused(home, tmp_path_factory, linked):
    real = tmp_path_factory.mktemp("task-data")
    if linked == "kanban":
        (home / "kanban").symlink_to(real, target_is_directory=True)
    else:
        (home / "kanban").mkdir()
        (home / "kanban" / "boards").symlink_to(real, target_is_directory=True)
    phantom = home / "kanban" / "boards" / "default" / "kanban.db"
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(phantom)
    assert not any(real.rglob("kanban.db"))


@pytest.mark.parametrize("linked", ["kanban", "boards"])
def test_symlinked_structural_ancestor_resolved_target_refused(home, tmp_path_factory, linked):
    real = tmp_path_factory.mktemp("task-data")
    if linked == "kanban":
        (home / "kanban").symlink_to(real, target_is_directory=True)
        target = real / "boards" / "default" / "kanban.db"
    else:
        (home / "kanban").mkdir()
        (home / "kanban" / "boards").symlink_to(real, target_is_directory=True)
        target = real / "default" / "kanban.db"
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(target)
    assert not any(real.rglob("kanban.db"))


def test_symlinked_kanban_dir_named_boards_still_open(home, tmp_path_factory):
    real = tmp_path_factory.mktemp("task-data")
    (home / "kanban").symlink_to(real, target_is_directory=True)
    with kb.connect_closing(board="alpha") as conn:
        conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert (real / "boards" / "alpha" / "kanban.db").is_file()
    with kb.connect_closing() as conn:  # default stays <root>/kanban.db
        conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert (home / "kanban.db").is_file()


def test_symlinked_kanban_under_foreign_root_refused(home, tmp_path_factory):
    # Not this process's kanban_home(): only the logical-spelling view sees it.
    other = tmp_path_factory.mktemp("other-root")
    real = tmp_path_factory.mktemp("task-data")
    (other / "kanban").symlink_to(real, target_is_directory=True)
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(other / "kanban" / "boards" / "default" / "kanban.db")
    assert not any(real.rglob("kanban.db"))


def test_symlinked_parent_into_foreign_boards_default_refused(home, tmp_path_factory):
    # Spelling has no kanban/boards names and the root is not kanban_home():
    # only the resolved-target shape view sees it (Prism d0894d0cbe77 class).
    meta = tmp_path_factory.mktemp("other-root") / "kanban" / "boards" / "default"
    meta.mkdir(parents=True)
    link = tmp_path_factory.mktemp("links") / "elsewhere"
    link.symlink_to(meta, target_is_directory=True)
    for fn in (kb.connect, kb.init_db):
        with pytest.raises(kb.KanbanNonCanonicalBoardPathError):
            fn(link / "kanban.db")
    assert not (meta / "kanban.db").exists()
