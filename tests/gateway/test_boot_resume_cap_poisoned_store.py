"""A broken attempt store must neither cap innocent sessions nor stop capping.

Two review rounds on PR #761 found the two opposite failures, both measured
through the real boot-resume scheduler:

* **Round 1.** A poisoned ``auto_resume_attempts.json`` read as ``1_000_000``
  attempts, so ``count >= max`` was true for EVERY session on the host —
  including ones that had never resumed. At the time the cap's skip branch
  also cleared ``resume_pending``, so one corrupt file stripped restart
  continuity host-wide and destroyed the markers on the way out. That clear is
  gone (round 3 — see ``test_boot_resume_cap_keeps_recovery_context.py``); the
  innocent-session half below is still the contract.
* **Round 2.** The fix flipped the polarity: a store fault answered "unknown",
  the cap never fired, and ``_invalid`` latched with no write path ever
  repairing the file. Measured with the incident's own shape
  (``reason=restart_interrupted`` → ``kind=self``), cap=3:
  ``HEALTHY sched total=3``, ``POISONED total=10``, ``UNWRITABLE total=10``.
  Ten full-transcript replays is the incident this card exists to fix.

Both are reachable at once because the two faults are different:

* unreadable → the file is a recoverable counter cache, so it is REPAIRED to
  an empty store. The session honestly reads zero attempts, resumes, keeps its
  marker — and the cap counts again from this boot, so it still bounds.
* unwritable → nothing can be counted, ever, so the verdict denies the resume
  rather than replaying unbounded. ``resume_pending`` is LEFT SET, because the
  denial is about the disk and must evaporate when the disk is fixed.

Store-grain contracts for the same properties live in
``test_boot_resume_attempt_cap.py``; this file pins the scheduler grain, which
is where both defects actually landed.
"""

from __future__ import annotations

import logging

import pytest

from tests.gateway.test_boot_resume_attempt_cap import (
    _INTERRUPTED_TAIL,
    _boot,
    _remark,
    _runner,
    _seed,
    _source,
)


def _poison(runner) -> None:
    """Corrupt the on-disk store the way a torn write or bad edit would."""
    store = runner._get_auto_resume_attempt_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")


def _make_unwritable(runner) -> None:
    """Break persistence the way a read-only dir or a full disk would.

    Patched at ``_write`` rather than via filesystem permissions so the test
    runs identically as root and on every platform.
    """
    store = runner._get_auto_resume_attempt_store()

    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    store._write = _boom
    store._persist_proven = False


def _drive_boots(runner, db, entry, boots: int) -> list[int]:
    """Run ``boots`` consecutive boots, re-marking the session each time."""
    scheduled: list[int] = []
    for _ in range(boots):
        _remark(runner, entry)
        scheduled.append(_boot(runner))
    return scheduled


# --------------------------------------------------------------------------
# Unreadable: must not cap the innocent (round 1) AND must still bound (round 2)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_store_does_not_cap_a_session_that_never_resumed(
    tmp_path, monkeypatch, caplog
):
    """Round-1 regression: a store fault must not deny an innocent session."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    _poison(runner)

    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert _boot(runner) == 1, "a never-resumed session must still resume"

    messages = [record.getMessage() for record in caplog.records]
    assert not any("cause=attempt_cap" in m for m in messages)
    assert not any("cause=cap_unaccountable" in m for m in messages)
    db.close()


@pytest.mark.asyncio
async def test_unreadable_store_leaves_the_resume_pending_marker_intact(
    tmp_path, monkeypatch
):
    """Clearing the marker is irreversible; a store fault must never trigger it."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    _poison(runner)

    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    _boot(runner)

    refreshed = runner.session_store._entries[entry.session_key]
    assert refreshed.resume_pending is True
    db.close()


@pytest.mark.asyncio
async def test_unreadable_store_does_not_starve_every_session_on_the_host(
    tmp_path, monkeypatch
):
    """The measured round-1 blast radius: host-wide, not one session."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    alice = runner.session_store.get_or_create_session(_source("alice"))
    bob = runner.session_store.get_or_create_session(_source("bob"))
    _seed(db, alice, _INTERRUPTED_TAIL)
    _seed(db, bob, _INTERRUPTED_TAIL)
    _poison(runner)

    _remark(runner, alice)
    _remark(runner, bob)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 2
    db.close()


@pytest.mark.asyncio
async def test_unreadable_store_still_bounds_the_replay_across_ten_boots(
    tmp_path, monkeypatch
):
    """Round-2 regression, THE count that matters: 10 boots must not be 10 replays.

    This is the reviewer's probe, asserted on the replay count rather than on a
    log line — the count is what the bridge paged on. The repair means the
    first boot is not denied (round 1 stands) but the cap resumes counting
    immediately, so the session is bounded from this boot forward.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    _poison(runner)
    await runner._prepare_boot_resume_work_check()

    scheduled = _drive_boots(runner, db, entry, 10)

    assert sum(scheduled) == 3, f"expected the cap to bound the replay, got {scheduled}"
    assert scheduled[:3] == [1, 1, 1]
    assert scheduled[3:] == [0] * 7
    db.close()


# --------------------------------------------------------------------------
# Unwritable: cannot self-heal, so it must deny rather than replay forever
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwritable_store_does_not_replay_ten_times(
    tmp_path, monkeypatch, caplog
):
    """Round-2 regression, the arm that cannot repair itself.

    Measured at ``total=10 caps=0`` before this fix — the incident restated,
    for the incident's own session shape. With no way to count, the only
    bounded answer is to stop scheduling.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    await runner._prepare_boot_resume_work_check()
    _make_unwritable(runner)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        scheduled = _drive_boots(runner, db, entry, 10)

    assert sum(scheduled) == 0, f"a store that cannot count must not replay: {scheduled}"
    messages = [record.getMessage() for record in caplog.records]
    assert any("cause=cap_unaccountable" in m for m in messages)
    # Named honestly as "unknown", never as a spent budget.
    assert not any("cause=attempt_cap" in m for m in messages)
    db.close()


@pytest.mark.asyncio
async def test_unwritable_store_keeps_the_marker_so_the_denial_is_reversible(
    tmp_path, monkeypatch
):
    """The session is owed a resume it cannot account for — do not retire it.

    A disk fault must not irreversibly consume restart continuity. Leaving the
    marker set is what makes fixing the disk sufficient to recover, with no
    human touching ``sessions.json``.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    await runner._prepare_boot_resume_work_check()
    _make_unwritable(runner)

    _remark(runner, entry)
    assert _boot(runner) == 0
    assert runner.session_store._entries[entry.session_key].resume_pending is True

    # Disk fixed: a fresh store instance (i.e. the next gateway process) resumes
    # the session normally, with no manual marker surgery.
    runner._auto_resume_attempt_store = None
    assert _boot(runner) == 1
    db.close()


# --------------------------------------------------------------------------
# Control: a healthy store is unaffected by any of the above
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_store_bounds_the_incident_at_the_cap(tmp_path, monkeypatch):
    """Control arm — the card's headline requirement, measured, not asserted."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    await runner._prepare_boot_resume_work_check()

    scheduled = _drive_boots(runner, db, entry, 10)

    assert scheduled == [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    db.close()
