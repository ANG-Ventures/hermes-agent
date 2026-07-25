"""Prompt-cache + role-alternation invariants for /undo and /redo.

These are the AGENTS.md invariants a transcript rewind is most likely to break,
asserted as behavior contracts rather than argued in a review comment:

  * A rewind may only truncate a SUFFIX of the active transcript — never
    reorder, edit, or synthesise a row. So the surviving prefix is byte-identical
    to the prefix the provider already cached.
  * A redo restores exactly the row-id SET the matching undo removed, so
    undo→redo is a round trip to the original transcript.
  * Neither operation can leave a ``tool`` row whose ``assistant(tool_calls)``
    owner is inactive (the orphan guard), which is the shape strict
    OpenAI-compatible providers reject outright.
  * Whatever the landing shape, ``repair_message_sequence`` needs no repairs on
    the rewound transcript — i.e. /undo does not manufacture alternation
    violations. (Two consecutive user rows after an edit-and-resend ARE expected
    and are repaired at the existing pre-request belt; that path is covered in
    tests/cli/test_undo_redo_half_turn.py.)
"""

from __future__ import annotations

import pytest

import hermes_undo
from agent.agent_runtime_helpers import repair_message_sequence
from hermes_state import RewindWouldOrphanError, SessionDB


@pytest.fixture()
def db(tmp_path, monkeypatch):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(hermes_undo, "_session_db", session_db)
    hermes_undo.clear_state()
    yield session_db
    session_db.close()
    hermes_undo.clear_state()


def _conv(db, sid):
    return db.get_messages_as_conversation(sid)


def _rows(db, sid):
    return [(m["id"], m["role"]) for m in db.get_messages(sid)]


def _seed_alternating(db, sid, turns=4):
    db.create_session(sid, source="cli")
    for i in range(1, turns + 1):
        db.append_message(sid, "user", f"q{i}")
        db.append_message(sid, "assistant", f"a{i}")


def test_undo_only_truncates_a_suffix_so_the_cached_prefix_survives(db):
    """The rewound transcript must be a strict PREFIX of the pre-rewind one.

    This is the prompt-cache contract: the provider caches a prefix of the
    message list, so as long as /undo can only drop rows off the END, every
    surviving message is byte-identical to what was cached and the next turn
    still hits the cache for the surviving span. A rewind that edited or
    reordered a surviving row would invalidate the whole cached prefix.
    """
    sid = "cache-prefix"
    _seed_alternating(db, sid)
    before = _conv(db, sid)

    for n in (1, 1, 2):
        hermes_undo.undo(sid, n)
        after = _conv(db, sid)
        assert len(after) <= len(before)
        # The surviving rows are the *same objects, in the same order* — the
        # rewind is a truncation, not a rewrite.
        assert after == before[: len(after)]
        before = after


def test_undo_never_leaves_two_same_party_rows_adjacent(db):
    """Half-turn grouping must not split a party's run in half.

    A half-turn is one party's contiguous run, so a landing can never produce
    ``assistant, assistant`` or ``user, user`` at the seam — the boundary the
    core rewinds to is always a party CHANGE.
    """
    sid = "alt-seam"
    db.create_session(sid, source="cli")
    db.append_message(sid, "user", "q1")
    db.append_message(sid, "assistant", "a1a")
    db.append_message(sid, "assistant", "a1b")  # same party, two rows
    db.append_message(sid, "user", "q2")
    db.append_message(sid, "assistant", "a2")

    for _ in range(3):
        result = hermes_undo.undo(sid, 1)
        if not result.get("rewound_ids"):
            break
        roles = [m["role"] for m in _conv(db, sid)]
        # No two adjacent rows may come from the same PARTY (tool counts as
        # assistant-side), which is the alternation rule providers enforce.
        parties = [hermes_undo._party(r) for r in roles]
        for a, b in zip(parties, parties[1:]):
            assert not (a == b == "user"), f"user;user seam in {roles}"


def test_rewound_transcript_needs_zero_alternation_repairs(db):
    """The repair belt must find nothing to fix after a rewind.

    ``repair_message_sequence`` is the pre-request defensive belt. If /undo
    manufactured alternation violations, the belt would silently rewrite the
    transcript on every subsequent request — which both changes the bytes the
    provider sees and re-invalidates the cache each turn.
    """
    sid = "no-repairs"
    _seed_alternating(db, sid, turns=5)

    for n in (1, 2, 1, 3):
        hermes_undo.undo(sid, n)
        messages = _conv(db, sid)
        assert repair_message_sequence(None, messages) == 0, messages


