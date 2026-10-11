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
