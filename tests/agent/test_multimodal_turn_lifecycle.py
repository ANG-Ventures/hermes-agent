"""Turn-boundary lifecycle tests for native multimodal payloads."""

from __future__ import annotations

from agent.tool_dispatch_helpers import (
    _count_multimodal_image_parts,
    _degrade_prior_turn_multimodal_messages,
)


def _image_part(payload: str = "pixels") -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{payload}"},
    }


def test_prior_turn_tool_payload_degrades_to_exact_summary_without_source_mutation():
    source = [
        {
            "role": "tool",
            "name": "vision_analyze",
            "tool_call_id": "call_vision",
            "content": [
                {"type": "text", "text": "Image loaded for native inspection."},
                _image_part(),
            ],
            "api_content": "Exact prior wire text.",
            "_multimodal_text_summary": "Exact persisted vision summary.",
        }
    ]

    projected, degraded = _degrade_prior_turn_multimodal_messages(source)

    assert degraded == 1
    assert projected == [
        {
            "role": "tool",
            "name": "vision_analyze",
            "tool_call_id": "call_vision",
            "content": "Exact persisted vision summary.",
        }
    ]
    assert projected[0] is not source[0]
    assert "api_content" not in projected[0]
    assert source[0]["content"][1]["type"] == "image_url"
    assert source[0]["api_content"] == "Exact prior wire text."
    assert source[0]["_multimodal_text_summary"] == "Exact persisted vision summary."


def test_prior_turn_envelope_degrades_to_its_text_summary():
    source = [
        {
            "role": "tool",
            "tool_call_id": "call_vision",
            "content": {
                "_multimodal": True,
                "content": [
                    {"type": "text", "text": "Different immediate-turn text."},
                    _image_part(),
                ],
                "text_summary": "Envelope summary wins.",
            },
        }
    ]

    projected, degraded = _degrade_prior_turn_multimodal_messages(source)

    assert degraded == 1
    assert projected[0]["content"] == "Envelope summary wins."
    assert isinstance(source[0]["content"], dict)


def test_prior_turn_image_list_without_envelope_uses_persistence_projection():
    source = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please inspect this upload."},
                _image_part(),
            ],
        }
    ]

    projected, degraded = _degrade_prior_turn_multimodal_messages(source)

    assert degraded == 1
    assert projected[0]["content"] == "Please inspect this upload.\n[screenshot]"
    assert source[0]["content"][1]["type"] == "image_url"


def test_image_counter_finds_only_payload_bearing_messages():
    messages = [
        {"role": "user", "content": "plain"},
        {"role": "tool", "content": [{"type": "text", "text": "text only"}]},
        {
            "role": "tool",
            "content": [{"type": "text", "text": "one"}, _image_part("a")],
        },
        {
            "role": "tool",
            "content": {
                "_multimodal": True,
                "content": [_image_part("b")],
                "text_summary": "two",
            },
        },
    ]

    assert _count_multimodal_image_parts(messages) == 2
