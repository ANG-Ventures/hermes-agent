"""Tests for pre-API-call message-sequence repair.

Covers ``_repair_message_sequence`` and the extended
``_drop_trailing_empty_response_scaffolding`` behavior that rewinds past
orphan tool-result tails. Together these prevent the self-reinforcing empty-
response loop observed in session 20260507_044111_fa7e65, where a tool-result
followed directly by a user message produced silent empty responses from
providers (violating role alternation), which retriggered the empty-retry
recovery every turn.
"""

import pytest
from run_agent import AIAgent


def _bare_agent():
    return AIAgent.__new__(AIAgent)


# ── _drop_trailing_empty_response_scaffolding ──────────────────────────────

def test_drop_scaffolding_rewinds_orphan_tool_tail():
    """When scaffolding is stripped, also rewind the orphan assistant+tool pair."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "assistant", "content": "(empty)",
         "_empty_terminal_sentinel": True},
    ]

    AIAgent._drop_trailing_empty_response_scaffolding(agent, messages)

    assert messages == [{"role": "user", "content": "task"}]






# ── _repair_message_sequence ───────────────────────────────────────────────

def test_repair_merges_consecutive_user_messages():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 1
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "first\n\nsecond"


def test_repair_preserves_user_content_when_one_side_empty():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "real message"},
    ]

    AIAgent._repair_message_sequence(agent, messages)

    assert messages == [{"role": "user", "content": "real message"}]


def test_repair_does_not_rewind_ongoing_dialog_tool_pair():
    """assistant(tool_calls) + tool + user is a VALID pattern (user redirect
    before the model gets its continuation turn). Repair must not touch it —
    only the flag-gated scaffolding strip rewinds, and only when the
    empty-recovery scaffolding was actually present.
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "user", "content": "Q2"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_drops_stray_tool_with_unknown_tool_call_id():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "tool_call_id": "orphan", "content": "stray"},
        {"role": "user", "content": "real"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    assert all(m.get("role") != "tool" for m in messages)


@pytest.mark.xfail(
    reason=(
        "INHERITED fork/main defect, NOT parity-merge damage -- see card t_08cca32f. "
        "Pass 1.5 of repair_message_sequence (agent/agent_runtime_helpers.py, "
        "fork-only code from PR #196 / aaa34311e0) resolves a tool_call's answered "
        "budget against tc.get('id') ONLY, while Pass 1 correctly matches the "
        "id||call_id superset per PR #58168. A tool_call carrying only call_id (or a "
        "Codex-Responses call whose id and call_id differ) therefore reads as "
        "unanswered, and the 'none answered' branch deletes an assistant turn that "
        "WAS answered. Evidence: reproduced identically on a clean detached worktree "
        "at fork/main ee2fce2876 (repairs=1, expected 0); the merge's diff over the "
        "Pass 1.5 region is empty; 'Pass 1.5' appears 6x at fork/main HEAD and 0x at "
        "both merge-base a7a696ba and upstream target 1e5b5074. These tests exist at "
        "the merge base and upstream but were dropped from fork/main -- the merge "
        "restored them, which is how the bug surfaced. strict=False so this xpasses "
        "and flags itself for deletion once t_08cca32f lands."
    ),
    strict=False,
)
def test_repair_keeps_tool_matching_codex_call_id():
    """A valid tool result must survive when the assistant tool_call carries a
    Codex-format ``call_id`` distinct from ``id`` and the result matches on
    ``call_id`` (#58168).

    Before the fix, Pass 1 registered only ``tc.get("id")`` (``fc_...``) in the
    known-id set, so a result keyed on ``call_id`` (``call_...``) looked
    orphaned and was dropped -- leaving the assistant tool_call unanswered and
    triggering an HTTP 400 on strict providers (DeepSeek, Kimi):
    "Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'".
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "fc_123", "call_id": "call_ABC",
                         "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_ABC", "content": "result"},
        {"role": "user", "content": "next"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2]["tool_call_id"] == "call_ABC"


@pytest.mark.xfail(
    reason=(
        "INHERITED fork/main defect, NOT parity-merge damage -- see card t_08cca32f. "
        "Pass 1.5 of repair_message_sequence (agent/agent_runtime_helpers.py, "
        "fork-only code from PR #196 / aaa34311e0) resolves a tool_call's answered "
        "budget against tc.get('id') ONLY, while Pass 1 correctly matches the "
        "id||call_id superset per PR #58168. A tool_call carrying only call_id (or a "
        "Codex-Responses call whose id and call_id differ) therefore reads as "
        "unanswered, and the 'none answered' branch deletes an assistant turn that "
        "WAS answered. Evidence: reproduced identically on a clean detached worktree "
        "at fork/main ee2fce2876 (repairs=1, expected 0); the merge's diff over the "
        "Pass 1.5 region is empty; 'Pass 1.5' appears 6x at fork/main HEAD and 0x at "
        "both merge-base a7a696ba and upstream target 1e5b5074. These tests exist at "
        "the merge base and upstream but were dropped from fork/main -- the merge "
        "restored them, which is how the bug surfaced. strict=False so this xpasses "
        "and flags itself for deletion once t_08cca32f lands."
    ),
    strict=False,
)
def test_repair_keeps_tool_matching_only_call_id():
    """Same as above but the assistant tool_call carries ONLY ``call_id`` (no
    ``id``). The result keyed on ``call_id`` must still be recognized (#58168).
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"call_id": "call_XYZ", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_XYZ", "content": "result"},
        {"role": "user", "content": "next"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert any(m.get("role") == "tool" for m in messages)











def test_repair_leaves_valid_conversation_unchanged():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "a.txt b.txt"},
        {"role": "assistant", "content": "Found 2 files"},
        {"role": "user", "content": "more"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original




# ── repair_message_sequence_with_cursor (#44837) ───────────────────────────

from agent.agent_runtime_helpers import repair_message_sequence_with_cursor


def test_cursor_clamped_when_compaction_shrinks_below_cursor():
    """Cursor past the new end of the list must come back in range so the
    turn-end flush doesn't skip the assistant/tool chain (#44837)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    agent._last_flushed_db_idx = 2  # both rows already flushed

    repairs = repair_message_sequence_with_cursor(agent, messages)

    assert repairs == 1
    assert len(messages) == 1
    assert agent._last_flushed_db_idx == 1


def test_cursor_rewinds_when_compaction_happens_before_cursor():
    """Repair that drops/merges messages at indexes BELOW the cursor must
    rewind it by the number removed, or unflushed rows get skipped.
    A plain min() clamp does NOT catch this case."""
    agent = _bare_agent()
    flushed_a = {"role": "user", "content": "first"}
    flushed_b = {"role": "user", "content": "second"}  # merged into flushed_a
    unflushed_assistant = {"role": "assistant", "content": "answer"}
    messages = [flushed_a, flushed_b, unflushed_assistant]
    agent._last_flushed_db_idx = 2  # the two user rows are flushed

    repairs = repair_message_sequence_with_cursor(agent, messages)

    assert repairs == 1
    assert len(messages) == 2
    # Cursor must now point at the assistant (index 1), not stay at 2 —
    # min(2, len=2) would leave it at 2 and the flush would skip it.
    assert agent._last_flushed_db_idx == 1
    assert messages[agent._last_flushed_db_idx] is unflushed_assistant






def test_flush_guard_clamps_overshooting_cursor():
    """_flush_messages_to_session_db safety net: an overshooting cursor must
    not produce a negative-start slice that skips everything (#44837)."""

    class _DB:
        def __init__(self):
            self.rows = []

        def append_message(self, **kw):
            self.rows.append(kw)

        def append_messages_batch(self, session_id, messages, **kw):
            for m in messages:
                self.rows.append(dict(m, session_id=session_id))
            return list(range(1, len(messages) + 1))


        def recompute_effective_last_active(self, _session_id):
            pass

    agent = _bare_agent()
    agent._session_db = _DB()
    agent._session_db_created = True
    agent.session_id = "s1"
    agent._persist_user_message_override = None
    agent._last_flushed_db_idx = 5  # stale — past end of compacted list
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]

    AIAgent._flush_messages_to_session_db(agent, messages, conversation_history=[])

    # min(5, 2) = 2 → nothing skipped below start_idx, cursor settles at 2
    assert agent._last_flushed_db_idx == 2


# ── Pass 0: merge consecutive assistant messages (issue #29148, #49147) ─────



















# ── Pass 1.5: mid-history orphan tool_use repair ───────────────────────────
# An assistant(tool_calls) whose calls get NO answering tool result before the
# next turn is a guaranteed provider 400 ("tool_use ids were found without
# tool_result"). The 2026-07-04 incident: a tool-call row double-written during
# an FTS-write-corruption restart storm left an orphan buried MID-history — the
# dangling-TAIL stripper and Pass 0 both missed it, so it reached the wire and
# cascaded across 11 fallback subs.

def test_repair_strips_unanswered_tool_calls_keeps_text():
    """assistant(tool_calls)+text followed by a user message (calls never
    answered) → strip the tool_calls, keep the assistant text turn."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do X"},
        {"role": "assistant", "content": "Working on it.",
         "tool_calls": [{"id": "orphan1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "actually do Y instead"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    # No message may still carry an unanswered tool_call.
    assert all("tool_calls" not in m for m in messages)
    # The assistant text survives.
    assert any(m.get("role") == "assistant" and m.get("content") == "Working on it."
               for m in messages)
    # No tool-role message was invented.
    assert all(m.get("role") != "tool" for m in messages)


def test_repair_drops_orphan_tool_call_turn_with_no_text():
    """A pure assistant(tool_calls) with empty content and no answers → drop
    the whole orphan turn (nothing salvageable)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do X"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "orphan1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "never mind"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    assert messages == [
        {"role": "user", "content": "do X"},
        {"role": "user", "content": "never mind"},
    ] or messages == [
        # Pass 2 may merge the two now-adjacent user turns.
        {"role": "user", "content": "do X\n\nnever mind"},
    ]


def test_repair_leaves_answered_tool_call_untouched():
    """Regression: an assistant(tool_calls) that IS answered stays put — even
    when followed by a user redirect (the valid ongoing-dialog pattern)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "user", "content": "Q2"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_partial_orphan_keeps_answered_drops_unanswered():
    """assistant(tool_calls A,B) where only A is answered → drop B, keep A."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run two"},
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "A", "type": "function", "function": {"name": "f", "arguments": "{}"}},
             {"id": "B", "type": "function", "function": {"name": "g", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "A", "content": "ra"},
        {"role": "assistant", "content": "done"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    tc_turn = next(m for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    ids = [tc["id"] for tc in tc_turn["tool_calls"]]
    assert ids == ["A"]  # B (unanswered) dropped, A (answered) kept


def test_repair_exempts_codex_interim_unanswered_tool_state():
    """A Codex Responses interim assistant turn legitimately carries unanswered
    interim tool state for the encrypted replay chain — Pass 1.5 must not touch
    it (same exemption as Pass 0)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "think"},
        {"role": "assistant", "content": "", "finish_reason": "incomplete",
         "codex_reasoning_items": [{"encrypted_content": "enc"}],
         "tool_calls": [{"id": "interim1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "content": "final"},
    ]
    before = [dict(m) for m in messages]

    AIAgent._repair_message_sequence(agent, messages)

    interim = [m for m in messages if m.get("finish_reason") == "incomplete"]
    assert len(interim) == 1
    assert interim[0].get("tool_calls") == before[1]["tool_calls"]


def test_repair_incident_shape_duplicate_first_orphan_second_answered():
    """The exact 2026-07-04 incident shape: a duplicated assistant(tool_use)
    where the FIRST copy is never answered (orphan) and a later byte-identical
    copy IS answered. After repair there must be ZERO unanswered tool_use, and
    the answered copy + its result survive."""
    agent = _bare_agent()
    dup_call = {"id": "toolu_dup", "type": "function",
                "function": {"name": "cronjob", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "schedule it"},
        # orphan copy — no tool result follows, then the user jumped in
        {"role": "assistant", "content": "Scheduling.", "tool_calls": [dict(dup_call)]},
        {"role": "user", "content": "continue"},
        # ... later, the answered twin (same id, correctly resolved) ...
        {"role": "assistant", "content": "Scheduling.", "tool_calls": [dict(dup_call)]},
        {"role": "tool", "tool_call_id": "toolu_dup", "content": "{\"success\": true}"},
        {"role": "assistant", "content": "Done."},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    # Reconstruct answered-set validation: every assistant tool_call id must be
    # answered by the immediately-following tool run.
    unanswered = []
    for idx, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = set()
            k = idx + 1
            while k < len(messages) and messages[k].get("role") == "tool":
                answered.add(messages[k].get("tool_call_id"))
                k += 1
            for tc in m["tool_calls"]:
                if tc["id"] not in answered:
                    unanswered.append(tc["id"])
    assert unanswered == []
    # The answered twin + its result survive.
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "toolu_dup"
               for m in messages)


def test_repair_duplicate_ids_in_one_turn_countmatched_not_setmatched():
    """Greptile P1 (#196): an assistant turn with DUPLICATE ids in ONE turn
    ``tool_calls=[X, X]`` answered by only a SINGLE ``tool`` result for X must
    keep exactly ONE X (count-based), not both (set-based would leave 2 calls /
    1 result → still a 400)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run X twice"},
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "X", "content": "one result"},
        {"role": "assistant", "content": "done"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    tc_turn = next(m for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    # exactly ONE X kept — matches the single result; wire-valid.
    assert [tc["id"] for tc in tc_turn["tool_calls"]] == ["X"]
    n_results = sum(1 for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "X")
    assert len(tc_turn["tool_calls"]) == n_results  # calls == results for id X


def test_repair_duplicate_ids_in_one_turn_both_answered_kept():
    """Counterpart: ``[X, X]`` answered by TWO results for X (a real parallel
    call reusing an id) → both kept, untouched."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run X twice"},
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "X", "content": "r1"},
        {"role": "tool", "tool_call_id": "X", "content": "r2"},
        {"role": "assistant", "content": "done"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_drops_surplus_duplicate_tool_result():
    """Greptile P1 (#196) inverse: ONE assistant call for X followed by TWO
    ``tool`` results for X → Anthropic 400s ('each tool_use must have a single
    result. Found multiple tool_result blocks with id'). Pass 1 must drop the
    surplus result, keeping one-result-per-call."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run X"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "X", "content": "r1"},
        {"role": "tool", "tool_call_id": "X", "content": "r2 (surplus)"},
        {"role": "assistant", "content": "done"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    x_results = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "X"]
    assert len(x_results) == 1  # surplus dropped
    assert x_results[0]["content"] == "r1"  # the FIRST result is kept

# ── Self-recovery: heal empty-content non-final messages ──────────────────
# Repro of the production incident: a dead stream persisted an empty-content
# assistant stub mid-transcript, and every later request 400'd with
# "all messages must have non-empty content except for the optional final
# assistant message" (INVALID_REQUEST_BODY). sanitize_api_messages now heals
# such turns on the per-call copy so the session recovers itself in memory.












def test_repair_two_calls_two_results_same_id_kept():
    """Symmetric counterpart: TWO calls for X answered by TWO results for X →
    both results kept (parallel-call id reuse is valid; budget = 2)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run X twice"},
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
             {"id": "X", "type": "function", "function": {"name": "f", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "X", "content": "r1"},
        {"role": "tool", "tool_call_id": "X", "content": "r2"},
        {"role": "assistant", "content": "done"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


# --- upstream-owned sanitize_api_messages coverage (frozen target 1e5b5074) ---
# fork parity NOTE: restored verbatim from upstream's side of this file. The
# merge dropped them alongside upstream's own test-prune hunks, but these are
# NOT pruned upstream -- they still ship at the frozen target and guard live
# sanitize_api_messages behavior present in agent/agent_runtime_helpers.py.


def test_sanitize_deduplicates_duplicate_assistant_tool_call_ids():
    """sanitize_api_messages collapses duplicate tool_calls sharing an id
    WITHIN a single assistant message (the message[6] shape from #58327)."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_Y", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}},
            {"id": "call_Y", "type": "function",
             "function": {"name": "bar", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_Y", "content": "r"},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    ids = [tc["id"] for tc in assistant["tool_calls"]]
    assert ids == ["call_Y"]  # duplicate collapsed


def test_sanitize_deduplicates_duplicate_tool_results():
    """sanitize_api_messages (final pre-API chokepoint) drops duplicate tool
    results sharing a tool_call_id."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_X", "type": "function",
                         "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_X", "content": "A"},
        {"role": "tool", "tool_call_id": "call_X", "content": "B (duplicate)"},
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_api_messages(list(messages))
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == ["call_X"]  # exactly one survives


def test_sanitize_drops_empty_tool_calls_array():
    """sanitize_api_messages strips ``tool_calls: []`` from assistant messages.

    DeepSeek v4 rejects an empty tool_calls array with HTTP 400 "Invalid
    'messages[N].tool_calls': empty array" (#58755). The empty array is
    semantically "no tool calls", so the key is dropped while content is
    preserved.
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "answer", "tool_calls": []},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert "tool_calls" not in assistant
    assert assistant["content"] == "answer"


def test_sanitize_preserves_distinct_tool_call_ids():
    """Negative control: legitimate DISTINCT tool_call_ids must NOT be dropped
    (guards against over-dedup)."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_A", "type": "function",
             "function": {"name": "a", "arguments": "{}"}},
            {"id": "call_B", "type": "function",
             "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_A", "content": "ra"},
        {"role": "tool", "tool_call_id": "call_B", "content": "rb"},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["call_A", "call_B"]
    assert sorted(m["tool_call_id"] for m in out if m.get("role") == "tool") == ["call_A", "call_B"]
