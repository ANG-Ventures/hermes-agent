"""A `/stop`'d turn's in-flight writes must not keep landing in the transcript.

`/stop` interrupts the running agent and invalidates the turn's run generation,
but interruption is cooperative: the turn is already inside a tool call or a
streaming response and keeps going until it reaches a checkpoint. Every persist
that fires in that window still writes — so a turn the user explicitly stopped
continues appending assistant content to the durable transcript *after* the
stop, and those rows come back on the next `/resume`.

The persist layer must drop a superseded turn's NEW content rows, with two
load-bearing carve-outs:

* the interrupt-close tail must ALWAYS persist — it is the role-alternation
  repair that stops the next user message landing as `... tool -> user`;
* a `tool` result may only be dropped when its owning `assistant(tool_calls)`
  row was dropped too, otherwise an already-durable tool call is orphaned —
  the exact transcript corruption the repair above exists to prevent.
"""

import pytest

from agent.message_sanitization import (
    _INTERRUPT_CLOSE_FINISH_REASON,
    close_interrupted_tool_sequence,
)


class _FakeDB:
    """Records what actually reached the durable store."""

    def __init__(self):
        self.rows = []

    def append_message(self, **kwargs):
        self.rows.append(kwargs)
        return True

    def add_message(self, **kwargs):
        self.rows.append(kwargs)
        return True

    @property
    def roles(self):
        return [row.get("role") for row in self.rows]

    @property
    def contents(self):
        return [row.get("content") for row in self.rows]


def _make_agent(*, superseded: bool):
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._session_db = _FakeDB()
    agent._session_db_created = True
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent.session_id = "sess-1"
    agent._persist_disabled = False
    agent._persist_superseded = superseded
    return agent


def _flush(agent, messages, history=None):
    agent._flush_messages_to_session_db_unlocked(messages, history or [])
    return agent._session_db


# ── the core symptom ────────────────────────────────────────────────────────


def test_stopped_turn_content_is_not_persisted():
    """The zombie's continued assistant content must not reach the store."""
    agent = _make_agent(superseded=True)

    db = _flush(agent, [
        {"role": "assistant", "content": "text written after the user pressed /stop"},
    ])

    assert db.rows == [], "a stopped turn's content still landed in the transcript"


def test_normal_turn_still_persists_everything():
    """Control: the gate is inert for a turn that was never stopped."""
    agent = _make_agent(superseded=False)

    db = _flush(agent, [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "done"},
    ])

    assert db.roles == ["user", "assistant"]


# ── carve-out 1: the interrupt-close tail is load-bearing ───────────────────


def test_interrupt_close_tail_still_persists_on_a_stopped_turn():
    """The role-alternation repair must survive the gate.

    Without it the durable transcript ends on a raw `tool` row and the next
    user message lands as `... tool -> user`, which strict providers react to
    by ignoring prior context.
    """
    agent = _make_agent(superseded=True)
    messages = [{"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    # Reuse the real repair helper rather than hand-rolling its output, so this
    # test tracks the helper's actual contract.
    assert close_interrupted_tool_sequence(messages, "") is True

    db = _flush(agent, messages)

    # The `tool` row passes through because its owning tool_call was never
    # suppressed by us (pairing safety), and the interrupt-close tail is
    # carved out unconditionally — so the durable tail is assistant, not tool.
    assert db.roles == ["tool", "assistant"], (
        "the interrupt-close tail was suppressed"
    )
    assert db.rows[-1]["finish_reason"] == _INTERRUPT_CLOSE_FINISH_REASON


# ── carve-out 2: tool-call pairing safety ───────────────────────────────────


def test_orphan_is_not_created_when_the_owning_call_is_already_durable():
    """A tool result whose owner already persisted must still be written.

    Dropping it would strand a durable `assistant(tool_calls)` row with no
    matching result — a dangling tool call, which is worse than a stray row.
    """
    agent = _make_agent(superseded=True)
    owner = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
    }
    # The owner was persisted in a PRIOR flush, before /stop.
    _flush(_make_agent(superseded=False), [owner])
    agent._flush_messages_to_session_db_unlocked([owner], [owner])
    agent._session_db.rows.clear()

    db = _flush(agent, [
        owner,
        {"role": "tool", "tool_call_id": "call-1", "content": "output"},
    ], history=[owner])

    assert db.roles == ["tool"], (
        "suppressed a tool result whose owning tool_call is already durable "
        "— that orphans the call"
    )


def test_new_call_and_its_result_are_dropped_together():
    """When BOTH halves are new, the pair is dropped atomically."""
    agent = _make_agent(superseded=True)

    db = _flush(agent, [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-9", "function": {"name": "terminal"}}],
        },
        {"role": "tool", "tool_call_id": "call-9", "content": "output"},
    ])

    assert db.rows == [], "a new tool_call/result pair was only half-suppressed"


def test_pairing_holds_when_the_result_arrives_in_a_later_flush():
    """The owner and its result flush separately within one turn.

    `conversation_loop` appends the assistant(tool_calls) row and flushes,
    THEN executes tools and flushes again. Suppression state must therefore
    survive across flushes, or the result persists orphaned.
    """
    agent = _make_agent(superseded=True)
    owner = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-7", "function": {"name": "terminal"}}],
    }

    _flush(agent, [owner])
    db = _flush(agent, [
        owner,
        {"role": "tool", "tool_call_id": "call-7", "content": "output"},
    ])

    assert db.rows == [], (
        "the tool result persisted orphaned because suppression state did not "
        "survive across flushes within the turn"
    )


# ── fail-open safety ────────────────────────────────────────────────────────


def test_a_user_row_is_never_dropped():
    """Fail-open on unexpected roles — losing a real user message is data loss.

    A zombie turn only ever emits assistant/tool rows, so a NEW user row here
    is anomalous and must be persisted rather than silently discarded.
    """
    agent = _make_agent(superseded=True)

    db = _flush(agent, [{"role": "user", "content": "a real message"}])

    assert db.contents == ["a real message"]


def test_absent_flag_behaves_as_not_superseded():
    """Back-compat: an agent without the attribute persists normally."""
    agent = _make_agent(superseded=False)
    del agent._persist_superseded

    db = _flush(agent, [{"role": "assistant", "content": "hello"}])

    assert db.contents == ["hello"]


def test_unreadable_flag_fails_open():
    """A guard bug must never cost a real row."""

    class _Boom:
        def __bool__(self):
            raise RuntimeError("flag is broken")

    agent = _make_agent(superseded=False)
    agent._persist_superseded = _Boom()

    db = _flush(agent, [{"role": "assistant", "content": "hello"}])

    assert db.contents == ["hello"]
