"""connect()/init_db() refuse ``kanban/boards/default/kanban.db`` (card t_1462ab0d).

2026-10-02 09:02:00 a cron agent ran ``kb.connect(<root>/kanban/boards/default/
kanban.db)``; connect() created the dir and a full-schema, zero-card phantom
board there. The default board's DB is ``<root>/kanban.db``.
"""
from __future__ import annotations

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