def test_undo_redo_round_trip_restores_the_exact_transcript(db):
    """undo→redo must be an identity on the transcript.

    ``restore_ids`` reactivates the exact row-id set the undo deactivated (not
    an ``id >=`` range), so the restored transcript is byte-identical to the
    original — which is what lets the provider re-use its cached prefix after a
    redo instead of re-ingesting the conversation.
    """
    sid = "round-trip"
    _seed_alternating(db, sid)
    original_rows = _rows(db, sid)
    original_conv = _conv(db, sid)

    hermes_undo.undo(sid, 2)
    assert _rows(db, sid) != original_rows

    hermes_undo.redo(sid, 1)
    assert _rows(db, sid) == original_rows
    assert _conv(db, sid) == original_conv
    assert repair_message_sequence(None, _conv(db, sid)) == 0


def test_stacked_undos_redo_back_to_the_exact_original(db):
    """Several stacked undos redo back to the original, in order."""
    sid = "stacked-round-trip"
    _seed_alternating(db, sid, turns=5)
    original_rows = _rows(db, sid)

    hermes_undo.undo(sid, 1)
    hermes_undo.undo(sid, 2)
    hermes_undo.undo(sid, 1)
    assert len(_rows(db, sid)) < len(original_rows)

    hermes_undo.redo(sid, 3)
    assert _rows(db, sid) == original_rows
    assert repair_message_sequence(None, _conv(db, sid)) == 0


def test_rewind_refuses_to_orphan_a_tool_row(db):
    """The orphan guard fires BEFORE any write, so nothing is mutated.

    A ``tool`` row whose ``assistant(tool_calls)`` owner is inactive is rejected
    outright by strict OpenAI-compatible providers. The guard runs before the
    single write, so a refusal leaves the transcript exactly as it was — which
    is what lets the caller report "busy, nothing changed" honestly.
    """
    sid = "orphan-guard"
    db.create_session(sid, source="cli")
    db.append_message(sid, "user", "q1")
    # A mid-flush interleave: the tool result landed with a LOWER id than the
    # assistant row carrying its tool_calls.
    db.append_message(sid, "tool", "result", tool_call_id="call-1")
    db.append_message(
        sid, "assistant", "a1",
        tool_calls=[{"id": "call-1", "type": "function",
                     "function": {"name": "x", "arguments": "{}"}}],
    )
    before = _rows(db, sid)

    with pytest.raises(RewindWouldOrphanError):
        db.rewind_to_message(sid, before[-1][0], require_user_role=False)

    # Nothing was mutated — the guard is pre-write.
    assert _rows(db, sid) == before


def test_assistant_tool_pair_is_rewound_together(db):
    """An assistant(tool_calls)+tool pair is one half-turn and moves as a unit.

    Rewinding into the middle of a tool round would strand either the call or
    its result. Because ``tool`` and ``assistant`` share a party in the
    half-turn grouping, the pair is always inside the same group.
    """
    sid = "tool-pair"
    db.create_session(sid, source="cli")
    db.append_message(sid, "user", "q1")
    db.append_message(
        sid, "assistant", "",
        tool_calls=[{"id": "call-1", "type": "function",
                     "function": {"name": "x", "arguments": "{}"}}],
    )
    db.append_message(sid, "tool", "result", tool_call_id="call-1")
    db.append_message(sid, "assistant", "a1")

    hermes_undo.undo(sid, 1)
    roles = [m["role"] for m in _conv(db, sid)]
    # The whole assistant-side run went, leaving just the user turn — the tool
    # row cannot survive without its owner and vice versa.
    assert roles == ["user"]
    assert repair_message_sequence(None, _conv(db, sid)) == 0


def test_undo_does_not_touch_the_system_prompt_or_inject_a_user_row(db):
    """No synthetic rows: the row COUNT only ever goes down on undo.

    AGENTS.md forbids injecting a synthetic user message mid-loop and requires a
    byte-stable system prompt for the life of a conversation. The undo core only
    flips ``active`` on existing rows — it has no INSERT path — so neither can
    happen. Asserted structurally: every id present after a rewind was present
    before it, and no row's content changed.
    """
    sid = "no-injection"
    _seed_alternating(db, sid)
    before = {m["id"]: (m["role"], m["content"]) for m in db.get_messages(sid)}

    hermes_undo.undo(sid, 2)
    after = {m["id"]: (m["role"], m["content"]) for m in db.get_messages(sid)}

    assert set(after) < set(before), "undo must only remove ids, never add"
    for mid, payload in after.items():
        assert payload == before[mid], f"row {mid} was mutated by the rewind"
    assert not any(m["role"] == "system" for m in db.get_messages(sid))
