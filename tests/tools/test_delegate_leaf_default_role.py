"""delegate_task: the orchestrator role is explicit opt-in.

A child is a LEAF unless its parent passes ``role="orchestrator"``;
``delegation.max_spawn_depth`` is only the ceiling. Before this contract the
role was depth-derived: with max_spawn_depth=5 every child at depth 1-4 was
promoted to orchestrator (kept the ``delegation`` toolset and got the
"Subagent Spawning" prompt section) even though no caller asked for it. That
is what fanned out 129 role-less delegate_task calls on 2026-09-08.
"""

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task

_SPAWN_SECTION = "Subagent Spawning (Orchestrator Role)"
_CREDS = {
    "provider": None, "base_url": None,
    "api_key": None, "api_mode": None, "model": None,
}


def _parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file", "delegation"]
    return parent


def _child():
    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done", "completed": True,
        "api_calls": 1, "messages": [],
    }
    child._delegate_saved_tool_names = []
    child._credential_pool = None
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.model = "test"
    return child


def _spawn(cfg, *, depth=0, **kwargs):
    """Run delegate_task with a mocked AIAgent; return (child, AIAgent kwargs)."""
    with (
        patch("tools.delegate_tool._resolve_delegation_credentials", return_value=_CREDS),
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("run_agent.AIAgent") as MockAgent,
    ):
        child = _child()
        MockAgent.return_value = child
        delegate_task(goal="gather facts", parent_agent=_parent(depth), **kwargs)
        return child, MockAgent.call_args.kwargs


def _assert_leaf(child, agent_kwargs):
    assert child._delegate_role == "leaf"
    assert "delegation" not in agent_kwargs["enabled_toolsets"]
    assert _SPAWN_SECTION not in agent_kwargs["ephemeral_system_prompt"]


def _assert_orchestrator(child, agent_kwargs):
    assert child._delegate_role == "orchestrator"
    assert "delegation" in agent_kwargs["enabled_toolsets"]
    assert _SPAWN_SECTION in agent_kwargs["ephemeral_system_prompt"]


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_no_role_is_leaf_at_every_depth_below_the_ceiling(depth):
    """The 2026-09-08 reproduction: max_spawn_depth=5, no role passed."""
    child, kwargs = _spawn({"max_spawn_depth": 5}, depth=depth)
    _assert_leaf(child, kwargs)


def test_batch_task_without_role_is_leaf():
    child, kwargs = _spawn({"max_spawn_depth": 5}, tasks=[{"goal": "gather facts"}])
    _assert_leaf(child, kwargs)


def test_explicit_orchestrator_below_ceiling_gets_toolset_and_section():
    child, kwargs = _spawn({"max_spawn_depth": 5}, role="orchestrator")
    _assert_orchestrator(child, kwargs)
    assert "max_spawn_depth=5" in kwargs["ephemeral_system_prompt"]


def test_per_task_orchestrator_role_is_honored():
    child, kwargs = _spawn(
        {"max_spawn_depth": 5}, tasks=[{"goal": "lead", "role": "orchestrator"}]
    )
    _assert_orchestrator(child, kwargs)


def test_per_task_leaf_beats_top_level_orchestrator():
    child, kwargs = _spawn(
        {"max_spawn_depth": 5},
        role="orchestrator",
        tasks=[{"goal": "gather", "role": "leaf"}],
    )
    _assert_leaf(child, kwargs)


def test_explicit_orchestrator_at_floor_downgrades_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="tools.delegate_tool")
    # parent depth 1, max_spawn_depth 2 -> child depth 2 is the floor
    child, kwargs = _spawn({"max_spawn_depth": 2}, depth=1, role="orchestrator")
    _assert_leaf(child, kwargs)
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("depth=2" in m and "orchestrator" in m for m in warnings), warnings


def test_kill_switch_forces_leaf_even_when_requested():
    child, kwargs = _spawn(
        {"max_spawn_depth": 5, "orchestrator_enabled": False}, role="orchestrator"
    )
    _assert_leaf(child, kwargs)


def test_spawn_logs_one_attribution_line(caplog):
    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    _spawn({"max_spawn_depth": 5}, depth=1)
    lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("delegate_task spawn id=")
    ]
    assert len(lines) == 1, lines
    assert "depth=2" in lines[0]
    assert "role=leaf" in lines[0]
    assert "requested=leaf" in lines[0]
    assert "max_spawn_depth=5" in lines[0]


def test_schema_advertises_role_top_level_and_per_task():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    for surface in (props, props["tasks"]["items"]["properties"]):
        assert surface["role"]["enum"] == ["leaf", "orchestrator"]
    desc = props["role"]["description"]
    assert "leaf (default)" in desc
    assert "cannot delegate" in desc
    assert "Gathering/research/coding workers are leaves" in desc
