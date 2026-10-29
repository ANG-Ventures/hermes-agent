"""/kanban create auto-subscribe writes the notify row on the board the card landed on.

Prism 35e770f2b096: the slash handler only sees a LEADING ``--board``. When the CLI derived the
board (``--parent <card>``, or ``--board`` after the verb), the subscription went to ``default``.
"""
import asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_subscription_lands_on_the_cards_board_when_the_slash_named_none(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
    from hermes_cli import kanban_db_board_registry as reg

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    reg._PINNED_BOARD_MEMO.clear()
    kb.create_board("foo")
    with kbc.connect_closing(board="foo") as conn:
        task = kb.create_task(conn, title="child of a foo card")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    source = SessionSource(platform=Platform.DISCORD, chat_id="post", chat_type="thread",
                           thread_id="post", profile="default")
    event = MessageEvent(text=f"/kanban create --parent {task} child", source=source)
    assert asyncio.run(runner._kanban_auto_subscribe(event, task, None))

    with kbc.connect_closing(board="foo") as conn:
        assert [s["chat_id"] for s in kbn.list_notify_subs(conn, task)] == ["post"]
    with kbc.connect_closing(board="default") as conn:
        assert kbn.list_notify_subs(conn, task) == []
