"""A resume note delivered after an interrupted tool result must stay in the replay (class D, t_91b6a4c4).

Phase-1 measurement (t_38dd407a): one break where an earlier user message lost a text block. Blackbox
(session 20261012_014728_1a97ee71, 2026-10-16 05:00:11) logged request index 640 shrinking 1307 -> 467 B:
the Anthropic user message carrying the interrupted turn's ``tool_result`` plus the 808 B resume note
(durable row 1962891: ``content=''``, note in ``api_content``) replayed with the tool_result alone, because
the gateway replay builder dropped the sidecar-only user row. Same root cause as class C, reached from a
tool row instead of an assistant closer.
"""

import copy
import json
import time
from unittest.mock import patch

import pytest

import gateway.run as gateway_run
from gateway.run import _build_gateway_agent_history, build_resume_recovery_note
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.run_agent.append_only_oracle import append_only_breaks
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
)

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


def _run_turn(agent, user_text, history, **kwargs):
    sent: list[dict] = []

    def _create(*args, **kw):
        sent.append(copy.deepcopy(kw))
        return _mock_response(content="Resumed; muting the beeper next.")

    agent.client.chat.completions.create.side_effect = _create
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent


def test_resume_note_after_tool_result_append_only(tmp_path):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "resume-after-tool-append-only"
    db = _attach_real_session_db(agent, db_path, session_id)
    now = time.time()
    call = {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"q": "ups"})}}
    db.append_message(session_id, "user", "mute the UPS beeper", timestamp=now - 120)
    db.append_message(session_id, "assistant", "", tool_calls=[call], finish_reason="tool_calls", timestamp=now - 110)
    db.append_message(session_id, "tool", '{"output": "OK audible_alarm enabled -> disabled"}',
                      tool_call_id="c1", tool_name="web_search", timestamp=now - 100)

    note = build_resume_recovery_note("shutdown_timeout", "", interactive=True)
    sent_n = _run_turn(agent, note, _reload_replay(db_path, session_id),
                       persist_user_message="", persist_user_timestamp=now)
    assert any(m.get("role") == "user" and note in str(m.get("content")) for m in sent_n[-1]["messages"])

    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("what other cases aren't handled?")
    sent_n1 = _run_turn(agent, api_text, _reload_replay(db_path, session_id),
                        persist_user_message=persist_text, persist_user_timestamp=persist_ts)

    violations = [
        v for v in compare(fingerprint_request(sent_n[-1]), fingerprint_request(sent_n1[0]), turn_boundary=True)
        if v["segment"] == "messages"
    ]
    assert violations == []
    assert append_only_breaks(sent_n[-1], sent_n1[0]) == []
    assert any(m.get("role") == "user" and note in str(m.get("content")) for m in sent_n1[0]["messages"])
