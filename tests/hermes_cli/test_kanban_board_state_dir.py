"""Dispatcher latches for the ``default`` board must not mint ``kanban/boards/default/``.

The default board's DB is ``<root>/kanban.db``. Writing its rate-limit circuit
marker into ``boards/default/`` created that dir (2026-09-24), and a later
create-mode ``sqlite3.connect`` on ``boards/default/kanban.db`` left a 0-byte
phantom board DB that failed enumerators closed for 24h (2026-09-29 18:31).
Without the dir, that connect raises instead of creating a file.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_budget as kbudget
from hermes_cli import kanban_db as kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_SANDBOX", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbudget, "_notify_script_path", lambda home=None: None)
    return tmp_path


def test_default_board_state_dir_is_outside_boards(home):
    assert kb.board_state_dir(None) == home / "kanban"
    assert kb.board_state_dir("default") == home / "kanban"
    assert kb.board_state_dir("alpha") == kb.board_dir("alpha")


def test_default_circuit_latch_does_not_mint_boards_default(home):
    kb._notify_rate_limit_circuit(None, "claude-bpr", until=2_000_000_000, trip=5, now=1_000)
    marker = home / "kanban" / kb._RATE_LIMIT_CIRCUIT_MARKER
    assert json.loads(marker.read_text()) == {"claude-bpr": 2_000_000_000}
    assert not (home / "kanban" / "boards" / "default").exists()
    # The phantom's creator shape now fails instead of leaving a 0-byte DB.
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(home / "kanban" / "boards" / "default" / "kanban.db")
    assert not (home / "kanban" / "boards" / "default" / "kanban.db").exists()


def test_named_board_circuit_latch_stays_in_board_dir(home):
    kb._notify_rate_limit_circuit("alpha", "claude-bpr", until=2_000_000_000, trip=5, now=1_000)
    assert (kb.board_dir("alpha") / kb._RATE_LIMIT_CIRCUIT_MARKER).is_file()
    assert not (home / "kanban" / kb._RATE_LIMIT_CIRCUIT_MARKER).exists()
