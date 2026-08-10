"""End-to-end acceptance for the "phantom second session per channel" bug.

The unit suites pin the DB column and the repair verb. This one drives the
*whole* chain the card describes — subscribe -> notify-sub row -> the wake
injector's own scope-rebuild code -> ``build_session_key`` — and asserts the
property Ace actually reported: a kanban notification delivered into a
per-user channel resolves to the SAME session key as the creator's own
messages. ONE key for that channel, not two.

Deliberately NOT a mock of the rebuild: it imports the real
``build_session_key`` and reproduces the wake injector's ``SessionSource``
construction from ``gateway/kanban_watchers.py`` field-for-field, so a change
to either side shows up here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

CHAT = "1535189663533506600"
USER = "117431298246705156"


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


def _wake_key(sub: dict) -> str:
    """Drive the wake injector's scope rebuild, then the real key builder.

    Mirrors ``GatewayKanbanWatchersMixin._kanban_notifier_watcher``'s
    push-adapter branch: chat_type falls back through the row column, then
    delivery_metadata, then "group".
    """
    chat_type = str(sub.get("chat_type") or "").strip()
    if not chat_type:
        meta = sub.get("delivery_metadata")
        if isinstance(meta, dict):
            chat_type = str(meta.get("chat_type") or "").strip()
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=sub["chat_id"],
        chat_type=chat_type or "group",
        thread_id=sub.get("thread_id") or None,
        user_id=sub.get("user_id"),
    )
    return build_session_key(source, group_sessions_per_user=True)


def _creator_key() -> str:
    return build_session_key(
        SessionSource(
            platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
            user_id=USER,
        ),
        group_sessions_per_user=True,
    )


def _only_sub(task_id: str) -> dict:
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id)
        assert len(subs) == 1, subs
        return subs[0]
    finally:
        conn.close()


def test_gateway_turn_create_yields_one_key_for_the_channel(kanban_home, monkeypatch):
    """A card created from a gateway turn with a bound identity: the wake and
    the creator's own messages resolve to the same key."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", CHAT)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", USER)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    from gateway.session_context import reset_session_vars

    reset_session_vars()

    from tools.kanban_tools import subscribe_calling_session

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="from a gateway turn", assignee="worker")
        assert subscribe_calling_session(conn, tid) is True
    finally:
        conn.close()

    keys = {_wake_key(_only_sub(tid)), _creator_key()}
    assert len(keys) == 1, f"two session keys for one channel: {sorted(keys)}"
    assert next(iter(keys)).endswith(f":{USER}")


def test_repairing_a_legacy_row_collapses_two_keys_into_one(kanban_home, monkeypatch):
    """The historical rows: BEFORE the repair the wake resolves to a second,
    chat-unreachable key; AFTER it, to the creator's own. This is the card's
    acceptance criterion stated as a before/after differential."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy row", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT, chat_type="group",
        )
    finally:
        conn.close()

    before = _wake_key(_only_sub(tid))
    assert before != _creator_key(), "fixture did not reproduce the split"
    assert not before.endswith(f":{USER}")

    monkeypatch.setattr(
        kc, "_routing_participant_index", lambda: {("discord", CHAT): {USER}},
    )
    assert kc._cmd_notify_repair(
        argparse.Namespace(dry_run=False, json=False)
    ) == 0

    after = _wake_key(_only_sub(tid))
    assert after == _creator_key()
    assert after != before


def test_userless_subscription_keeps_delivering_to_the_shared_session(kanban_home):
    """NEGATIVE CONTROL, end to end: a cron/CLI-origin sub still resolves to a
    real, deliverable key — the shared per-chat session — rather than being
    dropped or given a fabricated participant."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cron origin", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT, chat_type="group",
        )
    finally:
        conn.close()

    sub = _only_sub(tid)
    assert not sub["user_id"]
    key = _wake_key(sub)
    assert key.endswith(CHAT)
    assert key != _creator_key()
