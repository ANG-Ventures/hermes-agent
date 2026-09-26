"""A pre-delivery stream failure the main loop retries is not an ERROR.

``interruptible_streaming_api_call`` propagates non-transient stream errors
(429s included) to the main retry loop, which owns credential rotation,
backoff, fallback and the final-failure ERROR (``API call failed after N
retries``).  Logging ERROR at the helper fired once per attempt even when the
retry then succeeded, so error-repair kept filing rate-limit cards
(signature a5cfcdf9df963a1a, 885 hits).
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _chunk(content=None, finish_reason=None, model=None):
    delta = SimpleNamespace(
        content=content, tool_calls=None, reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


class _RateLimitError(Exception):
    status_code = 429

    def __str__(self):
        return (
            "Error code: 429 - {'type': 'error', 'error': {'type': "
            "'rate_limit_error', 'message': \"This request would exceed "
            "your account's rate limit. Please try again later.\"}}"
        )


@patch("run_agent.AIAgent._create_request_openai_client")
@patch("run_agent.AIAgent._close_request_openai_client")
def test_retried_then_succeeded_429_emits_no_error_log(mock_close, mock_create, caplog):
    from run_agent import AIAgent

    chunks = [
        _chunk(content="ok", finish_reason="stop", model="m"),
        SimpleNamespace(
            choices=[], model=None,
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    ]
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [_RateLimitError(), iter(chunks)]
    mock_create.return_value = mock_client

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False

    with caplog.at_level(logging.DEBUG, logger="agent.chat_completion_helpers"):
        # Attempt 1: the 429 propagates to the main-loop caller.
        with pytest.raises(_RateLimitError):
            agent._interruptible_streaming_api_call({})
        # Attempt 2 (the main loop's retry): succeeds.
        response = agent._interruptible_streaming_api_call({})

    assert response.choices[0].message.content == "ok"
    helper = [r for r in caplog.records if r.name == "agent.chat_completion_helpers"]
    assert not [r for r in helper if r.levelno >= logging.ERROR], [
        (r.levelname, r.getMessage()) for r in helper if r.levelno >= logging.ERROR
    ]
    # Still visible (with traceback) at WARNING for diagnosis.
    assert any(
        r.levelno == logging.WARNING
        and "Streaming failed before delivery" in r.getMessage()
        and r.exc_info
        for r in helper
    )
