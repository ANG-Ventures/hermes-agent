"""A /steer row must keep its bare bytes when replay output is replayed again (t_fd02a3a1).

Live finding (claude-alr, 2026-10-17/18): 81 misses/day, each a seq=0 prefix_mutations row that
flips ONE mid-history message by exactly +/-30 B, often right after an interrupted background
review fork. Replaying the live state.db rows named the message: a ``[OUT-OF-BAND USER
MESSAGE ...]`` steer row, sent bare, gained a ``[Sun 2026-10-18 ...] `` stamp on the next turn
and lost it again on the turn after.

Cause: ``_build_replay_entry`` dropped ``display_kind``. The steer exemption in
``_build_gateway_agent_history`` (class A, t_91b6a4c4) keys on it, so an entry that went through
the builder once and is fed back in gets stamped. The in-band queued follow-up does exactly that:
``_run_agent(history=result["messages"])`` (a message that arrived while the turn was finishing,
which is also what cancels the review fork). Compaction persisting those entries stores the steer
row without its kind, with the same result on every later reload.

Harness as test_steer_timestamp_append_only.py: real flush, real SessionDB reload, real gateway
replay builder with timestamps on; oracle = blackbox comparator at the turn boundary.
"""

import copy
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import gateway.run as gateway_run
from agent.prompt_builder import STEER_MARKER_OPEN
from agent.tool_dispatch_helpers import make_tool_result_message
from gateway.run import _build_gateway_agent_history
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.run_agent.append_only_oracle import append_only_breaks
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
    _mock_tool_call,
)

STEER = "STEER-Q: also include the word MANGO-Q in the final reply."
_TS_ON = {"gateway": {"message_timestamps": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _timestamps_enabled(monkeypatch):
    # A configured zone: the suite's TZ=UTC env without tzset() makes server-local rendering flip
    # mid-test, which would show up as a stamp change on every user row, not just the steer row.
    monkeypatch.setattr("hermes_time.get_timezone", lambda: ZoneInfo("America/Los_Angeles"))
    with patch.object(gateway_run, "_load_gateway_config", return_value=_TS_ON):
        yield


def _run_turn(agent, user_text, history, responses, *, steer=None, **kwargs):
    sent: list[dict] = []
    queue = list(responses)

    def _create(*args, **kw):
        sent.append(copy.deepcopy(kw))
        return queue.pop(0)

    agent.client.chat.completions.create.side_effect = _create

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        if steer:
            assert agent.steer(steer)
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message("web_search", '{"output": "ok"}', tc.id))
            agent._flush_messages_to_session_db(messages)
        agent._apply_pending_steer_to_tool_results(messages, len(assistant_message.tool_calls))

    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
    ):
        result = agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent, result


def _turn_text(text):
    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn(text)
    return api_text, {"persist_user_message": persist_text, "persist_user_timestamp": persist_ts}


def _steer_rows(request):
    return [m for m in request["messages"] if m.get("role") == "user" and STEER in str(m.get("content"))]


def test_queued_followup_replays_steer_bare(tmp_path):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "steer-queued-followup"
    _attach_real_session_db(agent, db_path, session_id)

    # Turn N: a /steer lands mid-turn and is sent bare.
    api_text, persist = _turn_text("run it, then reply DONE")
    sent_n, _ = _run_turn(
        agent, api_text, [],
        [
            _mock_response(content="", finish_reason="tool_calls",
                           tool_calls=[_mock_tool_call(name="web_search", call_id="c1")]),
            _mock_response(content="DONE"),
        ],
        steer=STEER, **persist,
    )
    assert _steer_rows(sent_n[-1])[0]["content"].startswith(STEER_MARKER_OPEN)

    # Turn N+1: ordinary turn, history reloaded from state.db (the class-A path, already stable).
    db = SessionDB(db_path=db_path)
    try:
        rows = db.get_messages_as_conversation(session_id, include_timestamp=True, repair_alternation=True)
    finally:
        db.close()
    history, _ = _build_gateway_agent_history(rows, inject_timestamps=True)
    api_text, persist = _turn_text("next question")
    sent_n1, result_n1 = _run_turn(agent, api_text, history, [_mock_response(content="ok")], **persist)

    # Turn N+2: a message queued during N+1 drains in-band; run_turn passes result["messages"]
    # back in as history, so it goes through the replay builder a second time.
    history2, _ = _build_gateway_agent_history(result_n1["messages"], inject_timestamps=True)
    api_text, persist = _turn_text("queued while you were finishing")
    sent_n2, _ = _run_turn(agent, api_text, history2, [_mock_response(content="ok2")], **persist)

    violations = [
        v for v in compare(fingerprint_request(sent_n1[-1]), fingerprint_request(sent_n2[0]), turn_boundary=True)
        if v["segment"] == "messages"
    ]
    assert violations == []
    assert append_only_breaks(sent_n1[-1], sent_n2[0]) == []
    assert _steer_rows(sent_n2[0])[0]["content"] == _steer_rows(sent_n[-1])[0]["content"]
