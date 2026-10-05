"""``kanban comment --author`` cannot forge an operator author (Prism gate spec §8b).

Before: ``env -u HERMES_KANBAN_TASK hermes kanban comment --author human:apollo``
from a worker shell stored ``human:apollo`` as the comment author, and every
operator-trust reader (``_ruled_since_block``, the merge pass's changes_hold)
reads ``task_comments.author``. Now a non-operator caller without the operator
token writes as itself and the requested label is kept as ``claimed_author``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

TOKEN = "t0ken-for-tests"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_TASK", "HERMES_PROFILE_NAME", kb.OPERATOR_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    (home / "kanban").mkdir(exist_ok=True)
    kb.operator_token_path().write_text(TOKEN + "\n", encoding="utf-8")
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    return tid


def _comment_as(monkeypatch, profile, tid, author, token=None):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if token is None:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, token)
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    args = top.parse_args(["kanban", "comment", tid, "--author", author, "forged ruling"])
    assert kc.kanban_command(args) == 0
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented' "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
    return row["author"], json.loads(ev["payload"])


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_worker_cannot_forge_operator_author(board, monkeypatch, capsys, token):
    author, payload = _comment_as(monkeypatch, "daedalus", board, "human:apollo", token)
    assert author == "daedalus"
    assert payload.get("claimed_author") == "human:apollo"
    assert "claimed_author" in capsys.readouterr().err


def test_operator_token_honours_author(board, monkeypatch):
    author, payload = _comment_as(monkeypatch, "daedalus", board, "human:apollo", TOKEN)
    assert author == "human:apollo"
    assert "claimed_author" not in payload


def test_operator_profile_keeps_its_service_label(board, monkeypatch):
    """Fleet crons run as the operator profile and pick NON-operator labels on
    purpose (land-autopilot, themis): those must not collapse to ``default``."""
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "land-autopilot"
    assert "claimed_author" not in payload
