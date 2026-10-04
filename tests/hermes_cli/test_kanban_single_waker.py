"""One WAKER per card, wake by default (Ace 2026-10-03 13:28, t_74bf5296).

"I don't want to wake up multiple sessions." At most one subscription row per
card carries a wake mode, across every platform. The store enforces it twice:
``add_notify_sub`` resolves who holds the wake (a second subscriber gets
``notify``; a takeover MOVES the wake), and the partial UNIQUE index
``idx_notify_one_waker`` refuses a second wake row from any writer.

Live precedent (Athena, 13:47 PT): notification-replay t_46aeb7d5 / t_72ee25ec
each held telegram ``wake`` + discord ``notify+wake``. Per-platform dedupe
missed both.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

OWNER = "1553876718639390760"
OTHER = "1552754415428177980"
TG = "571820863"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kbn._CHAT_LIVENESS_CACHE.clear()
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


def _session(home: Path, sid: str, chat: str, age_s: float, source: str = "discord") -> None:
    ts = time.time() - age_s
    db = sqlite3.connect(home / "state.db")
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        (sid, source, f"agent:main:{source}:group:{chat}:u1", chat, ts, ts, ts),
    )
    db.commit()
    db.close()


def _card(conn, home_sid: str = "sess-owner") -> str:
    tid = kb.create_task(conn, title="card", assignee="worker")
    conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (home_sid, tid))
    conn.commit()
    return tid


def _modes(conn, tid) -> dict:
    return {
        (s["platform"], s["chat_id"]): s["delivery_mode"]
        for s in kbn.list_notify_subs(conn, tid)
    }


def _wakers(conn, tid) -> list:
    return [k for k, m in _modes(conn, tid).items() if m in kb.NOTIFY_WAKE_MODES]


def _sub(conn, tid, chat, platform="discord", **kw):
    return kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat,
                             chat_type="group", **kw)


# -- second subscriber gets notify --------------------------------------------

def test_second_chat_with_also_gets_notify_not_wake(board, caplog):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        with caplog.at_level("INFO"):
            _sub(conn, tid, OTHER, delivery_mode="notify+wake", also=True)
        assert _modes(conn, tid) == {
            ("discord", OWNER): "notify+wake", ("discord", OTHER): "notify",
        }
    assert f"wake held: discord:{OWNER} holds the wake on {tid}" in caplog.text


def test_cross_platform_second_waker_gets_notify(board):
    """The t_72ee25ec shape: a telegram wake next to a discord waker."""
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-tg", TG, age_s=60, source="telegram")
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        _sub(conn, tid, TG, platform="telegram", delivery_mode="wake")
        assert _wakers(conn, tid) == [("discord", OWNER)]
        assert _modes(conn, tid)[("telegram", TG)] == "notify"


def test_resubscribe_of_waker_keeps_its_wake(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        _sub(conn, tid, OWNER)  # mode None: untouched
        assert _wakers(conn, tid) == [("discord", OWNER)]


# -- the index: no writer can store two wakers --------------------------------

def _raw_wake_row(conn, tid, chat, platform="telegram"):
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id,"
        " delivery_mode, created_at) VALUES (?, ?, ?, '', 'wake', 0)",
        (tid, platform, chat),
    )


def test_store_refuses_second_wake_row_from_raw_sql(board):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        with pytest.raises(sqlite3.IntegrityError):
            _raw_wake_row(conn, tid, TG)


def test_mutation_without_index_second_wake_row_lands(board):
    """Mutation proof: the refusal above IS the index, not something else."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        conn.execute(f"DROP INDEX {kb.ONE_WAKER_INDEX}")
        _raw_wake_row(conn, tid, TG)  # no IntegrityError without the index
        assert len(_wakers(conn, tid)) == 2


