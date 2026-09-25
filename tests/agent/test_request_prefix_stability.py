"""Protect the ordinary request-builder boundary from rewriting prior context."""
import copy
import json

from agent.anthropic_adapter import build_anthropic_kwargs


def test_consecutive_requests_preserve_sent_history_and_system_bytes():
    """Only an explicit upstream compaction may change the canonical input list."""
    history = [
        {"role": "system", "content": "stable instructions"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    before = copy.deepcopy(history)
    first = build_anthropic_kwargs(
        model="claude-sonnet-4-20250514", messages=history,
        tools=None, max_tokens=4096, reasoning_config=None,
    )
    next_input = copy.deepcopy(history) + [
        {"role": "assistant", "content": "new answer"},
        {"role": "user", "content": "third"},
    ]
    second = build_anthropic_kwargs(
        model="claude-sonnet-4-20250514", messages=next_input,
        tools=None, max_tokens=4096, reasoning_config=None,
    )
    assert history == before
    assert next_input[:len(before)] == before
    assert first["system"] == second["system"]
    assert json.dumps(first["messages"], sort_keys=True) == json.dumps(
        second["messages"][:len(first["messages"])], sort_keys=True
    )
