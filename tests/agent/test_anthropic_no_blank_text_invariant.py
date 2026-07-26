"""Invariant: no Anthropic conversion path may emit a whitespace-only text block.

The Messages input schema rejects blank text blocks ("messages: text content
blocks must contain non-whitespace text"). A single whitespace block anywhere
in the converted payload turns the WHOLE request into a deterministic HTTP 400
that the harness fans across the entire fallback chain — presenting as a total
provider outage (2026-07-25 incident: the synthetic leading user turn carried
" ", wedging a session permanently because the turn is re-prepended on every
conversion and never exists in the stored transcript).

Unit tests on individual call sites can't guard this class — the bug was even
PINNED as the expected value by two older tests. This suite asserts the
property over the converter's OUTPUT for a matrix of adversarial history
shapes, so any future placeholder/merge/coercion path that leaks a blank text
block fails here by construction.
"""

import pytest

from agent.anthropic_adapter import convert_messages_to_anthropic


def _assert_no_blank_text_blocks(amsgs):
    """The invariant: every text block in every message is non-whitespace."""
    for i, m in enumerate(amsgs):
        content = m.get("content")
        if isinstance(content, str):
            assert content.strip() != "", (
                f"message {i} ({m.get('role')}): whitespace-only string content"
            )
        elif isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    assert str(block.get("text", "")).strip() != "", (
                        f"message {i} ({m.get('role')}) block {j}: "
                        f"whitespace-only text block {block.get('text')!r}"
                    )


# History shapes chosen to drive every synthetic/coercion path in the
# converter: leading-assistant (synthetic user turn), empty assistant
# content (placeholder path), whitespace content strings, whitespace text
# blocks in content arrays, empty tool-call turns, replayed api_content
# blocks with blank text, and plain healthy shapes as controls.
_SHAPES = {
    "leading_assistant_summary": [
        {"role": "assistant", "content": "[Context compaction summary] earlier"},
        {"role": "user", "content": "continue"},
    ],
    "leading_assistant_with_system": [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "[Context compaction summary] earlier"},
        {"role": "user", "content": "continue"},
    ],
    "empty_assistant_content": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "again"},
    ],
    "whitespace_assistant_content": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "   \n\t  "},
        {"role": "user", "content": "again"},
    ],
    "whitespace_user_content": [
        {"role": "user", "content": "  \n "},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ],
    "whitespace_text_block_in_array": [
        {"role": "user", "content": [
            {"type": "text", "text": "  "},
            {"type": "text", "text": "real question"},
        ]},
    ],
    "assistant_all_blank_blocks": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "text", "text": ""},
            {"type": "text", "text": " \n"},
        ]},
        {"role": "user", "content": "next"},
    ],
    "empty_assistant_with_tool_call": [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_01", "function": {"name": "terminal", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_01", "content": "done"},
    ],
    "empty_tool_result": [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [
                {"id": "toolu_02", "function": {"name": "terminal", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_02", "content": ""},
        {"role": "user", "content": "and?"},
    ],
    "replayed_api_content_with_blank_text": [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "did it",
            "api_content": [
                {"type": "text", "text": "  ", "citations": None},
                {"type": "text", "text": "did it"},
            ],
        },
        {"role": "user", "content": "thanks"},
    ],
    "healthy_control": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "bye"},
    ],
}


@pytest.mark.parametrize("shape_name", sorted(_SHAPES))
def test_conversion_never_emits_blank_text_blocks(shape_name):
    _, amsgs = convert_messages_to_anthropic(list(_SHAPES[shape_name]))
    assert amsgs, f"{shape_name}: conversion produced no messages"
    _assert_no_blank_text_blocks(amsgs)


def test_combined_worst_case_history():
    """All adversarial shapes concatenated into one history still hold."""
    history = []
    for shape in _SHAPES.values():
        history.extend(dict(m) for m in shape)
    _, amsgs = convert_messages_to_anthropic(history)
    assert amsgs
    _assert_no_blank_text_blocks(amsgs)
