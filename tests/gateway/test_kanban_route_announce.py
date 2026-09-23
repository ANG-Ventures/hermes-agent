"""Kanban dispatcher route announcement formatting."""

from gateway.kanban_watchers import _format_spawn_routes, _log_dispatch_tick
from hermes_cli.kanban_db import DispatchResult
import logging


def test_gateway_tick_logs_lane_source_and_idle_expiry(caplog):
    logger = logging.getLogger('test-kanban-tick')
    result = DispatchResult()
    result.spawned.append(('t_a', 'worker', '/tmp/test'))
    result.spawn_routes['t_a'] = 'claude-bpx-19/claude-opus-5'
    result.spawn_route_sources['t_a'] = 'lane-override(3600s remaining)'
    with caplog.at_level(logging.INFO, logger='test-kanban-tick'):
        _log_dispatch_tick(logger, 'sandbox', result)
        _log_dispatch_tick(logger, 'sandbox', DispatchResult(expired_lane_models=[('(board-wide)', 'claude-bpx-19/claude-opus-5')]))
    assert 'route=claude-bpx-19/claude-opus-5 source=lane-override(3600s remaining)' in caplog.text
    assert 'lane-model expired -> profile default' in caplog.text



def test_spawn_route_summary_names_each_task_route():
    summary = _format_spawn_routes(
        {
            "t_a": "openai-codex/gpt-5.6-sol-900k",
            "t_b": "claude-apr/claude-opus-5",
            "t_c": "openai-codex/gpt-6-astra-900k",
        }
    )
    assert "t_a route=openai-codex/gpt-5.6-sol-900k source=profile-default kind=standard" in summary
    assert "t_b route=claude-apr/claude-opus-5 source=profile-default kind=standard" in summary
    assert (
        "t_c route=openai-codex/gpt-6-astra-900k source=profile-default kind=firepower-override"
        in summary
    )


def test_spawn_route_source_includes_lane_ttl():
    summary = _format_spawn_routes(
        {"t_a": "claude-bpx-19/claude-opus-5"},
        {"t_a": "lane-override(3600s remaining)"},
    )
    assert "route=claude-bpx-19/claude-opus-5 source=lane-override(3600s remaining)" in summary
