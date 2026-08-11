"""Session-persistence failure must name the REAL cause, not guess "full disk".

Regression origin (2026-08-10): a manual /compress on a 792-message session was
interrupted by a gateway restart armed by a DIFFERENT session (SIGTERM 73s in).
The incremental session append could not complete, so the turn ended with
``_turn_exit_reason = "session_persistence_failed"`` and the user was told:

    "This is often a full disk — free some space (or fix state.db permissions)"

Measured at that moment: disk 16% used with 6.1 TiB free, state.db 2.7 GB with
normal permissions, and ZERO sqlite errors in the log all day. The message sent
the operator after a problem that did not exist.
"""

from __future__ import annotations

import types

import pytest

import run_agent
from run_agent import AIAgent


REASON = "session_persistence_failed"


def _explain(agent=None):
    return AIAgent._format_turn_completion_explanation(REASON, agent)


@pytest.fixture(autouse=True)
def _clear_shutdown_flag(monkeypatch):
    """Each test controls the shutdown signal itself."""
    from gateway import shutdown_forensics as sf
    monkeypatch.setattr(sf, "_SHUTDOWN_SIGNAL_AT", None, raising=False)
    yield


# --- the incident: a restart, NOT a disk problem ---------------------------


def test_restart_mid_turn_is_named_and_disk_is_not_blamed(monkeypatch):
    from gateway import shutdown_forensics as sf
    import time as _time
    monkeypatch.setattr(sf, "_SHUTDOWN_SIGNAL_AT", _time.monotonic(), raising=False)

    agent = types.SimpleNamespace(_session_persistence_error=None)
    out = _explain(agent)

    assert "restarted mid-turn" in out or "shut down or restarted" in out
    # The whole point: stop sending people after a disk that is fine.
    assert "full disk" not in out.lower()
    assert "free some space" not in out.lower()
    assert "nothing was lost" in out.lower()


def test_stale_shutdown_signal_is_not_blamed(monkeypatch):
    """A signal from an hour ago must not explain a failure happening now."""
    from gateway import shutdown_forensics as sf
    import time as _time
    monkeypatch.setattr(
        sf, "_SHUTDOWN_SIGNAL_AT", _time.monotonic() - 3600.0, raising=False
    )
    agent = types.SimpleNamespace(_session_persistence_error=None)
    out = _explain(agent)
    # It must not ASSERT a restart happened. The fallback text legitimately
    # suggests checking for one, so assert on the claim, not the substring.
    assert "shut down or restarted mid-turn, so the" not in out
    assert "not identified" in out.lower()


# --- a REAL disk-full error must still say so ------------------------------


def test_genuine_disk_full_is_still_reported():
    import errno
    exc = OSError(errno.ENOSPC, "No space left on device")
    agent = types.SimpleNamespace(_session_persistence_error=exc)
    out = _explain(agent)
    assert "disk is full" in out.lower()
    assert "free some space" in out.lower()


def test_sqlite_disk_full_string_is_detected():
    agent = types.SimpleNamespace(
        _session_persistence_error=Exception("database or disk is full")
    )
    out = _explain(agent)
    assert "disk is full" in out.lower()


# --- unknown cause: say so, don't invent one -------------------------------


def test_unknown_cause_is_admitted_not_guessed():
    agent = types.SimpleNamespace(
        _session_persistence_error=RuntimeError("some other failure")
    )
    out = _explain(agent)
    assert "not identified" in out.lower()
    # It should still hand over the real exception text to act on.
    assert "some other failure" in out
    assert "RuntimeError" in out
    # And must NOT assert disk-full.
    assert "the disk is full" not in out.lower()


def test_no_agent_supplied_still_renders_and_does_not_blame_disk():
    """Back-compat: existing callers invoke this unbound with one arg."""
    out = _explain(None)
    assert out
    assert "session storage could not be written" in out
    assert "the disk is full" not in out.lower()


# --- the flag's own contract ----------------------------------------------


def test_snapshot_arms_the_flag_only_for_a_real_signal(monkeypatch):
    from gateway import shutdown_forensics as sf
    import signal as _signal

    monkeypatch.setattr(sf, "_SHUTDOWN_SIGNAL_AT", None, raising=False)
    sf.snapshot_shutdown_context(None)
    assert sf.shutdown_signal_seen_at() is None, "a non-signal snapshot must not arm it"

    sf.snapshot_shutdown_context(_signal.SIGTERM)
    assert sf.shutdown_signal_seen_at() is not None
    assert sf.shutdown_landed_within(300.0) is True


def test_shutdown_landed_within_is_false_when_never_signalled(monkeypatch):
    from gateway import shutdown_forensics as sf
    monkeypatch.setattr(sf, "_SHUTDOWN_SIGNAL_AT", None, raising=False)
    assert sf.shutdown_landed_within(300.0) is False
    assert sf.shutdown_signal_seen_at() is None
