"""Serialized request-body byte budgeting and image remediation tests."""

from __future__ import annotations

import base64

import httpx
from openai import OpenAI

from agent.request_body_budget import (
    remediate_request_body,
    request_body_limit_for_provider,
    request_body_limit_from_error,
    serialized_request_body_size,
)
from providers import get_provider_profile
from providers.base import ProviderProfile


def test_estimator_matches_actual_openai_sdk_serialized_body_length():
    """The estimator tracks real compact JSON serialization, not a frozen size."""
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    kwargs = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "héllo"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(b"pixels").decode()
                        },
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "inspect",
                    "description": "Inspect the request",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "timeout": 3.0,
        "extra_body": {"reasoning": {"enabled": True}},
    }

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAI(
            api_key="test-key",  # gitleaks:allow
            base_url="https://example.invalid/v1",
            http_client=http_client,
        )
        client.chat.completions.create(**kwargs)

    assert serialized_request_body_size(kwargs) == len(captured["body"])


def test_anthropic_profile_sets_headroom_below_hard_ten_mib_limit():
    profile = get_provider_profile("anthropic")

    cap = profile.get_max_request_body_bytes("claude-opus-4-6")

    assert 9 * 1024 * 1024 < cap < 10 * 1024 * 1024


def test_provider_profiles_default_to_no_request_body_cap():
    assert ProviderProfile(name="test").get_max_request_body_bytes("model") is None


def test_custom_anthropic_transport_inherits_protocol_body_cap():
    cap = request_body_limit_for_provider(
        "claude-apx-1",
        "claude-opus-5",
        api_mode="anthropic_messages",
    )

    assert cap is not None
    assert 9 * 1024 * 1024 < cap < 10 * 1024 * 1024
    assert (
        request_body_limit_for_provider(
            "claude-apx-1",
            "claude-opus-5",
            api_mode="chat_completions",
        )
        is None
    )


def test_anthropic_error_cap_parser_retains_headroom():
    error = Exception("request body too large (max 10485760 bytes)")

    cap = request_body_limit_from_error(error)

    assert cap == int(10_485_760 * 0.95)


def test_remediation_evicts_oldest_image_first_with_placeholder():
    raw = b"not-an-image" * 5_000
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    request = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "old screenshot"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "new screenshot"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    before = serialized_request_body_size(request)

    result = remediate_request_body(request, max_body_bytes=before - 60_000)

    assert result.fits is True
    assert result.evicted_images == 1
    old_content = result.request_kwargs["messages"][0]["content"]
    new_content = result.request_kwargs["messages"][1]["content"]
    assert old_content[1] == {
        "type": "text",
        "text": "[image evicted: 0.1 MB screenshot, turn 1]",
    }
    assert new_content[1]["type"] == "image_url"
    assert request["messages"][0]["content"][1]["type"] == "image_url"


def test_remediation_reaches_anthropic_image_nested_in_fresh_tool_result():
    raw = b"not-an-image" * 5_000
    encoded = base64.b64encode(raw).decode()
    request = {
        "model": "claude-test",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_vision",
                        "content": [
                            {"type": "text", "text": "Fresh screenshot"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": encoded,
                                },
                            },
                        ],
                    }
                ],
            }
        ],
    }

    result = remediate_request_body(request, max_body_bytes=2_000)

    assert result.fits is True
    assert result.image_count == 1
    nested_content = result.request_kwargs["messages"][0]["content"][0]["content"]
    assert nested_content[1] == {
        "type": "text",
        "text": "[image evicted: 0.1 MB screenshot, turn 1]",
    }
