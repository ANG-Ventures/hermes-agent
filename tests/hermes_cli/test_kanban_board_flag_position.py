"""``--board`` after the verb (``kanban create "T" --board X``) routes exactly like
``kanban --board X create "T"``; conflicting positions are refused (t_268d2c98)."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_parser


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    # Hermetic: no ambient kanban/session/cron identity and no tty, so the run is the same
    # in an agent shell, CI and off-box. Creates pass --unhomed (the home guard's explicit
    # opt-out) because the board under test, not the home session, is what is asserted.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "HERMES_SESSION", "HERMES_CRON"))}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(_WORKTREE))
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args],
                          env=env, capture_output=True, text=True, cwd=str(_WORKTREE),
                          stdin=subprocess.DEVNULL, timeout=60)


def _titles(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[0] for r in conn.execute("SELECT title FROM tasks")}


@pytest.fixture
def home(tmp_path):
    assert _cli(tmp_path, "boards", "create", "beta").returncode == 0
    return tmp_path


def test_verb_and_top_level_positions_hit_the_same_board(home):
    after = _cli(home, "create", "after-verb", "--board", "beta", "--unhomed")
    before = _cli(home, "--board", "beta", "create", "before-verb", "--unhomed")
    assert after.returncode == 0, after.stderr
    assert before.returncode == 0, before.stderr
    beta_db = home / "kanban" / "boards" / "beta" / "kanban.db"
    assert {"after-verb", "before-verb"} <= _titles(beta_db)
    default_db = home / "kanban.db"
    assert not default_db.exists() or not ({"after-verb", "before-verb"} & _titles(default_db))


def test_verb_position_unknown_board_hits_existence_guard(home):
    r = _cli(home, "create", "T", "--board", "nonexistent", "--unhomed")
    assert r.returncode != 0
    assert "does not exist. Create it with" in r.stderr


def test_conflicting_positions_refused_equal_positions_accepted(home):
    clash = _cli(home, "--board", "default", "create", "T", "--board", "beta", "--unhomed")
    assert clash.returncode != 0
    assert "--board given twice" in clash.stderr
    same = _cli(home, "--board", "beta", "create", "same", "--board", "BETA", "--unhomed")
    assert same.returncode == 0, same.stderr
    assert "same" in _titles(home / "kanban" / "boards" / "beta" / "kanban.db")


def test_boards_show_ignores_verb_position_override(home):
    r = _cli(home, "boards", "show", "--board", "beta")
    assert r.returncode == 0, r.stderr
    assert "Current board: default" in r.stdout


def _leaves(action: argparse._SubParsersAction, path=()):
    seen: set[int] = set()
    for name, p in action.choices.items():
        if id(p) in seen:
            continue
        seen.add(id(p))
        kids = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if kids:
            for kid in kids:
                yield from _leaves(kid, path + (name,))
        else:
            yield path + (name,), p


def test_every_board_scoped_verb_accepts_verb_position_board():
    root = argparse.ArgumentParser()
    k = kanban_parser.build_parser(root.add_subparsers())
    sub = next(a for a in k._actions if isinstance(a, argparse._SubParsersAction))
    missing = [
        " ".join(path) for path, p in _leaves(sub)
        if path[0] not in kanban_parser.VERB_BOARD_EXCLUDED
        and not any("--board" in a.option_strings and a.dest == "verb_board" for a in p._actions)
    ]
    assert missing == []
    assert "--board" in sub.choices["create"].format_help()
