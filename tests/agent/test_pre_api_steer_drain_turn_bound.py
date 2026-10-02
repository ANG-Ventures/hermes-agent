"""The pre-API /steer drain must only inject into a tool result of the CURRENT turn.

Incident (2026-09-29, session 20260927_183134_b6f8858a): a /steer accepted in
the window between a new turn's start and its first API request was appended
to the PREVIOUS turn's final tool result -- before the previous final assistant
reply and before the new user message. The model never acted on it, the only
log line was DEBUG, and the historical-message mutation broke the prompt-cache
prefix. The gateway had already acked "Steered into current run".

Contract pinned here, through the real ``AIAgent.run_conversation`` loop:

1. First API call of a turn: no historical message is mutated; the steer stays
   pending and the first tool batch of THIS turn delivers it.
2. A steer that arrives between iterations (after a tool batch of this turn)
   is delivered by the pre-API drain as a standalone user row right after THIS
   turn's newest tool result (upstream #110979 delivery shape; the tool row
   itself is never rewritten), with an INFO ``Delivered /steer to agent
   (pre-API`` log line.
3. A turn that ends with no tool batch hands it back as
   ``result["pending_steer"]`` (the gateway delivers it as the next turn).
"""

import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.prompt_builder import STEER_MARKER_OPEN
from agent.tool_dispatch_helpers import make_tool_result_message
from run_agent import AIAgent

STEER = "slim the CLI harness please"


def _make_agent():
    home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (home / "logs").mkdir(parents=True, exist_ok=True)
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "web_search tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _history():
    """A completed PREVIOUS turn that ended with a tool batch + final reply."""
    return [
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "old1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old1", "content": "old result"},
        {"role": "assistant", "content": "old final answer"},
    ]


def _tool_call(call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="web_search", arguments="{}"),
    )


def _response(content="", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _content_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def _steer_after_tool(api_messages, call_id):
    """Text of the standalone steer user row that directly follows tool result *call_id*."""
    for i, m in enumerate(api_messages):
        if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
            nxt = api_messages[i + 1] if i + 1 < len(api_messages) else {}
            return _content_text(nxt.get("content")) if nxt.get("role") == "user" else ""
    return ""


def _run(agent, responses, *, execute=None, step_callback=None):
    sent: list[list] = []
    queue = list(responses)

    def _create(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs.get("messages") or []))
        return queue.pop(0)

    agent.client.chat.completions.create.side_effect = _create
    if step_callback is not None:
        agent.step_callback = step_callback

    def _default_execute(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", "new result", tc.id))
        # Mirror the real tool_executor: deliver pending steer after the batch.
        agent._apply_pending_steer_to_tool_results(messages, len(assistant_message.tool_calls))

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_flush_messages_to_session_db"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=execute or _default_execute),
    ):
        result = agent.run_conversation("new question", conversation_history=_history())
    return result, sent


def _tool_contents(api_messages):
    return {
        m.get("tool_call_id"): _content_text(m.get("content"))
        for m in api_messages
        if m.get("role") == "tool"
    }


