"""``timed_out`` kanban notices name the cause instead of ``max_runtime=0s``.

Two run-ending conditions share the ``timed_out`` event kind: the wall-clock
``max_runtime_seconds`` guard and the per-run iteration cap (``agent.max_turns``).
The old formatter only knew the first, so an iteration-cap exit rendered as
``timed out (max_runtime=0s); will retry`` — a broken-looking timer that paged
the operator for a routine checkpoint (2026-10-02, 21 such pages in one day).
"""
import pytest

from gateway.kanban_notify_failures import format_timed_out_notice


def test_iteration_cap_names_the_cap_and_keeps_work():
    msg = format_timed_out_notice(
        {"error": "Iteration budget exhausted (300/300) — task could not complete",
         "failures": 1, "retry_status": "ready"},
        task_id="t_abc", board_tag="", tag="@w ",
    )
    assert "iteration cap (300 turns)" in msg
    assert "kept" in msg
    assert "back to ready" in msg
    assert "max_runtime=0s" not in msg


def test_wall_clock_limit_still_reports_seconds():
    msg = format_timed_out_notice(
        {"elapsed_seconds": 3700, "limit_seconds": 3600, "retry_status": "ready"},
        task_id="t_abc", board_tag="", tag="",
    )
    assert "max_runtime (3600s)" in msg
    assert "max_runtime=0s" not in msg


def test_unknown_payload_never_prints_a_zero_timer():
    msg = format_timed_out_notice({}, task_id="t_abc", board_tag="", tag="")
    assert "t_abc" in msg and "timed out" in msg
    assert "max_runtime=0s" not in msg


ITER_CAP_PAYLOAD = {
    "error": "Iteration budget exhausted (300/300) — task could not complete",
    "failures": 1, "retry_status": "ready",
}


def test_tui_formatter_renders_the_iteration_cap():
    # Drive the real TUI notice path, not its source text.
    from types import SimpleNamespace
    from tui_gateway.session_notifications import _format_kanban_event_text

    ev = SimpleNamespace(kind="timed_out", payload=dict(ITER_CAP_PAYLOAD))
    task = SimpleNamespace(title="t", assignee="w", result=None)
    text = _format_kanban_event_text({"task_id": "t_abc"}, task, ev, "")
    assert "iteration cap (300 turns)" in text
    assert "max_runtime=0s" not in text


@pytest.mark.asyncio
async def test_gateway_notifier_renders_the_iteration_cap(tmp_path, monkeypatch):
    # Drive the real gateway notifier tick end to end on a temp board.
    import asyncio
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    import hermes_cli.kanban_db as kb
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap task", assignee="worker1")
        import hermes_cli.kanban_db_notify as kbn
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
        kb._append_event(conn, tid, kind="timed_out", payload=dict(ITER_CAP_PAYLOAD))
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: adapter}
    _sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=1), timeout=10.0)

    adapter.send.assert_called_once()
    msg = adapter.send.call_args[0][1]
    assert tid in msg
    assert "iteration cap (300 turns)" in msg
    assert "max_runtime=0s" not in msg
