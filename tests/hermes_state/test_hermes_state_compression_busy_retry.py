"""Appends flow freely during compression; the commit preserves them (#75316).

HISTORY: ``append_message`` used to refuse while another writer held the
session's compression lock, with a short busy-wait (#75264 → #75083). That
fenced ordinary transcript writes behind a lease whose real job is stopping
two COMPRESSIONS colliding — turns died as ``session_persistence_failed``
whenever a slow provider summary overlapped an incoming message (#74568,
#77386), and a stale lock from a dead PID blocked writes for the full TTL.

CURRENT CONTRACT (watermark commit): appends never check compression_locks.
``archive_and_compact()`` takes a watermark captured at compression start and
re-sequences every row that arrived after it (the concurrent tail) back into
the live transcript, atomically, instead of archiving it with the snapshot.
The commit itself is holder-fenced: a compression whose lease was lost cannot
publish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_state_errors import CompressionSessionBusyError


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "state.db")
    d.create_session("sess1", source="test")
    return d


@pytest.fixture
def lease_waits(db: SessionDB, monkeypatch: pytest.MonkeyPatch) -> list:
    """Witness for "the writer waited on a lease": every lease/lock retry in
    ``_execute_write`` sleeps through ``_sleep_before_write_retry``. Record the
    calls and refuse the retry, so a writer that takes the wait path surfaces
    as its busy error on the first collision instead of as elapsed seconds
    (a stopwatch bound here measured 0.70 s on a loaded CI shard with no
    behavioural change: cold first-write setup, not a lease wait)."""
    waits: list = []

    def _no_wait(deadline: float, patience_s: float) -> bool:
        waits.append(patience_s)
        return False

    monkeypatch.setattr(db, "_sleep_before_write_retry", _no_wait)
    return waits


def _append_without_waiting(db: SessionDB, lease_waits: list, content: str) -> None:
    try:
        db.append_message("sess1", role="user", content=content)
    except CompressionSessionBusyError as exc:
        pytest.fail(f"append waited on a compression lease: {exc!r} (waits={lease_waits})")
    assert lease_waits == [], "append must not wait on a compression lease"


def test_append_is_never_blocked_by_a_foreign_compression_lock(db: SessionDB, lease_waits: list) -> None:
    """The classic race: a steer lands while compression owns the session.

    Old behavior: busy-wait then land (or die on timeout). New behavior: the
    append lands IMMEDIATELY — the watermark commit is what protects it.
    """
    assert db.try_acquire_compression_lock("sess1", "compressor") is True

    _append_without_waiting(db, lease_waits, "steered mid-compression")
    rows = db.get_messages("sess1")
    assert any(r["content"] == "steered mid-compression" for r in rows)


def test_append_is_never_blocked_by_a_stale_dead_pid_lock(db: SessionDB, lease_waits: list) -> None:
    """A crashed compressor's unexpired lock must not fence writes (#74568)."""
    assert db.try_acquire_compression_lock(
        "sess1", "pid-9999999-long-gone", ttl_seconds=3600
    ) is True
    _append_without_waiting(db, lease_waits, "lands despite stale lock")
    rows = db.get_messages("sess1")
    assert any(r["content"] == "lands despite stale lock" for r in rows)


def test_the_lock_owner_append_still_works(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "compressor") is True
    db.append_message(
        "sess1",
        role="assistant",
        content="written by the compressor",
        compression_lock_holder="compressor",
    )
    rows = db.get_messages("sess1")
    assert any(r["content"] == "written by the compressor" for r in rows)






def test_a_lost_compression_lease_still_fails_fast(db: SessionDB, lease_waits: list) -> None:
    """``publish_compression_child`` with a lost lease is permanent — no retry."""
    with pytest.raises(CompressionSessionBusyError):
        db.publish_compression_child(
            parent_session_id="sess1",
            child_session_id="child1",
            source="test",
            messages=[{"role": "user", "content": "compacted"}],
            compression_lock_holder="not-the-holder",
            require_compression_lease=True,
        )
    assert lease_waits == [], (
        "a lost lease is permanent and must not spend the retry budget"
    )
