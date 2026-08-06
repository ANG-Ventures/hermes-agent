"""Regression test for the ``hermes kanban notify-subscribe`` CLI seam.

Commit 11cffc4d5 (fork adaptation of upstream #80564) left a
``chat_type=args.chat_type`` kwarg at the ``_cmd_notify_subscribe`` call
site even though this fork's ``kanban_db.add_notify_sub`` predates the
upstream ``chat_type`` param. Every ``hermes kanban notify-subscribe``
invocation raised ``TypeError: add_notify_sub() got an unexpected keyword
argument 'chat_type'`` — the command was fully broken.

This test intentionally drives the REAL CLI seam end-to-end:
argparse (``build_parser``) -> ``kanban_command`` dispatch ->
``_cmd_notify_subscribe`` -> ``add_notify_sub`` -> persisted row in
``kanban_notify_subs``. A direct DB-helper unit test would not have
caught the regression, because the bug lived in the kwargs the CLI
handler forwarded — not in the helper.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # notify-subscribe is a mutation verb; the delegated-child guard in
    # kanban_command would short-circuit before reaching the handler.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb.init_db()
    return home


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    """Build the real ``hermes kanban`` parser tree and parse ``argv``."""
    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    kc.build_parser(sub)
    return root.parse_args(argv)


def test_notify_subscribe_cli_persists_row(kanban_home, capsys):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="notify seam test")

    args = _parse_cli([
        "kanban", "notify-subscribe", task_id,
        "--platform", "discord",
        "--chat-id", "1234567890",
        "--chat-type", "dm",  # accepted flag; must not break the DB write
        "--thread-id", "42",
        "--user-id", "u_1",
        "--notifier-profile", "test-profile",
    ])
    rc = kc.kanban_command(args)

    captured = capsys.readouterr()
    assert rc == 0, f"notify-subscribe failed: {captured.err}"
    assert f"Subscribed discord:1234567890:42 to {task_id}" in captured.out

    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT task_id, platform, chat_id, thread_id, user_id,"
            "       notifier_profile"
            "  FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row == {
        "task_id": task_id,
        "platform": "discord",
        "chat_id": "1234567890",
        "thread_id": "42",
        "user_id": "u_1",
        "notifier_profile": "test-profile",
    }


def test_notify_subscribe_cli_unknown_task_errors(kanban_home, capsys):
    args = _parse_cli([
        "kanban", "notify-subscribe", "t_nope",
        "--platform", "telegram",
        "--chat-id", "99",
    ])
    rc = kc.kanban_command(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "no such task: t_nope" in captured.err

    with kb.connect_closing() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs"
        ).fetchone()[0]
    assert count == 0
