"""A mid-turn /steer row must replay with the bytes it was sent with (class A, t_91b6a4c4).

Phase-1 prefix-stability measurement (t_38dd407a, 1341 claude-alr pairs): 14 breaks were a
``[OUT-OF-BAND USER MESSAGE ...]`` steer row sent bare on the turn it arrived, then replayed
with a ``[Thu 2026-10-15 17:37:04 PDT] `` prefix on the next request. The steer row is
persisted bare (``display_kind=steer``, no ``api_content``) and the gateway replay builder
rendered a timestamp onto every user row, steer rows included.

Real flush, real SessionDB reload, real gateway replay builder with timestamps enabled; the
oracle is the blackbox comparator at the turn boundary.
"""

import copy
from unittest.mock import patch

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

STEER = "STEER-A: also include the word MANGO-A in the final reply."
_TS_ON = {"gateway": {"message_timestamps": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _timestamps_enabled():
    with patch.object(gateway_run, "_load_gateway_config", return_value=_TS_ON):
        yield


def _reload_replay(db_path, session_id):
    db = SessionDB(db_path=db_path)
    try:
        rows = db.get_messages_as_conversation(session_id, include_timestamp=True, repair_alternation=True)
    finally:
        db.close()
    history, _observed = _build_gateway_agent_history(rows, inject_timestamps=True)
    return history


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
        agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent


def test_steer_timestamp_replay_is_prefix_stable(tmp_path):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "steer-ts-append-only"
    _attach_real_session_db(agent, db_path, session_id)

    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("run it, then reply DONE")
    sent_n = _run_turn(
        agent, api_text, [],
        [
            _mock_response(content="", finish_reason="tool_calls",
                           tool_calls=[_mock_tool_call(name="web_search", call_id="c1")]),
            _mock_response(content="DONE"),
        ],
        steer=STEER, persist_user_message=persist_text, persist_user_timestamp=persist_ts,
    )
    steer_sent = [m for m in sent_n[-1]["messages"] if m.get("role") == "user" and STEER in str(m.get("content"))]
    assert steer_sent and steer_sent[0]["content"].startswith(STEER_MARKER_OPEN)

    history = _reload_replay(db_path, session_id)
    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("next question")
    sent_n1 = _run_turn(agent, api_text, history, [_mock_response(content="ok")],
                        persist_user_message=persist_text, persist_user_timestamp=persist_ts)

    violations = [
        v for v in compare(fingerprint_request(sent_n[-1]), fingerprint_request(sent_n1[0]), turn_boundary=True)
        if v["segment"] == "messages"
    ]
    assert violations == []
    assert append_only_breaks(sent_n[-1], sent_n1[0]) == []
    replayed = [m for m in sent_n1[0]["messages"] if m.get("role") == "user" and STEER in str(m.get("content"))]
    assert replayed[0]["content"] == steer_sent[0]["content"]
