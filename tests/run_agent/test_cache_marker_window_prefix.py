"""The rolling cache_control window must not read as a prefix mutation.

Live finding (card t_29abfaf6, 2026-09-30): 465 blackbox ``messages``
mutations in 24 h on Apollo (2602 all-time), every one on
openrouter/moonshotai/kimi-k3, every one exactly 25 bytes. A live 3-call turn
on that lane rebuilt the delta byte-exact: the breakpoint decorator wraps a
string ``content`` as ``[{"type":"text","text":X,"cache_control":...}]`` while
the message is inside the system-and-3 window, and sends the bare string once
it leaves. With ``cache_control`` stripped the wrapper alone is
``[{"text":`` + ``,"type":"text"}]`` = 25 B. The text is unchanged.

The real loop runs here with prompt caching on (the envelope layout used for
OpenRouter), and consecutive requests are compared with the blackbox guard.
"""

import copy
from unittest.mock import patch

from agent.tool_dispatch_helpers import make_tool_result_message
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.agent.test_tool_call_incremental_persistence import (
    _make_agent,
    _mock_response,
    _mock_tool_call,
)


def _run_three_calls():
    agent = _make_agent()
    agent._use_prompt_caching = True
    agent._use_native_cache_layout = False
    sent: list[dict] = []
    queue = [
        _mock_response(content="checking one", finish_reason="tool_calls",
                       tool_calls=[_mock_tool_call(call_id="c1")]),
        _mock_response(content="checking two", finish_reason="tool_calls",
                       tool_calls=[_mock_tool_call(call_id="c2")]),
        _mock_response(content="checking three", finish_reason="tool_calls",
                       tool_calls=[_mock_tool_call(call_id="c3")]),
        _mock_response(content="DONE"),
    ]

    def _create(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs))
        return queue.pop(0)

    agent.client.chat.completions.create.side_effect = _create

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", '{"output": "ok"}', tc.id))

    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
    ):
        agent.run_conversation("run three searches, then reply DONE", conversation_history=[])
    return sent


def test_marker_window_leaves_messages_with_their_text_unchanged():
    sent = _run_three_calls()
    assert len(sent) == 4
    # Premise: the decorator really does wrap and later unwrap the same row,
    # otherwise this test proves nothing.
    wrapped_then_bare = False
    for prev, cur in zip(sent, sent[1:]):
        for a, b in zip(prev["messages"], cur["messages"]):
            if isinstance(a.get("content"), list) and isinstance(b.get("content"), str):
                assert len(a["content"]) == 1 and a["content"][0]["text"] == b["content"]
                wrapped_then_bare = True
    assert wrapped_then_bare

    for i, (prev, cur) in enumerate(zip(sent, sent[1:]), start=1):
        violations = compare(fingerprint_request(prev), fingerprint_request(cur))
        assert violations == [], (i, violations)
