"""Every path that injects text into live history stays in the CURRENT turn.

Sibling-defect sweep for #1496 (card t_3b7540fe). #1496 bounded the pre-API
/steer drain and the run-budget wrap-up notice to the current turn. The
remaining append-into-an-existing-message channel is the post-tool-batch
/steer delivery (``apply_pending_steer_to_tool_results``). It picked its target
by COUNT (the last ``num_tool_msgs`` rows), not by turn. A window wider than
the tool rows the batch actually appended reached back past the current user
message and stapled the steer onto the previous turn's tool result: lost to the
model and a prompt-cache prefix break.

Contracts pinned here:

1. RED on main: the post-tool delivery never mutates a previous-turn message.
   With no current-turn tool row in its window the steer stays pending.
2. Cache-prefix invariant: through the real ``run_conversation`` loop, with a
   steer at t=0 of a fresh turn plus one between iterations and the run-budget
   notice firing, the serialized previous-turn history is byte-identical in
   every API request and in the returned history.
3. Placement lint: every function in ``agent/`` that appends the steer marker
   or the run-budget notice into an existing message selects its target with
   ``_current_turn_tail_tool_index``.
"""

import ast
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
from agent.prompt_builder import STEER_MARKER_OPEN
from agent.tool_dispatch_helpers import make_tool_result_message

from tests.agent.test_pre_api_steer_drain_turn_bound import (
    STEER,
    _content_text,
    _history,
    _make_agent,
    _response,
    _tool_call,
)

REPO = Path(__file__).resolve().parents[2]


class _SteerAgent:
    """Minimal agent surface ``apply_pending_steer_to_tool_results`` reads."""

    def __init__(self, steer, turn_idx=None):
        self._pending_steer = steer
        self._pending_steer_lock = None
        self._persist_user_message_idx = turn_idx

    def _drain_pending_steer(self):
        text, self._pending_steer = self._pending_steer, None
        return text


def _serialize(msgs):
    return json.dumps(msgs, sort_keys=True, ensure_ascii=False)


def _fresh_turn_with_unanswered_batch():
    """Previous turn (ends in a tool batch + final reply), then a new user
    message and an assistant tool call whose results are not in history."""
    msgs = _history() + [
        {"role": "user", "content": "new question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "web_search", "arguments": "{}"}}
                for i in range(4)
            ],
        },
    ]
    return msgs, len(_history())


def test_post_tool_steer_never_mutates_previous_turn_tool_result():
    for turn_idx in (None, len(_history())):
        messages, prior_len = _fresh_turn_with_unanswered_batch()
        prior_bytes = _serialize(messages[:prior_len])
        agent = _SteerAgent(STEER, turn_idx=turn_idx)

        # Window of 4 spans [old tool, old final, user, assistant(tool_calls)].
        apply_pending_steer_to_tool_results(agent, messages, 4)

        assert _serialize(messages[:prior_len]) == prior_bytes, turn_idx
        assert all(STEER not in _content_text(m.get("content")) for m in messages)
        # Not delivered, not lost: it stays pending for the next channel.
        assert agent._pending_steer == STEER


def test_post_tool_steer_still_lands_in_current_turn_tool_result():
    messages, prior_len = _fresh_turn_with_unanswered_batch()
    prior_bytes = _serialize(messages[:prior_len])
    messages.append(make_tool_result_message("web_search", "new result", "c0"))
    agent = _SteerAgent(STEER, turn_idx=prior_len)

    apply_pending_steer_to_tool_results(agent, messages, 1)

    assert STEER in _content_text(messages[-1]["content"])
    assert STEER_MARKER_OPEN in _content_text(messages[-1]["content"])
    assert _serialize(messages[:prior_len]) == prior_bytes
    assert agent._pending_steer is None


def test_cache_prefix_byte_identical_across_all_injection_channels():
    """Real loop: steer at t=0, a second steer between iterations, and the
    run-budget notice. The previous-turn prefix never changes."""
    agent = _make_agent()
    agent.run_budget_seconds = 900
    prior_len = len(_history())
    prior_bytes = _serialize(_history())

    sent: list[list] = []
    responses = [
        _response(finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
        _response(finish_reason="tool_calls", tool_calls=[_tool_call("c2")]),
        _response(content="done"),
    ]

    def _create(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs.get("messages") or []))
        return responses.pop(0)

    agent.client.chat.completions.create.side_effect = _create

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", "new result", tc.id))
        if api_call_count == 1:
            return  # leave the batch undrained: the pre-API drain delivers
        agent._apply_pending_steer_to_tool_results(messages, len(assistant_message.tool_calls))

    def _step(api_call_count, prev_tools):
        if api_call_count == 3:
            # After the second tool batch, before the last API request.
            agent.steer("second steer")
            # Push the turn past 80% of the budget so the notice fires too.
            agent._run_budget_started_at = time.time() - 800

    agent.step_callback = _step
    assert agent.steer(STEER)  # t=0 of the fresh turn

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_flush_messages_to_session_db"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
    ):
        result = agent.run_conversation("new question", conversation_history=_history())

    assert result["final_response"] == "done"
    assert len(sent) == 3
    for i, api_messages in enumerate(sent):
        body = [m for m in api_messages if m.get("role") != "system"]
        assert _serialize(body[:prior_len]) == prior_bytes, f"API call {i}"
    assert _serialize(result["messages"][:prior_len]) == prior_bytes

    # Every injection reached the model inside the CURRENT turn.
    current = _content_text(
        [m for m in sent[2] if m.get("tool_call_id") == "c2"][0]["content"]
    )
    earlier = _content_text(
        [m for m in sent[1] if m.get("tool_call_id") == "c1"][0]["content"]
    )
    assert STEER in earlier  # t=0 steer: deferred, then pre-API into c1
    assert "second steer" in current  # between iterations: pre-API into c2
    from agent.conversation_loop import RUN_BUDGET_WRAPUP_NOTICE
    assert RUN_BUDGET_WRAPUP_NOTICE in current
    assert agent._run_budget_wrapup_injected is True
    assert not result.get("pending_steer")


_INJECTED_NAMES = {"format_steer_marker", "RUN_BUDGET_WRAPUP_NOTICE"}


def _functions_injecting(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        writes_content = any(
            isinstance(t, ast.Subscript)
            and isinstance(t.slice, ast.Constant)
            and t.slice.value == "content"
            for a in ast.walk(node)
            if isinstance(a, ast.Assign)
            for t in a.targets
        )
        if writes_content and names & _INJECTED_NAMES:
            yield node, names


def test_every_history_injection_site_uses_the_turn_bound():
    """Placement lint: a new append-into-existing-message channel must pick its
    target with ``_current_turn_tail_tool_index``, not its own backward scan."""
    offenders, seen = [], []
    for path in sorted((REPO / "agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn, names in _functions_injecting(tree):
            seen.append(f"{path.name}:{fn.name}")
            if "_current_turn_tail_tool_index" not in names:
                offenders.append(f"{path.name}:{fn.name}:{fn.lineno}")
    # The lint must see the known channels, or it proves nothing.
    assert "agent_runtime_helpers.py:apply_pending_steer_to_tool_results" in seen
    assert "conversation_loop.py:_maybe_inject_run_budget_wrapup" in seen
    assert "conversation_loop.py:run_conversation" in seen, seen
    assert offenders == []
