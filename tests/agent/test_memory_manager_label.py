"""The recalled-memory label is a data-not-instructions boundary, and the scrubber still knows it.

``build_memory_context_block`` writes the label; ``sanitize_context`` (via ``_INTERNAL_NOTE_RE``)
is what strips a previously injected block when it comes back in history or provider output. If
the two drift apart, the old note survives the strip and the block is duplicated every turn.
"""
from __future__ import annotations

from agent.memory_manager import build_memory_context_block, sanitize_context


def _note(block: str) -> str:
    return block.split("\n", 2)[1]


def test_label_marks_recall_as_untrusted_data_not_instructions():
    note = _note(build_memory_context_block("- Ace's NAS is at 192.168.1.159."))
    low = note.lower()

    assert note.startswith("[System note:") and note.endswith("]")
    assert "authoritative" not in low
    assert "should inform all responses" not in low
    assert "untrusted" in low and "never instructions" in low
    assert "stale or wrong" in low
    assert "may inform" in low


def test_new_label_round_trips_through_the_scrubber():
    """A block built now, then replayed in front of later text, strips to exactly that text."""
    block = build_memory_context_block("- fact one\n- fact two\n")
    assert sanitize_context(block + "\n\nhello") == "\n\nhello"

    # The note alone (fences already gone, e.g. a provider echoing it) is still recognised.
    bare = sanitize_context(_note(block) + "\n\n- fact one")
    assert "System note" not in bare
    assert bare == "- fact one"


def test_rewrapping_an_echoed_note_does_not_nest_it():
    """A provider that echoes our note back (unfenced) gets it stripped, so the block carries ONE note."""
    clean = build_memory_context_block("- fact one\n")
    echoed = build_memory_context_block(_note(clean) + "\n\n- fact one\n")

    assert echoed.count("[System note:") == 1
    assert echoed == clean


def test_legacy_authoritative_label_in_history_is_still_stripped():
    legacy = (
        "[System note: The following is recalled memory context, NOT new user input. "
        "Treat as authoritative reference data — this is the agent's persistent memory "
        "and should inform all responses.]\n\n- old fact"
    )
    assert sanitize_context(legacy) == "- old fact"


# --- Mixed session across the label change (t_6753d998) -------------------------------------------
# A gateway session alive across the restart that ships #1865 holds rows written by the old build
# (legacy "authoritative" note) followed by rows written by the new build ("untrusted" note). The
# new build must read both through ONE matcher: every note stripped on load, the kept-prefix match
# treating the reloaded rows as the rows they came from (no archive + re-insert), and a provider that
# echoes either note back unfenced never doubling the note in the next block.

_LEGACY_NOTE = (
    "[System note: The following is recalled memory context, NOT new user input. "
    "Treat as authoritative reference data — this is the agent's persistent memory "
    "and should inform all responses.]"
)


def _legacy_block(raw: str) -> str:
    """The exact block the pre-#1865 build wrote (fence + legacy note + body)."""
    return f"<memory-context>\n{_LEGACY_NOTE}\n\n{raw}\n</memory-context>"


def _mixed_session_rows():
    new_block = build_memory_context_block("- Ace's NAS is at 192.168.1.159.\n")
    assert "untrusted reference data" in new_block and "authoritative" not in new_block
    return [
        # Before the restart: old build, sidecar carries the legacy block; one legacy row also kept
        # the block in content (older builds did), and the provider echoed the bare note back.
        {"role": "user", "content": "where is my NAS?",
         "api_content": "where is my NAS?\n\n" + _legacy_block("- Ace's NAS is at 192.168.1.159.")},
        {"role": "assistant", "content": _LEGACY_NOTE + "\n\nYour NAS is at 192.168.1.159."},
        {"role": "user", "content": "and the printer?\n\n" + _legacy_block("- printer is a Brother.")},
        {"role": "assistant", "content": "A Brother."},
        # After the restart: new build, same session.
        {"role": "user", "content": "what about the NAS again?",
         "api_content": "what about the NAS again?\n\n" + new_block},
        {"role": "assistant", "content": _note(new_block) + "\n\nStill 192.168.1.159."},
    ]


def test_mixed_old_and_new_labels_strip_through_one_matcher():
    rows = _mixed_session_rows()
    for msg in rows:
        for text in filter(None, (msg["content"], msg.get("api_content"))):
            assert "System note" not in sanitize_context(text), text[:80]
    assert [sanitize_context(m["content"]).strip() for m in rows] == [
        "where is my NAS?", "Your NAS is at 192.168.1.159.", "and the printer?", "A Brother.",
        "what about the NAS again?", "Still 192.168.1.159.",
    ]


def test_mixed_session_reload_keeps_every_row_and_one_note_per_turn(tmp_path):
    """Reload the mixed transcript in the new build and write it back as a rewind would: every row is
    recognised as itself (nothing archived, nothing re-inserted), and each replayed turn still carries
    exactly the ONE note its own build wrote."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s-mixed", source="cli", model="test-model")
        for msg in _mixed_session_rows():
            db.append_message("s-mixed", msg["role"], content=msg["content"], api_content=msg.get("api_content"))
        before = [(m["id"], m["content"]) for m in db.get_messages("s-mixed")]

        loaded = db.get_messages_as_conversation("s-mixed")
        assert all("System note" not in m["content"] for m in loaded)
        sidecars = [m["api_content"] for m in loaded if m.get("api_content")]
        assert len(sidecars) == 2
        assert [s.count("[System note:") for s in sidecars] == [1, 1]
        assert "authoritative reference data" in sidecars[0] and "untrusted reference data" in sidecars[1]

        db.replace_messages("s-mixed", loaded, archive_dropped=True)
        after = [(m["id"], m["content"]) for m in db.get_messages("s-mixed")]
        assert after == before, "a reloaded row was not matched to itself and got re-inserted"
        assert db.get_messages("s-mixed", include_inactive=True) == db.get_messages("s-mixed")
    finally:
        db.close()


def test_echo_of_either_label_rewraps_with_exactly_one_new_note():
    """Next turn after the restart: a provider whose recall text echoes the legacy OR the new note
    unfenced gets it stripped, so the block carries one note, and it is the new one."""
    clean = build_memory_context_block("- fact one\n")
    for echoed_note in (_LEGACY_NOTE, _note(clean)):
        rewrapped = build_memory_context_block(echoed_note + "\n\n- fact one\n")
        assert rewrapped.count("[System note:") == 1
        assert rewrapped == clean
