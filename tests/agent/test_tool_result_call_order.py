"""Pre-send sanitizer orders each tool-result run by its tool_calls index.

The claude-bpr native result relay fails closed (400 "received tool results
out of tool_calls order") when the tool messages after an assistant
tool_calls turn are not in that turn's tool_calls order.
"""

from agent.agent_runtime_helpers import (
    order_tool_results_by_call_index,
    sanitize_api_messages,
)


def _tc(cid, name="memory"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _tool(cid, content="ok"):
    return {"role": "tool", "name": "memory", "tool_call_id": cid, "content": content}


def _tool_run_ids(messages):
    idx = max(
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    out = []
    for m in messages[idx + 1:]:
        if m.get("role") != "tool":
            break
        out.append(m["tool_call_id"])
    return out


def test_live_shape_reordered_on_wire_and_input_untouched():
    # 2026-09-25 14:14:05 bg-review: expected [U1, LE, 011d], got [011d, U1, LE].
    history = [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b"), _tc("c")]},
        _tool("c", "invalid"),
        _tool("a"),
        _tool("b"),
    ]
    snapshot = [dict(m) for m in history]

    wire = sanitize_api_messages(history)

    assert _tool_run_ids(wire) == ["a", "b", "c"]
    assert [m["content"] for m in wire[2:]] == ["ok", "ok", "invalid"]
    assert history == snapshot  # persisted list not mutated


def test_already_ordered_returns_same_list():
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool("a"),
        _tool("b"),
    ]
    out, n = order_tool_results_by_call_index(msgs)
    assert out is msgs
    assert n == 0


def test_every_run_ordered_independently():
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool("b"),
        _tool("a"),
        {"role": "assistant", "content": "", "tool_calls": [_tc("c"), _tc("d"), _tc("e")]},
        _tool("e"),
        _tool("d"),
        _tool("c"),
    ]
    out, n = order_tool_results_by_call_index(msgs)
    assert n == 2
    assert [m.get("tool_call_id") for m in out if m["role"] == "tool"] == ["a", "b", "c", "d", "e"]


def test_missing_results_stubbed_in_call_order():
    # The stub flush emits in sorted-id order; the run must still follow tool_calls.
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("z"), _tc("m"), _tc("a")]},
    ]
    wire = sanitize_api_messages(msgs)
    assert _tool_run_ids(wire) == ["z", "m", "a"]