def test_migration_collapses_legacy_double_wakers_and_restores_index(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        conn.execute(f"DROP INDEX {kb.ONE_WAKER_INDEX}")
        _raw_wake_row(conn, tid, TG)                      # oldest, foreign
        _raw_wake_row(conn, tid, OWNER, platform="discord")  # the home chat
        conn.commit()
        assert len(_wakers(conn, tid)) == 2
    kb.init_db()  # re-runs the migration pass
    with kb.connect_closing() as conn:
        assert _wakers(conn, tid) == [("discord", OWNER)], "home chat keeps the wake"
        assert _modes(conn, tid)[("telegram", TG)] == "notify", "row kept, demoted"
        with pytest.raises(sqlite3.IntegrityError):
            _raw_wake_row(conn, tid, "x")


def test_api_server_rows_are_exempt(board):
    """api_server's wake self-post IS its delivery: never demoted, never counted."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        for origin in ("origin-a", "origin-b"):
            kbn.add_notify_sub(conn, task_id=tid, platform="api_server",
                              chat_id=origin, also=True)
        modes = _modes(conn, tid)
        assert modes[("api_server", "origin-a")] == "notify+wake"
        assert modes[("api_server", "origin-b")] == "notify+wake"
        assert modes[("discord", OWNER)] == "notify+wake"
        assert kb.card_waker(conn, tid)["chat_id"] == OWNER


# -- takeover moves the wake ---------------------------------------------------

def test_explicit_takeover_moves_the_wake(board, caplog):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        with caplog.at_level("INFO"):
            _sub(conn, tid, OTHER, delivery_mode="notify+wake", also=True, takeover=True)
        assert _wakers(conn, tid) == [("discord", OTHER)]
        assert _modes(conn, tid)[("discord", OWNER)] == "notify"
    assert f"wake moved: {tid} discord:{OWNER} -> discord:{OTHER}" in caplog.text


def test_idle_holder_loses_the_wake(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-tg", TG, age_s=3 * 86400, source="telegram")
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, TG, platform="telegram", delivery_mode="wake")
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        assert _wakers(conn, tid) == [("discord", OWNER)]


def test_home_chat_takes_the_wake_back(board):
    """A --takeover re-home subscribes the new home; the wake follows it."""
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-tg", TG, age_s=60, source="telegram")
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, TG, platform="telegram", delivery_mode="wake")
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        assert _wakers(conn, tid) == [("discord", OWNER)]


# -- inheritance ---------------------------------------------------------------

def test_inherited_wake_becomes_notify_when_child_has_a_waker(board):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-tg", TG, age_s=60, source="telegram")
    with kb.connect_closing() as conn:
        parent = _card(conn)
        _sub(conn, parent, TG, platform="telegram", delivery_mode="wake")
        child = _card(conn)
        _sub(conn, child, OWNER, delivery_mode="notify+wake")
        kb.link_tasks(conn, parent, child)
        modes = _modes(conn, child)
        assert modes[("telegram", TG)] == "notify", "inherited row kept as notify"
        assert _wakers(conn, child) == [("discord", OWNER)]


# -- CLI -----------------------------------------------------------------------

def _cli_sub(tid, chat, **kw):
    ns = argparse.Namespace(
        task_id=tid, platform="discord", chat_id=chat, chat_type="group",
        thread_id=None, user_id=None, user_id_alt=None, notifier_profile=None,
        delivery_mode=None, wake=True, also=True, takeover=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return kc._cmd_notify_subscribe(ns)


def test_cli_prints_who_holds_the_wake(board, capsys):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
    assert _cli_sub(tid, OTHER) == 0
    out = capsys.readouterr().out
    assert f"wake held by discord:{OWNER}" in out
    assert _cli_sub(tid, OTHER, takeover=True) == 0
    assert "wake held by" not in capsys.readouterr().out
    kc._cmd_notify_list(argparse.Namespace(task_id=tid, json=False))
    lines = capsys.readouterr().out.splitlines()
    assert [l for l in lines if "[waker]" in l][0].count(OTHER) == 1
    assert len([l for l in lines if "[waker]" in l]) == 1


# -- default = wake ------------------------------------------------------------

def _gateway_env(monkeypatch, chat):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "u1")


def test_auto_subscribe_defaults_to_wake(board, monkeypatch):
    from tools.kanban_tools import subscribe_calling_session

    _gateway_env(monkeypatch, OWNER)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert subscribe_calling_session(conn, tid, require_platform_identity=True)
        assert _modes(conn, tid) == {("discord", OWNER): "notify+wake"}


def test_auto_subscribe_notify_only_opt_out(board, monkeypatch):
    from tools.kanban_tools import subscribe_calling_session

    _gateway_env(monkeypatch, OWNER)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert subscribe_calling_session(conn, tid, wake=False)
        assert _modes(conn, tid) == {("discord", OWNER): "notify"}


# -- --takeover alone moves the wake (Prism #1679 P1 6d9abd90336a) --------------

def test_takeover_without_a_mode_moves_the_wake(board):
    """``takeover=True`` with no delivery_mode must still claim the wake.

    The CLI help ("Move the card's wake to this chat") and the ``wake held``
    hint ("Pass --takeover to move the wake") both promise the flag works
    alone; with no mode the store fell back to the row's ``notify`` and never
    called ``_claim_wake``, so the old waker kept it.
    """
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        _sub(conn, tid, OTHER, also=True)
        assert _wakers(conn, tid) == [("discord", OWNER)]
        _sub(conn, tid, OTHER, also=True, takeover=True)
        assert _wakers(conn, tid) == [("discord", OTHER)]
        assert _modes(conn, tid)[("discord", OWNER)] == "notify"


def test_takeover_with_explicit_notify_stays_notify(board):
    """An explicit mode still wins: ``--takeover --delivery-mode notify`` takes no wake."""
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
        _sub(conn, tid, OTHER, also=True, takeover=True, delivery_mode="notify")
        assert _wakers(conn, tid) == [("discord", OWNER)]


def test_cli_takeover_alone_moves_the_wake(board, capsys):
    _session(board, "sess-owner", OWNER, age_s=60)
    _session(board, "sess-other", OTHER, age_s=60)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _sub(conn, tid, OWNER, delivery_mode="notify+wake")
    assert _cli_sub(tid, OTHER, wake=False) == 0
    assert _cli_sub(tid, OTHER, wake=False, takeover=True) == 0
    assert "wake held by" not in capsys.readouterr().out
    with kb.connect_closing() as conn:
        assert _wakers(conn, tid) == [("discord", OTHER)]