def test_first_api_call_never_injects_into_previous_turn_tool_result():
    agent = _make_agent()
    assert agent.steer(STEER)

    result, sent = _run(
        agent,
        [
            _response(finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
            _response(content="done"),
        ],
    )

    assert result["final_response"] == "done"
    first_call_tools = _tool_contents(sent[0])
    # The previous turn's tool result is sent byte-identical (cache prefix intact).
    assert first_call_tools["old1"] == "old result"
    assert all(STEER not in _content_text(m.get("content")) for m in sent[0])

    # After this turn's first tool batch, the steer lands in THIS turn's result.
    second_call_tools = _tool_contents(sent[1])
    assert second_call_tools["old1"] == "old result"
    assert second_call_tools["c1"] == "new result"  # the tool row is never rewritten
    assert STEER in _steer_after_tool(sent[1], "c1")
    assert STEER_MARKER_OPEN in _steer_after_tool(sent[1], "c1")
    assert not result.get("pending_steer")


def test_turn_without_tool_batch_hands_steer_back_as_pending():
    agent = _make_agent()
    assert agent.steer(STEER)

    result, sent = _run(agent, [_response(content="direct answer")])

    assert result["final_response"] == "direct answer"
    assert result.get("pending_steer") == STEER
    assert _tool_contents(sent[0])["old1"] == "old result"
    # The durable history the gateway persists is not mutated either.
    old_tool = [m for m in result["messages"] if m.get("tool_call_id") == "old1"]
    assert old_tool and _content_text(old_tool[0]["content"]) == "old result"


def test_steer_between_iterations_injects_into_current_turn_tool():
    import agent.conversation_loop as conversation_loop

    agent = _make_agent()

    def _execute_without_post_drain(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", "new result", tc.id))

    def _step(api_call_count, prev_tools):
        # Arrives after this turn's tool batch, before the next API request.
        if api_call_count == 2:
            agent.steer(STEER)

    info_lines: list[str] = []
    real_info = conversation_loop.logger.info

    def _capture_info(msg, *args, **kwargs):
        info_lines.append(msg % args if args else msg)
        return real_info(msg, *args, **kwargs)

    with patch.object(conversation_loop.logger, "info", side_effect=_capture_info):
        result, sent = _run(
            agent,
            [
                _response(finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
                _response(content="done"),
            ],
            execute=_execute_without_post_drain,
            step_callback=_step,
        )

    assert result["final_response"] == "done"
    tools = _tool_contents(sent[1])
    assert tools["old1"] == "old result"
    assert tools["c1"] == "new result"
    assert STEER in _steer_after_tool(sent[1], "c1")
    assert not result.get("pending_steer")
    assert any("Delivered /steer to agent (pre-API" in line for line in info_lines)


def test_run_budget_wrapup_never_lands_in_previous_turn_tool_result():
    """Sibling channel: the run-budget wrap-up notice uses the same append-to-
    newest-tool-result delivery and must obey the same turn bound."""
    import time

    from agent.conversation_loop import (
        RUN_BUDGET_WRAPUP_NOTICE,
        _maybe_inject_run_budget_wrapup,
    )

    agent = SimpleNamespace(
        run_budget_seconds=900,
        _run_budget_started_at=time.time() - 800,
        _run_budget_wrapup_injected=False,
    )
    messages = _history() + [{"role": "user", "content": "new question"}]
    before = copy.deepcopy(messages)
    assert _maybe_inject_run_budget_wrapup(agent, messages) is False
    assert messages == before
    assert agent._run_budget_wrapup_injected is False

    messages += [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "new result"},
    ]
    assert _maybe_inject_run_budget_wrapup(agent, messages) is True
    assert RUN_BUDGET_WRAPUP_NOTICE in messages[-1]["content"]
    assert messages[2]["content"] == "old result"


def test_steer_after_synthetic_mid_turn_nudge_still_injects_into_current_turn_tool():
    """Synthetic user nudges (verify/kanban stop guards, length continuation,
    Codex incomplete) follow this turn's tool results. They are not a turn
    boundary: the pre-API drain must still reach THIS turn's tool result
    instead of deferring the steer to pending_steer (a separate later turn)."""
    agent = _make_agent()

    def _execute_then_nudge(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", "new result", tc.id))
        messages.append(
            {"role": "user", "content": "verify before stopping", "_pre_verify_synthetic": True}
        )

    def _step(api_call_count, prev_tools):
        if api_call_count == 2:
            agent.steer(STEER)

    result, sent = _run(
        agent,
        [
            _response(finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
            _response(content="done"),
        ],
        execute=_execute_then_nudge,
        step_callback=_step,
    )

    assert result["final_response"] == "done"
    tools = _tool_contents(sent[1])
    assert tools["old1"] == "old result"
    assert tools["c1"] == "new result"
    assert STEER in _steer_after_tool(sent[1], "c1")
    assert not result.get("pending_steer")


def test_turn_tail_tool_index_uses_turn_start_and_rejects_invalid_index():
    from agent.conversation_loop import _current_turn_tail_tool_index

    messages = _history() + [
        {"role": "user", "content": "new question"},  # index 4
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "new result"},  # 6
        {"role": "user", "content": "nudge", "_kanban_stop_synthetic": True},
    ]
    assert _current_turn_tail_tool_index(messages, 4) == 6
    # No tool after the turn start: never reaches the previous turn (index 2).
    assert _current_turn_tail_tool_index(messages[:5], 4) is None
    # Unknown/invalid bound: conservative first-user stop.
    assert _current_turn_tail_tool_index(messages, None) is None
    assert _current_turn_tail_tool_index(messages, 2) is None  # not a user row
    assert _current_turn_tail_tool_index(messages, 99) is None


def test_pre_api_drain_does_not_depend_on_tool_content_shape():
    """Delivery is a standalone user row, so a current-turn tool result whose content cannot
    take a text block no longer matters (the fork's append-into-content put-back path is gone
    with upstream #110979): the steer is delivered, logged, and not left pending."""
    import agent.conversation_loop as conversation_loop

    agent = _make_agent()

    def _execute_bad_content(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": 12345})

    def _step(api_call_count, prev_tools):
        if api_call_count == 2:
            agent.steer(STEER)

    info_lines: list[str] = []
    real_info = conversation_loop.logger.info

    def _capture_info(msg, *args, **kwargs):
        info_lines.append(msg % args if args else msg)
        return real_info(msg, *args, **kwargs)

    with patch.object(conversation_loop.logger, "info", side_effect=_capture_info):
        result, sent = _run(
            agent,
            [
                _response(finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
                _response(content="done"),
            ],
            execute=_execute_bad_content,
            step_callback=_step,
        )

    assert any("Delivered /steer to agent (pre-API" in line for line in info_lines)
    assert STEER in _steer_after_tool(sent[1], "c1")
    assert not result.get("pending_steer")
