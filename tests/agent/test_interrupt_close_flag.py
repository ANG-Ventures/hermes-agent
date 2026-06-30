from agent.message_sanitization import close_interrupted_tool_sequence


def _tool_tail_messages():
    return [
        {"role": "user", "content": "finish the migration"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "halfway done"},
    ]


def test_interrupt_close_tail_is_structurally_flagged():
    messages = _tool_tail_messages()

    assert close_interrupted_tool_sequence(messages) is True

    assert messages[-1] == {
        "role": "assistant",
        "content": "Operation interrupted.",
        "_interrupt_close": True,
    }


def test_plain_operation_interrupted_assistant_turn_is_distinguishable():
    normal_assistant_turn = {"role": "assistant", "content": "Operation interrupted."}

    assert normal_assistant_turn.get("_interrupt_close") is not True
