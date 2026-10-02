"""A gateway follow-up turn must replay byte-identical after a session reload.

Live finding (card t_29abfaf6, rig t_65de70fe, 2026-09-30): with
gateway.message_timestamps enabled, a leftover /steer delivered as the next
turn was sent as bare text (82 chars, 110 B canonical) but replayed with the
``[Wed 2026-09-30 19:49:09 PDT] `` prefix (140 B). The recursive follow-up in
``GatewayRunner._run_agent_inner`` handed the text straight to ``_run_agent``
without the timestamp composition a fresh inbound turn gets, so the agent
stamped the row with wall-clock time and replay rendered a prefix the turn
never sent. Blackbox logged it as a -30 B ``messages`` mutation at the next
turn's seq 0 (41 rows/24 h on Apollo).

Same harness as test_steer_persist_cross_turn_prefix.py: real flush, real
SessionDB reload through the gateway's replay builder, and the blackbox
comparator with ``turn_boundary=True``.
"""

import ast
import copy
import inspect
import textwrap
import time
from unittest.mock import patch

import pytest

import gateway.run as gateway_run
from gateway.run import _build_gateway_agent_history
from plugins.blackbox.prefix_guard import compare, fingerprint_request
from tests.agent.test_tool_call_incremental_persistence import (
    SessionDB,
    _attach_real_session_db,
    _make_agent,
    _mock_response,
)

STEER = "STEER-2D: in your final reply for the current task, also include the word KIWI-2D."
_TS_ON = {"gateway": {"message_timestamps": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _timestamps_enabled():
    with patch.object(gateway_run, "_load_gateway_config", return_value=_TS_ON):
        yield


def _reload_replay(db_path, session_id):
    # Same pair the gateway uses: transcript load, then the replay builder
    # that renders the stored send time as the user-row prefix.
    db = SessionDB(db_path=db_path)
    try:
        rows = db.get_messages_as_conversation(
            session_id, include_timestamp=True, repair_alternation=True
        )
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
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation(user_text, conversation_history=history, **kwargs)
    return sent


def _boundary_violations(prev_request, next_request):
    return [
        v
        for v in compare(
            fingerprint_request(prev_request),
            fingerprint_request(next_request),
            turn_boundary=True,
        )
        if v["segment"] == "messages"
    ]


def _three_turns(tmp_path, compose_followup):
    agent = _make_agent()
    db_path = tmp_path / "state.db"
    session_id = "followup-cross-turn"
    _attach_real_session_db(agent, db_path, session_id)

    # Turn N: a fresh inbound message, composed the way _handle_message_with_agent does.
    api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn(
        "Reply with exactly DONE-2C", time.time() - 30
    )
    _run_turn(
        agent, api_text, [],
        persist_user_message=persist_text, persist_user_timestamp=persist_ts,
    )

    # Turn N+1: the leftover /steer delivered as the next turn (no event).
    history = _reload_replay(db_path, session_id)
    if compose_followup:
        api_text, persist_text, persist_ts = gateway_run._compose_inbound_user_turn(STEER)
        sent_n1 = _run_turn(
            agent, api_text, history,
            persist_user_message=persist_text, persist_user_timestamp=persist_ts,
        )
    else:
        # Pre-fix call site: bare text, no persist override.
        sent_n1 = _run_turn(agent, STEER, history)

    # Turn N+2: reload again and start the next turn.
    history = _reload_replay(db_path, session_id)
    sent_n2 = _run_turn(agent, "Reply with exactly QUEUED-2E", history)
    return sent_n1, sent_n2


def test_bare_followup_replays_a_timestamp_prefix_it_never_sent(tmp_path):
    """Names the delta: the old call site breaks the prefix by the prefix length."""
    sent_n1, sent_n2 = _three_turns(tmp_path, compose_followup=False)
    violations = _boundary_violations(sent_n1[-1], sent_n2[0])
    assert len(violations) == 1
    v = violations[0]
    assert v["kind"] == "mutation"
    # Sent bare, replayed with "[Wed 2026-09-30 19:49:09 PDT] " (30 B in the
    # rig; the zone abbreviation length varies by host). The row grows by
    # exactly the rendered prefix.
    replayed = [m["content"] for m in sent_n2[0]["messages"] if m.get("role") == "user"]
    steer_replayed = next(c for c in replayed if c.endswith(STEER))
    assert steer_replayed.startswith("[") and steer_replayed != STEER
    prefix_bytes = len((steer_replayed[: -len(STEER)]).encode())
    assert v["bytes_after"] - v["bytes_before"] == prefix_bytes


def test_composed_followup_reload_keeps_prefix(tmp_path):
    sent_n1, sent_n2 = _three_turns(tmp_path, compose_followup=True)
    steer_sent = sent_n1[-1]["messages"][-1]["content"]
    assert steer_sent.startswith("[") and steer_sent.endswith(STEER)
    assert _boundary_violations(sent_n1[-1], sent_n2[0]) == []


def test_recursive_followup_run_agent_call_passes_persist_overrides():
    """Every recursive ``_run_agent`` follow-up carries the composed persist pair."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(gateway_run.GatewayRunner)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run_agent"
        and any(kw.arg == "_interrupt_depth" for kw in node.keywords)
    ]
    assert calls, "recursive follow-up _run_agent call not found"
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        assert {"persist_user_message", "persist_user_timestamp"} <= names, call.lineno
