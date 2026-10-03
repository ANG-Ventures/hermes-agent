"""``timed_out`` kanban notices name the cause instead of ``max_runtime=0s``.

Two run-ending conditions share the ``timed_out`` event kind: the wall-clock
``max_runtime_seconds`` guard and the per-run iteration cap (``agent.max_turns``).
The old formatter only knew the first, so an iteration-cap exit rendered as
``timed out (max_runtime=0s); will retry`` — a broken-looking timer that paged
the operator for a routine checkpoint (2026-10-02, 21 such pages in one day).
"""
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


def test_tui_and_gateway_formatters_share_the_renderer():
    # Both notice paths must route through the one truthful renderer.
    import inspect
    import gateway.kanban_watchers as kw
    import tui_gateway.server as srv
    assert "format_timed_out_notice" in inspect.getsource(kw)
    assert "format_timed_out_notice" in inspect.getsource(srv)
    assert "max_runtime={limit}s" not in inspect.getsource(kw)
    assert "max_runtime={limit}s" not in inspect.getsource(srv)
