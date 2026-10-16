"""A user turn absorbed into a preceding unanswered user row must replay with the bytes it was sent
with (class B, t_91b6a4c4).

Phase-1 measurement (t_38dd407a): one break where ``[Thu 2026-10-15 17:05:01 PDT] [Ace] rm`` replayed as
``[Ace] rm``. Durable rows (session 20261014_141420_25c80e3e, ids 1948471/1948472): an unanswered
``internal_notification`` user row (a kanban wake) directly followed by the human turn, each with its
stamped ``api_content`` sidecar. Real flush, real SessionDB reload, real gateway replay builder.
"""

import copy
import time
from unittest.mock import patch

import pytest

import gateway.run as gateway_run
from gateway.run import _build_gateway_agent_history
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.run_agent.append_only_oracle import append_only_breaks
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
)

_TS_ON = {"gateway": {"message_timestamps": {"enabled": True}}}
WAKE = "[kanban] Task t_f593626d review requested changes (BLOCK); implementation is not approved."


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
        return _mock_response(content="ok")

    agent.client.chat.completions.create.side_effect = _create
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent


def test_absorbed_user_turn_keeps_its_timestamp_prefix_stable(tmp_path):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "absorbed-user-ts"
    db = _attach_real_session_db(agent, db_path, session_id)
    now = time.time()
    db.append_message(session_id, "user", "hello", timestamp=now - 300)
    db.append_message(session_id, "assistant", "hi", timestamp=now - 290)
    # The unanswered wake row, as the gateway persists an internal turn: clean content, stamped sidecar.
    wake_api, wake_clean, wake_ts = gateway_run._compose_inbound_user_turn(WAKE, now - 7)
    db.append_message(session_id, "user", wake_clean, timestamp=wake_ts, api_content=wake_api,
                      display_kind="internal_notification")

    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("[Ace] rm", now)
    sent_n = _run_turn(agent, api_text, _reload_replay(db_path, session_id),
                       persist_user_message=persist_text, persist_user_timestamp=persist_ts)
    assert api_text in str(sent_n[-1]["messages"][-1]["content"])

    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("next")
    sent_n1 = _run_turn(agent, api_text, _reload_replay(db_path, session_id),
                        persist_user_message=persist_text, persist_user_timestamp=persist_ts)

    violations = [
        v for v in compare(fingerprint_request(sent_n[-1]), fingerprint_request(sent_n1[0]), turn_boundary=True)
        if v["segment"] == "messages"
    ]
    assert violations == []
    assert append_only_breaks(sent_n[-1], sent_n1[0]) == []
