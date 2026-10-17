"""A resume notice must replay as its own user row, never fold into the old assistant turn
(class C, t_91b6a4c4).

Phase-1 measurement (t_38dd407a): 4 prefix breaks where an already-sent assistant turn grew by
"\\nGateway restarted mid-turn ..." / "Resuming after the restart ...". Durable rows of the live
session (20260921_002254_3f4af45e) show the shape: assistant ``interrupt_close`` closer, then the
resume turn's user row persisted with ``content=''`` and the recovery note in ``api_content``
(gateway/run_turn_runner.py persists the RAW, empty user text; the note is API-only), then the
resumed assistant reply. The gateway replay builder dropped the empty-content user row although
its sidecar is exactly what was sent, so the two assistant rows became adjacent and merged.

Real flush, real SessionDB reload, real gateway replay builder; blackbox comparator oracle.
"""

import copy
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
CLOSER = "Operation interrupted: waiting for model response (26.2s elapsed)."
RESUMED = "Gateway restarted mid-turn; picking up where I was."


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


def _run_turn(agent, user_text, history, reply, **kwargs):
    sent: list[dict] = []

    def _create(*args, **kw):
        sent.append(copy.deepcopy(kw))
        return _mock_response(content=reply)

    agent.client.chat.completions.create.side_effect = _create
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent


def test_resume_notice_append_only_prefix_stable(tmp_path):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "resume-notice-append-only"
    db = _attach_real_session_db(agent, db_path, session_id)
    now = time.time()
    # The interrupted turn as the gateway leaves it on disk.
    db.append_message(session_id, "user", "run the long job", timestamp=now - 120)
    db.append_message(session_id, "assistant", CLOSER, finish_reason="interrupt_close", timestamp=now - 60)

    # The resume turn: the model receives the note; the transcript keeps the RAW (empty) user text.
    note = build_resume_recovery_note("restart_timeout", "", interactive=True)
    sent_n = _run_turn(agent, note, _reload_replay(db_path, session_id), RESUMED,
                       persist_user_message="", persist_user_timestamp=now)
    assert sent_n[-1]["messages"][-1] == {"role": "user", "content": note} or (
        sent_n[-1]["messages"][-1]["role"] == "user" and note in str(sent_n[-1]["messages"][-1]["content"]))

    history = _reload_replay(db_path, session_id)
    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("next question")
    sent_n1 = _run_turn(agent, api_text, history, "ok",
                        persist_user_message=persist_text, persist_user_timestamp=persist_ts)

    violations = [
        v for v in compare(fingerprint_request(sent_n[-1]), fingerprint_request(sent_n1[0]), turn_boundary=True)
        if v["segment"] == "messages"
    ]
    assert violations == []
    assert append_only_breaks(sent_n[-1], sent_n1[0]) == []
    closers = [m for m in sent_n1[0]["messages"] if m.get("role") == "assistant" and CLOSER in str(m.get("content"))]
    assert closers and closers[0]["content"] == CLOSER  # the old assistant turn was not appended to
