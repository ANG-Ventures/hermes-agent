"""Null containers are absent; null counts remain unknown downstream."""

import pytest

from agent.usage_pricing import normalize_usage
from hermes_state import SessionDB
from plugins.observability.langfuse import (
    _canonical_usage_and_cost,
    _unknown_usage_details,
)


@pytest.mark.parametrize(
    "details, expected_unknown",
    [
        pytest.param({}, False, id="absent-container"),
        pytest.param({"prompt_tokens_details": None}, False, id="null-container"),
        pytest.param(
            {"prompt_tokens_details": {"cached_tokens": None}},
            True, id="inner-null-count",
        ),
        pytest.param(
            {"cache_read_tokens_unavailable": True},
            True, id="explicit-unavailable",
        ),
    ],
)
def test_null_container_sessiondb_and_langfuse(tmp_path, details, expected_unknown):
    usage = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 50, **details},
        api_mode="chat_completions",
    )
    store = SessionDB(db_path=tmp_path / "state.db")
    try:
        store.create_session("s", source="cli")
        store.update_token_counts(
            "s", input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            input_tokens_unknown=usage.input_tokens_unknown,
            output_tokens_unknown=usage.output_tokens_unknown,
            cache_read_tokens_unknown=usage.cache_read_tokens_unknown,
            cache_write_tokens_unknown=usage.cache_write_tokens_unknown,
            usage_unknown=usage.usage_unknown,
        )
        row = store.get_session("s")
        assert row is not None
        assert bool(row["input_tokens_unknown"]) is expected_unknown
        assert bool(row["cache_read_tokens_unknown"]) is expected_unknown
        assert not row["cache_write_tokens_unknown"]
    finally:
        store.close()

    exported, _ = _canonical_usage_and_cost(
        usage, provider="anthropic", model="claude-sonnet-4-5", base_url="",
    )
    unknown = _unknown_usage_details(usage)
    assert exported["output"] == 50
    if expected_unknown:
        assert "input" not in exported
        assert unknown["input"] is True
        assert unknown["cache_read_input_tokens"] is True
    else:
        assert exported == {"input": 100, "output": 50}
        assert unknown == {}
