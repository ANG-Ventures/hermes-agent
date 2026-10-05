"""Gateway replay must resend the bytes the previous turn sent (card t_f98bc0c5).

Live finding (Apollo default gateway, claude-alr, 2026-10-04): 31% <5m cache
misses, each 0-2 s after a blackbox ``prefix_mutations`` row. Three classes:

* B, -117 B: a Discord user row. The sent bytes (``api_content`` sidecar) are
  ``[ts] [Triggering message id: ...]\\n\\n<text>``; the persisted content is
  ``<text>`` (the note is stripped for transcripts). Replay rendered
  ``[ts] <text>``, saw the sidecar did not start with it, dropped the sidecar,
  and sent 117 B less (the JSON-escaped note).
* C, ~-1575 B: the same drop, with a truncated mem0 ``<memory-context>``
  block behind the text.
* A, +30 B on msg[0]: a compaction summary row is sent bare at compaction,
  then stamped ``[ts] `` on every reload.

Harness as test_followup_timestamp_cross_turn_prefix.py: real inbound
composition, real flush, SessionDB reload through the gateway replay builder,
blackbox comparator with ``turn_boundary=True``.
"""

import copy
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _build_gateway_agent_history
from gateway.session import SessionSource
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
)

_TS_ON = {"gateway": {"message_timestamps": {"enabled": True}}}
# Shape of the live block: a truncated mem0 prefetch rides behind the text.
MEMORY_BLOCK = (
    "<memory-context>\n[System note: The following is recalled memory context, NOT new user"
    " input.]\n\n[mem0 memory prefetch output truncated - 12,415 chars]\n--- head ---\n"
    + "fact " * 200 + "\n</memory-context>"
)
LCM_SUMMARY = (
    "[Durable Summary (d2, node 1324)]\n# Consolidated summary\n- the user asked for X\n"
    "[Expand for details: lcm_expand node 1324]"
)


@pytest.fixture(autouse=True)
def _timestamps_enabled(monkeypatch):
    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: True)
    with patch.object(gateway_run, "_load_gateway_config", return_value=_TS_ON):
        yield


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(group_sessions_per_user=False)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    return runner


async def _discord_turn(text: str, message_id: str):
    """The gateway's inbound composition for a Discord message: (api_text, persist_text, ts)."""
    runner = _runner()
    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="dm", user_id="u1")
    event = MessageEvent(text=text, source=source, message_id=message_id)
    event.timestamp = datetime.now(timezone.utc) - timedelta(seconds=30)
    model_text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    return runner._hmwa_apply_message_timestamp(event, model_text)


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


def _boundary_violations(prev_request, next_request):
    return [
        v for v in compare(fingerprint_request(prev_request), fingerprint_request(next_request), turn_boundary=True)
        if v["segment"] == "messages"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_memory", [False, True], ids=["B-note", "C-note+memory"])
async def test_discord_note_sidecar_survives_reload(tmp_path, with_memory):
    agent = _make_agent()
    db_path, session_id = tmp_path / "state.db", "discord-note-cross-turn"
    _attach_real_session_db(agent, db_path, session_id)

    api_text, persist_text, persist_ts = await _discord_turn("why do we need a token rotation?", "1556380900902576248")
    assert "[Triggering message id:" in api_text and "[Triggering message id:" not in persist_text
    if with_memory:  # memory prefetch rides the sent user message only
        api_text = f"{api_text}\n\n{MEMORY_BLOCK}"
    sent_n = _run_turn(
        agent, api_text, [], persist_user_message=persist_text, persist_user_timestamp=persist_ts,
    )

    sent_n1 = _run_turn(agent, "next question", _reload_replay(db_path, session_id))
    assert _boundary_violations(sent_n[-1], sent_n1[0]) == []


def test_compaction_summary_row_replays_unstamped(tmp_path):
    agent = _make_agent()
    db_path, session_id = tmp_path / "state.db", "summary-cross-turn"
    db = _attach_real_session_db(agent, db_path, session_id)

    # The compaction rewrite persists the summary row (no flag, as LCM writes it) and the
    # compaction turn's request carries it bare at msg[0].
    stamp = time.time() - 600
    compacted = [
        {"role": "user", "content": LCM_SUMMARY, "timestamp": stamp},
        {"role": "assistant", "content": "ack", "timestamp": stamp + 1},
    ]
    for row in compacted:
        db.append_message(session_id, row["role"], row["content"], timestamp=row["timestamp"])
    agent._last_flushed_db_idx = len(compacted)
    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn("after compaction")
    sent_n = _run_turn(
        agent, api_text, [dict(m, _db_persisted=True) for m in compacted],
        persist_user_message=persist_text, persist_user_timestamp=persist_ts,
    )
    assert sent_n[-1]["messages"][1]["content"] == LCM_SUMMARY  # [0] is the system prompt

    sent_n1 = _run_turn(agent, "next question", _reload_replay(db_path, session_id))
    assert _boundary_violations(sent_n[-1], sent_n1[0]) == []
