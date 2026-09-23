"""Kanban dispatcher route announcement formatting."""

from gateway.kanban_watchers import _format_spawn_routes


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
