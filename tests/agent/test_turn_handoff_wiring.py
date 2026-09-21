"""The handoff is WIRED: written at the cut, injected on the next turn.

Round-trip regression for the 2026-09-21 turnover. The library half is
covered by ``test_turn_handoff.py``; this file drives the two live seams:

* ``capture_turn_handoff`` — called from the conversation loop's terminal
  provider-failure return, producing the file AND the 3-line chat notice;
* ``consume_handoff_context`` — called from the turn prologue, producing the
  injected context for the NEXT turn and consuming the file.
"""

from __future__ import annotations

import types

from agent.turn_handoff import (
    capture_turn_handoff,
    consume_handoff_context,
    handoff_path_for,
)


class _Store:
    def read(self):
        return [{"id": "1", "content": "finish the audit", "status": "pending"}]


def _agent(session_key="discord:chan-7"):
    a = types.SimpleNamespace()
    a._gateway_session_key = session_key
    a.session_id = "sess-1"
    a.provider = "claude-bpx-17"
    a.model = "claude-opus-5"
    a._todo_store = _Store()
    a.notices = []
    a._emit_status = lambda m: a.notices.append(m)
    return a


def _messages():
    return [
        {"role": "user", "content": "audit the cron jobs", "row_id": 42},
        {
            "role": "assistant",
            "content": "Listing jobs now.",
            "tool_calls": [{
                "id": "c1",
                "function": {"name": "terminal",
                             "arguments": '{"command":"hermes cron list"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "job-a\njob-b"},
        {
            "role": "assistant",
            "content": "Reading job-a.",
            "tool_calls": [{
                "id": "c2",
                "function": {"name": "read_file",
                             "arguments": '{"path":"job-a.json"}'},
            }],
        },
    ]


# ── capture at the cut ──────────────────────────────────────────────────


def test_capture_writes_the_file_and_returns_the_notice(tmp_path):
    agent = _agent()
    notice = capture_turn_handoff(
        agent, _messages(), turn_start_idx=0,
        reason="the provider is rate-limiting", root=tmp_path,
    )
    assert handoff_path_for("discord:chan-7", root=tmp_path).exists()
    assert "/resume-handoff" in notice
    assert len(notice.strip().splitlines()) == 3


def test_capture_without_a_session_key_is_a_silent_no_op(tmp_path):
    agent = _agent(session_key=None)
    assert capture_turn_handoff(agent, _messages(), turn_start_idx=0,
                                reason="x", root=tmp_path) == ""
    assert list(tmp_path.glob("*.json")) == []


def test_capture_of_an_empty_turn_writes_nothing(tmp_path):
    agent = _agent()
    msgs = [{"role": "user", "content": "hi", "row_id": 1}]
    assert capture_turn_handoff(agent, msgs, turn_start_idx=0,
                                reason="x", root=tmp_path) == ""
    assert list(tmp_path.glob("*.json")) == []


def test_capture_never_raises(tmp_path):
    """A dying turn must not die twice."""
    broken = types.SimpleNamespace(_gateway_session_key="k")
    assert capture_turn_handoff(broken, None, turn_start_idx=0,
                                reason="x", root=tmp_path) == ""


# ── the round trip ──────────────────────────────────────────────────────


def test_next_turn_sees_the_cut_turns_work(tmp_path):
    agent = _agent()
    capture_turn_handoff(agent, _messages(), turn_start_idx=0,
                         reason="the provider is rate-limiting", root=tmp_path)

    # NEXT turn, fresh agent object for the same session key.
    resumed = _agent()
    ctx = consume_handoff_context(resumed, root=tmp_path)

    assert "audit the cron jobs" in ctx              # the original ask
    assert "Listing jobs now." in ctx                # in-progress text
    assert "job-a" in ctx                            # the completed result
    assert "read_file" in ctx                        # the in-flight call
    assert "NEVER COMPLETED" in ctx                  # …and that it never ran
    assert "finish the audit" in ctx                 # the open todo


def test_the_handoff_is_consumed_exactly_once(tmp_path):
    agent = _agent()
    capture_turn_handoff(agent, _messages(), turn_start_idx=0,
                         reason="x", root=tmp_path)
    assert consume_handoff_context(_agent(), root=tmp_path) != ""
    assert consume_handoff_context(_agent(), root=tmp_path) == ""
    assert not handoff_path_for("discord:chan-7", root=tmp_path).exists()


def test_a_different_session_sees_nothing(tmp_path):
    capture_turn_handoff(_agent(), _messages(), turn_start_idx=0,
                         reason="x", root=tmp_path)
    assert consume_handoff_context(_agent("telegram:other"), root=tmp_path) == ""


def test_no_handoff_injects_nothing(tmp_path):
    assert consume_handoff_context(_agent(), root=tmp_path) == ""


def test_consume_never_raises(tmp_path):
    assert consume_handoff_context(types.SimpleNamespace(), root=tmp_path) == ""
