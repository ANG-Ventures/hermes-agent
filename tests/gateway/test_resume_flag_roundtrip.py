from agent.message_sanitization import close_interrupted_tool_sequence
from gateway.run import _is_interrupt_close_tail
from hermes_state import SessionDB
from run_agent import AIAgent


def test_interrupt_close_flag_survives_session_db_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-interrupt-roundtrip"
    db.create_session(session_id=session_id, source="discord")

    persisted_user = {"role": "user", "content": "scan the repo"}
    db.append_message(session_id=session_id, role="user", content=persisted_user["content"])

    live_messages = [
        persisted_user,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "terminal", "arguments": '{"command":"rg TODO"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "tool_name": "terminal",
            "content": "src/a.py: TODO",
        },
    ]
    assert close_interrupted_tool_sequence(live_messages, None) is True

    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent.platform = "discord"
    agent._last_flushed_db_idx = 1
    agent._flushed_db_message_ids = {id(persisted_user)}
    agent._flushed_db_message_session_id = session_id

    agent._flush_messages_to_session_db(live_messages)

    reloaded = db.get_messages(session_id)
    assert reloaded[-1]["role"] == "assistant"
    assert reloaded[-1]["content"] == "Operation interrupted."
    assert reloaded[-1]["finish_reason"] == "interrupt_close"
    assert _is_interrupt_close_tail(reloaded) is True


def test_plain_operation_interrupted_text_is_not_interrupt_close(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-interrupt-control"
    db.create_session(session_id=session_id, source="discord")
    db.append_message(session_id=session_id, role="user", content="hello")
    db.append_message(
        session_id=session_id,
        role="assistant",
        content="Operation interrupted.",
        finish_reason=None,
    )

    reloaded = db.get_messages(session_id)
    assert reloaded[-1].get("finish_reason") is None
    assert _is_interrupt_close_tail(reloaded) is False
