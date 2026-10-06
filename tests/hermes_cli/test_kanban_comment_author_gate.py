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


# ── Prism 41fd439722e6 (#1782 post-merge): the operator-profile exemption was
# keyed on the caller-selected HERMES_PROFILE. A worker shell could run
# ``env -u HERMES_KANBAN_TASK HERMES_PROFILE=default ... --author human:apollo``.
# The exemption now holds only when the process is not running under a
# dispatched worker, read from process ancestry (task_runs.worker_pid + its
# spawn fingerprint), which the environment cannot rewrite.


def _seed_worker_run(tid, pid, profile="daedalus", fingerprint=None):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    fp = _process_fingerprint(pid) if fingerprint is None else fingerprint
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, worker_pid, "
            "worker_started_at, started_at) VALUES (?, ?, 'running', ?, ?, 0)",
            (tid, profile, pid, fp),
        )
        conn.commit()


def test_worker_selecting_default_profile_cannot_forge_author(board, monkeypatch, capsys):
    """RED on aaac711c6: the forged label was stored as the comment author."""
    import os

    _seed_worker_run(board, os.getpid())
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo")
    assert author == "daedalus"
    assert payload.get("claimed_author") == "human:apollo"
    assert "claimed_author" in capsys.readouterr().err


def test_worker_selecting_default_profile_writes_as_itself_without_author(board, monkeypatch):
    """The bare default (no --author) is the same env-derived name: it must not
    read ``default`` (a RULING_AUTHORS label) inside a worker either."""
    import os

    _seed_worker_run(board, os.getpid())
    monkeypatch.setenv("HERMES_PROFILE", "default")
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    assert kc.kanban_command(top.parse_args(["kanban", "comment", board, "APOLLO RULED x"])) == 0
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (board,)
        ).fetchone()
    assert row["author"] == "daedalus"


def test_worker_with_operator_token_still_honoured(board, monkeypatch):
    import os

    _seed_worker_run(board, os.getpid())
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo", TOKEN)
    assert author == "human:apollo"
    assert "claimed_author" not in payload


@pytest.mark.parametrize("fingerprint", ["unverified", "0|1", ""])
def test_unproven_worker_row_is_not_a_worker(board, monkeypatch, fingerprint):
    """A recycled pid (fingerprint mismatch), an unverified spawn or a legacy
    row never re-labels the operator: the gate only acts on a proven worker."""
    import os

    _seed_worker_run(board, os.getpid(), fingerprint=fingerprint or None)
    if not fingerprint:
        with kb.connect() as conn:
            conn.execute("UPDATE task_runs SET worker_started_at=NULL")
            conn.commit()
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "land-autopilot"
    assert "claimed_author" not in payload


def test_agent_tool_identity_uses_worker_ancestry(board, monkeypatch):
    """Same class on the tool path: _persisted_identity is env-derived too."""
    import os

    from tools import kanban_tools as kt

    _seed_worker_run(board, os.getpid())
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kt._persisted_identity() == "daedalus"
    monkeypatch.setenv("HERMES_PROFILE", "argus")
    assert kt._persisted_identity() == "argus"   # non-operator names are not rewritten


def test_triage_sweep_author_is_gated(board, monkeypatch):
    """``specify``/``decompose --author`` writes the audit comment author: same gate."""
    seen = {}

    class _Mod:
        @staticmethod
        def list_triage_ids(tenant=None):
            return []

    def run_one(tid, author):
        seen["author"] = author
        return type("O", (), {"ok": True, "task_id": tid})()

    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    args = argparse.Namespace(all_triage=False, author="human:apollo", json=False,
                              tenant=None, task_id=board)
    kc._run_triage_sweep(args, "specify", _Mod, run_one, "specified", (), lambda o: "ok")
    assert seen["author"] == "daedalus"
