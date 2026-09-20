"""An unreadable attempt store must not cap sessions that never resumed.

Review finding (2026-09-20, PR #761 round 1). The per-session cap read a
poisoned ``auto_resume_attempts.json`` as ``1_000_000`` attempts, so
``count >= max`` was true for EVERY session on the host — including sessions
that had never been resumed once. The cap's skip branch also clears
``resume_pending``, so a single corrupt file permanently stripped restart
continuity from the whole host and destroyed the markers on the way out.

"Unknown" and "over budget" are different answers. A store fault is evidence
about the STORE, never about a session's budget, so it must not cap and must
not retire a marker. The bounded degradation for a poisoned store already
exists and is owned by ``has_attempt``, which fails closed and drops the
resume from ``auto`` to ``prompt`` — the transcript survives and the user sees
a banner.

Store-grain contracts for the same property live in
``test_boot_resume_attempt_cap.py``; this file pins the scheduler grain, which
is where the damage actually landed.
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


@pytest.mark.asyncio
async def test_poisoned_store_does_not_cap_a_session_that_never_resumed(
    tmp_path, monkeypatch, caplog
):
    """THE regression: a store fault must not deny resume to an innocent session."""
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
    db.close()


@pytest.mark.asyncio
async def test_poisoned_store_leaves_the_resume_pending_marker_intact(
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
async def test_poisoned_store_does_not_starve_every_session_on_the_host(
    tmp_path, monkeypatch
):
    """The measured blast radius: host-wide, not one session."""
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
