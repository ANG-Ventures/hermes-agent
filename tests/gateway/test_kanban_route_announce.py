"""Kanban dispatcher route announcement formatting."""

from gateway.kanban_watchers_dispatcher import _format_spawn_routes


def test_spawn_route_summary_names_each_task_route():
    summary = _format_spawn_routes({
        "t_a": "openai-codex/gpt-5.6-sol-900k",
        "t_b": "claude-apr/claude-opus-5",
        "t_c": "openai-codex/gpt-6-astra-900k",
    })
    assert "t_a route=openai-codex/gpt-5.6-sol-900k kind=standard" in summary
    assert "t_b route=claude-apr/claude-opus-5 kind=standard" in summary
    assert (
        "t_c route=openai-codex/gpt-6-astra-900k kind=firepower-override"
        in summary
    )
