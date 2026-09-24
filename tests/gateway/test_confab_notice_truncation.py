"""Responses history truncation must not spend content slots on UI-only notices."""

import pytest

from agent.confab_notice import CONFAB_NOTICE_KEY, is_metadata_only_tool_notice
from gateway.platforms.api_server import _auto_truncate_response_history


@pytest.mark.parametrize("position", [1, 2])
def test_tool_event_does_not_consume_content_window(position):
    content = [{"role": "user", "content": f"turn {i}"} for i in range(3)]
    event = {
        "role": "system", "content": "", "display_kind": "confab_notice",
        "display_metadata": {CONFAB_NOTICE_KEY: {
            "version": 1, "kind": "tool_call_as_text", "request_id": "window-test",
            "scope": "visible", "grammar": "inbound",
        }},
    }
    assert is_metadata_only_tool_notice(event)
    history = list(content)
    history.insert(position, event)
    trimmed = _auto_truncate_response_history(history, limit=2)
    assert [m for m in trimmed if not is_metadata_only_tool_notice(m)] == content[1:]
    assert event in trimmed


def test_tool_event_does_not_displace_preserved_summary():
    summary = {"role": "user", "content": "compacted", "_compressed_summary": True}
    event = {
        "role": "system", "content": "", "display_kind": "confab_notice",
        "display_metadata": {CONFAB_NOTICE_KEY: {
            "version": 1, "kind": "tool_call_as_text", "request_id": "summary-window",
            "scope": "visible", "grammar": "inbound",
        }},
    }
    latest = {"role": "user", "content": "latest"}
    history = [summary, {"role": "assistant", "content": "old"}, event, latest]
    assert _auto_truncate_response_history(history, limit=2) == [summary, event, latest]
