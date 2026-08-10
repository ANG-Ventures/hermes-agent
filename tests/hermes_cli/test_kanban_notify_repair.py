"""``hermes kanban notify-repair`` — backfill the creator identity on legacy
identity-less notify subscriptions.

``add_notify_sub`` is ``INSERT OR IGNORE``, so a subscription nobody
re-subscribes to can never self-heal its empty ``user_id`` — and while it is
empty the wake injector rebuilds the creator's scope without a participant,
splitting one chat into two session keys (see
``tests/gateway/test_kanban_notify_sub_user_id.py`` for that half).

The rules pinned here are the ones that make the repair safe to run against a
live board:

* it backfills only when the creator identity is UNAMBIGUOUS,
* it never invents an identity for a genuinely user-less origin,
* it never re-points a row that already names a participant,
* it is idempotent, and ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

CHAT = "1535189663533506600"
USER = "117431298246705156"
OTHER_USER = "999000111222333444"
CRON_CHAT = "1523978409129021484"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    resolved = str(kb.kanban_db_path())
    assert str(tmp_path) in resolved, f"test escaped its sandbox: {resolved}"
    kb.init_db()
    return home


def _routing(monkeypatch, chats: dict[tuple[str, str], list[str]]) -> None:
    """Stand in for the gateway routing index with an explicit chat->users map."""
    index = {key: set(users) for key, users in chats.items()}
    monkeypatch.setattr(kc, "_routing_participant_index", lambda: index)


def _sub(conn, task_id: str, *, chat_id: str = CHAT, user_id=None) -> None:
    kb.add_notify_sub(
        conn, task_id=task_id, platform="discord", chat_id=chat_id,
        chat_type="group", user_id=user_id,
    )


def _run(**kwargs) -> int:
    args = argparse.Namespace(dry_run=False, json=False)
    for key, value in kwargs.items():
        setattr(args, key, value)
    return kc._cmd_notify_repair(args)


def _user_id_of(task_id: str):
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id)
        assert len(subs) == 1, subs
        return subs[0]["user_id"]
    finally:
        conn.close()


def test_backfills_the_unambiguous_creator_identity(kanban_home, monkeypatch, capsys):
    """One known participant for the chat -> the empty row is repaired."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()
    assert not _user_id_of(tid)

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0

    assert _user_id_of(tid) == USER
    assert "Backfilled 1 of 1" in capsys.readouterr().out


def test_dry_run_reports_without_writing(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="dry", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run(dry_run=True) == 0

    assert not _user_id_of(tid), "--dry-run wrote to the DB"
    assert "Would backfill 1 of 1" in capsys.readouterr().out


def test_userless_origin_is_left_alone(kanban_home, monkeypatch, capsys):
    """NEGATIVE CONTROL. A cron / CLI / home-channel subscription has no
    participant by construction. The repair must report it and move on — a
    fabricated identity would route a system notification into a human's
    private per-user session."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cron origin", assignee="worker")
        _sub(conn, tid, chat_id=CRON_CHAT)
    finally:
        conn.close()

    # No routing evidence for that chat at all.
    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0

    assert not _user_id_of(tid), "an identity was fabricated for a user-less origin"
    out = capsys.readouterr().out
    assert "Left untouched (1)" in out
    assert "No identity is invented" in out


def test_ambiguous_shared_chat_is_left_alone(kanban_home, monkeypatch):
    """Two humans in one channel: there is no single right answer, and picking
    one would hijack the other's notification lane."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="shared", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER, OTHER_USER]})
    assert _run() == 0

    assert not _user_id_of(tid)


def test_never_repoints_a_row_that_already_has_an_identity(kanban_home, monkeypatch):
    """Rows naming a participant are outside the repair's WHERE clause, so a
    different resolved identity cannot steal an existing subscription."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned", assignee="worker")
        _sub(conn, tid, user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [OTHER_USER]})
    assert _run() == 0

    assert _user_id_of(tid) == USER


def test_repair_is_idempotent(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="twice", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0
    capsys.readouterr()

    assert _run() == 0
    assert _user_id_of(tid) == USER
    assert "nothing to repair" in capsys.readouterr().out


def test_json_report_separates_repaired_from_skipped(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        good = kb.create_task(conn, title="good", assignee="worker")
        _sub(conn, good)
        cron = kb.create_task(conn, title="cron", assignee="worker")
        _sub(conn, cron, chat_id=CRON_CHAT)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run(json=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["considered"] == 2
    assert report["backfilled"] == 1
    assert report["skipped_no_evidence"] == 1
    by_task = {r["task_id"]: r for r in report["rows"]}
    assert by_task[good]["user_id"] == USER
    assert by_task[good]["action"] == "backfilled"
    assert by_task[cron]["user_id"] is None
    assert by_task[cron]["action"] == "skipped_no_evidence"
