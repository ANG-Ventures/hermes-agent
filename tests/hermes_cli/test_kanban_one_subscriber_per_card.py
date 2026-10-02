"""One subscriber chat per card (Ace ruling 2026-10-02, t_484a3c72).

t_311b6b1f (home #cc-native) held three Discord subscriptions because linking
and mentioning cards across sessions subscribed each chat, so one needs_input
block posted in #prism too. These tests drive the real DB writers against a
real sandboxed state.db for session liveness.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

OWNER = "1553876718639390760"   # card's home chat (#cc-native)
OTHER = "1552754415428177980"   # a second session's chat (#prism)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb._CHAT_LIVENESS_CACHE.clear() if hasattr(kb, "_CHAT_LIVENESS_CACHE") else None
    kb.init_db()
    db = sqlite3.connect(home / "state.db")
    db.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_key TEXT,"
        " chat_id TEXT, thread_id TEXT, started_at REAL, last_activity_at REAL,"
        " effective_last_active REAL)"
    )
    db.commit()
    db.close()
    return home


def _session(home: Path, sid: str, chat: str, age_s: float) -> None:
    ts = time.time() - age_s
    db = sqlite3.connect(home / "state.db")
    db.execute(
        "INSERT INTO sessions VALUES (?, 'discord', ?, ?, NULL, ?, ?, ?)",
        (sid, f"agent:main:discord:group:{chat}:u1", chat, ts, ts, ts),
    )
    db.commit()
    db.close()


def _card(conn, home_sid: str = "sess-owner") -> str:
    tid = kb.create_task(conn, title="card", assignee="worker")
    conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (home_sid, tid))
    conn.commit()
    return tid


def _chats(conn, tid):
    return sorted(s["chat_id"] for s in kb.list_notify_subs(conn, tid))


def _sub(conn, tid, chat, **kw):
    return kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=chat,
                             chat_type="group", **kw)


# (1) second-subscribe refusal -------------------------------------------------

def test_second_chat_refused_while_owner_is_live(board, caplog):
    _session(board, "sess-owner", OWNER, age_s=600)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER)
        with caplog.at_level("INFO"):
            outcome = _sub(conn, tid, OTHER)
        assert _chats(conn, tid) == [OWNER]
    assert outcome == "kept"
    assert f"subscription kept: discord:{OWNER} owns {tid}" in caplog.text


def test_dead_owner_is_rehomed_not_added(board):
    _session(board, "sess-owner", OWNER, age_s=3 * 86400)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn, home_sid="sess-gone")  # home unknown to state.db
        _sub(conn, tid, OWNER)
        outcome = _sub(conn, tid, OTHER)
        assert _chats(conn, tid) == [OTHER]
    assert outcome == "rehomed"


def test_also_adds_second_chat_on_purpose(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER)
        assert _sub(conn, tid, OTHER, also=True) == "added"
        assert _chats(conn, tid) == sorted([OWNER, OTHER])


def test_home_chat_replaces_a_live_foreign_subscriber(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn, home_sid="sess-owner")
        _sub(conn, tid, OTHER, also=True)
        assert _sub(conn, tid, OWNER) == "rehomed"
        assert _chats(conn, tid) == [OWNER]


def test_link_does_not_inherit_a_second_chat(board):
    """The t_311b6b1f shape: #prism links a #cc-native card under its own."""
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        child = _card(conn, home_sid="sess-owner")
        _sub(conn, child, OWNER)
        parent = _card(conn, home_sid="sess-other")
        _sub(conn, parent, OTHER)
        kb.link_tasks(conn, parent, child)
        assert _chats(conn, child) == [OWNER]


def test_cli_notify_subscribe_reports_kept_and_honours_also(board, capsys):
    _session(board, "sess-owner", OWNER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER)

    def run(*extra):
        root = argparse.ArgumentParser(prog="hermes")
        kc.build_parser(root.add_subparsers(dest="command"))
        args = root.parse_args(["kanban", "notify-subscribe", tid, "--platform",
                                "discord", "--chat-id", OTHER, *extra])
        return kc.kanban_command(args)

    assert run() == 0
    assert "subscription kept" in capsys.readouterr().out
    with kb.connect_closing() as conn:
        assert _chats(conn, tid) == [OWNER]
    assert run("--also") == 0
    with kb.connect_closing() as conn:
        assert _chats(conn, tid) == sorted([OWNER, OTHER])


# (3) repair --------------------------------------------------------------------

def test_notify_repair_dedupe_keeps_home_chat(board, capsys):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn, home_sid="sess-owner")
        _sub(conn, tid, OTHER, also=True)   # older, foreign
        _sub(conn, tid, OWNER, also=True)
        _sub(conn, tid, "1553871972885078087", also=True)
        single = _card(conn)
        _sub(conn, single, OWNER)

    def run(*extra):
        root = argparse.ArgumentParser(prog="hermes")
        kc.build_parser(root.add_subparsers(dest="command"))
        return kc.kanban_command(root.parse_args(
            ["kanban", "notify-repair", "--dedupe", *extra]))

    assert run() == 0  # dry run
    out = capsys.readouterr().out
    assert tid in out and single not in out and "would drop 2" in out
    with kb.connect_closing() as conn:
        assert len(_chats(conn, tid)) == 3
    assert run("--apply") == 0
    with kb.connect_closing() as conn:
        assert _chats(conn, tid) == [OWNER]
        assert _chats(conn, single) == [OWNER]
    assert run() == 0
    assert "one subscriber chat per platform" in capsys.readouterr().out


# (4) a board that cannot be scanned is a FAILURE, never an all-clear ----------
# Prism P1 cf49dc4f0623 on #1635 (t_030662ba): every per-board exception was
# swallowed, so a locked DB printed the all-clear and exited 0.

def _run_dedupe(*extra):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers(dest="command"))
    return kc.kanban_command(root.parse_args(
        ["kanban", "notify-repair", "--dedupe", *extra]))


def _boom(conn, apply=False):
    raise sqlite3.OperationalError("database is locked")


def test_dedupe_board_failure_is_nonzero_and_not_all_clear(board, capsys, monkeypatch):
    monkeypatch.setattr(kb, "dedupe_notify_subs", _boom)
    assert _run_dedupe("--apply") != 0
    cap = capsys.readouterr()
    assert "one subscriber chat per platform" not in cap.out
    assert "database is locked" in cap.err


def test_dedupe_board_failure_json_reports_failed_board(board, capsys, monkeypatch):
    import json
    monkeypatch.setattr(kb, "dedupe_notify_subs", _boom)
    assert _run_dedupe("--apply", "--json") != 0
    d = json.loads(capsys.readouterr().out)
    assert d["failed"] and "database is locked" in d["failed"][0]["error"]


def test_dedupe_all_boards_one_bad_board_still_scans_the_rest(board, capsys, monkeypatch):
    real = kb.dedupe_notify_subs
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, also=True)
        _sub(conn, tid, OTHER, also=True)
    monkeypatch.setattr(kb, "list_boards",
                        lambda *a, **k: [{"slug": "default"}, {"slug": "bad"}])
    real_connect = kb.connect_closing

    def connect(*a, board=None, **k):
        if board == "bad":
            raise sqlite3.DatabaseError("file is not a database")
        return real_connect(*a, **k)
    monkeypatch.setattr(kb, "connect_closing", connect)
    monkeypatch.setattr(kb, "dedupe_notify_subs", real)
    assert _run_dedupe("--all-boards") != 0
    cap = capsys.readouterr()
    assert tid in cap.out                      # the good board was still reported
    assert "'bad'" in cap.err and "file is not a database" in cap.err
