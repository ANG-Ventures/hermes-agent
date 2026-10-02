"""Turn N+1 must replay turn N's history byte-identical after a session reload.

Live finding (card t_a17e2305, rig t_65de70fe, 2026-09-30): the blackbox
prefix guard logged a ``messages`` mutation at seq 0 of EVERY turn that
followed a tool turn, because the history the gateway reloads from state.db
differed from what the previous turn sent:

1. 18 bytes on every tool row (159->141, 155->137): the live tool message
   carries ``"name": "<tool>"`` (``make_tool_result_message``), the chat
   transport sends it, and the reloaded row came back without it.
2. A mid-turn /steer appended to an already-flushed tool row (448->137): the
   flush is append-only, so the steer marker never reached state.db. The
   steer vanished from history and the cached prefix broke. The fix stamps the
   sent bytes as the row's ``api_content`` sidecar.

#1555's cache-prefix test covers the within-turn invariant with the flush
patched out. These tests run the real flush against a real SessionDB, reload
the transcript the way the gateway does, and compare the last request of
turn N with the first request of turn N+1 using the blackbox comparator.
"""

import copy

from agent.prompt_builder import STEER_MARKER_OPEN, format_steer_marker
from agent.tool_dispatch_helpers import make_tool_result_message
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
    _mock_tool_call,
)
from unittest.mock import patch

STEER = "STEER-2B: in your final reply also include the word BANANA-2B."


def _reload(db_path, session_id):
    # Same call as gateway/session.py's live-replay transcript load.
    db = SessionDB(db_path=db_path)
    try:
        return db.get_messages_as_conversation(
            session_id, include_timestamp=True, repair_alternation=True
        )
    finally:
        db.close()


def _run_turn(agent, user_text, history, responses, *, steer=None):
    sent: list[dict] = []
    queue = list(responses)

    def _create(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs))
        return queue.pop(0)

    agent.client.chat.completions.create.side_effect = _create

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        if steer:
            assert agent.steer(steer)
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", '{"output": "ok"}', tc.id))
            # The real sequential executor flushes each result as it lands,
            # BEFORE the end-of-batch steer delivery mutates it.
            agent._flush_messages_to_session_db(messages)
        agent._apply_pending_steer_to_tool_results(messages, len(assistant_message.tool_calls))

    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
    ):
        result = agent.run_conversation(user_text, conversation_history=history)
    return result, sent


def _turn_boundary_violations(prev_request, next_request):
    prev_fp = fingerprint_request(prev_request)
    next_fp = fingerprint_request(next_request)
    return [v for v in compare(prev_fp, next_fp, turn_boundary=True) if v["segment"] == "messages"]


def _two_turns(tmp_path, *, steer):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "steer-cross-turn"
    _attach_real_session_db(agent, db_path, session_id)

    result_n, sent_n = _run_turn(
        agent,
        "run the command, then reply DONE",
        [],
        [
            _mock_response(content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(name="web_search", call_id="c1")]),
            _mock_response(content="DONE"),
        ],
        steer=steer,
    )
    assert result_n["final_response"] == "DONE"

    history = _reload(db_path, session_id)
    _result, sent_n1 = _run_turn(agent, "next question", history, [_mock_response(content="ok")])
    return sent_n, sent_n1, history


def test_tool_turn_reload_replays_tool_rows_byte_identical(tmp_path):
    sent_n, sent_n1, _history = _two_turns(tmp_path, steer=None)
    assert _turn_boundary_violations(sent_n[-1], sent_n1[0]) == []


def test_steered_turn_reload_keeps_steer_and_prefix(tmp_path):
    sent_n, sent_n1, history = _two_turns(tmp_path, steer=STEER)

    # The steer reached the model in turn N ...
    tool_sent = [m for m in sent_n[-1]["messages"] if m.get("role") == "tool"]
    assert tool_sent and tool_sent[-1]["content"].endswith(format_steer_marker(STEER))
    # ... is durable user intent in the reloaded history: the clean tool
    # output stays in ``content`` (append-only) and the sent bytes ride the
    # api_content sidecar that replay substitutes ...
    tool_rows = [m for m in history if m.get("role") == "tool"]
    assert tool_rows[-1]["content"] == '{"output": "ok"}'
    assert tool_rows[-1]["api_content"] == tool_sent[-1]["content"]
    assert STEER_MARKER_OPEN in tool_rows[-1]["api_content"]
    # ... and turn N+1 starts on the same cached prefix.
    assert _turn_boundary_violations(sent_n[-1], sent_n1[0]) == []
