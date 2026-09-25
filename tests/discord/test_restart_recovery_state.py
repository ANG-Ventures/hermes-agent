"""Phase 1 (drain-window recovery, SPEC D-4): durable restart-recovery state.

Unit tests for _DiscordRestartRecoveryState — the active-channel map +
shutdown_ts anchor, atomic-JSON persisted, bounded, debounced. No message
content or ids are stored (privacy, AC-12).
"""
import json
import importlib


def _load_state_class(tmp_path, monkeypatch):
    """Import the class with HERMES_HOME pointed at a tmp dir."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # hermes_constants.get_hermes_home reads the env at call time, so a fresh
    # import isn't required — but import lazily to avoid heavy adapter deps.
    from plugins.platforms.discord.adapter import _DiscordRestartRecoveryState
    return _DiscordRestartRecoveryState


def test_recovery_state_roundtrip(tmp_path, monkeypatch):
    """Active map + shutdown_ts survive a write → new instance → reload."""
    cls = _load_state_class(tmp_path, monkeypatch)
    s = cls()
    s.mark_channel_active("chan-A", now=1000.0)
    s.mark_channel_active("chan-B", now=1001.0)
    s.flush(shutdown_ts=1002.0)

    # Fresh instance loads from disk (simulates the post-restart process).
    s2 = cls()
    assert set(s2.recent_channels(lookback_s=10_000, now=1002.0)) == {"chan-A", "chan-B"}
    assert s2.shutdown_ts == 1002.0


def test_recovery_state_bounded(tmp_path, monkeypatch):
    """The active map is trimmed to the N most-recently-active channels."""
    cls = _load_state_class(tmp_path, monkeypatch)
    s = cls(max_channels=3, persist_interval_s=0.0)
    for i in range(10):
        s.mark_channel_active(f"chan-{i}", now=float(i))
    # Only the 3 newest (chan-7, chan-8, chan-9) survive.
    kept = set(s.recent_channels(lookback_s=1_000, now=100.0))
    assert kept == {"chan-7", "chan-8", "chan-9"}


def test_recent_channels_respects_lookback(tmp_path, monkeypatch):
    """recent_channels filters by the lookback window."""
    cls = _load_state_class(tmp_path, monkeypatch)
    s = cls()
    s.mark_channel_active("fresh", now=1000.0)
    s.mark_channel_active("stale", now=100.0)
    recent = s.recent_channels(lookback_s=500, now=1000.0)
    assert recent == ["fresh"]  # stale is 900s old, outside 500s window


def test_debounced_persist_and_flush(tmp_path, monkeypatch):
    """Coalesced background persist: a burst of marks lands on disk once, off
    the calling thread, and flush always writes synchronously."""
    cls = _load_state_class(tmp_path, monkeypatch)
    s = cls(persist_interval_s=10.0)
    s.mark_channel_active("c1", now=1.0)   # leading edge: written in background
    assert s._writer.wait_idle()
    assert "c1" in set(cls().recent_channels(lookback_s=10_000, now=1.0))

    # Inside the interval: in memory now, NOT on disk yet (trailing edge).
    s.mark_channel_active("c2", now=2.0)
    assert "c2" in s.recent_channels(lookback_s=10_000, now=2.0)
    assert "c2" not in set(cls().recent_channels(lookback_s=10_000, now=2.0))

    # flush always writes regardless of the coalescing window.
    s.flush()
    assert "c2" in set(cls().recent_channels(lookback_s=10_000, now=2.0))


def test_recovery_state_no_content(tmp_path, monkeypatch):
    """Privacy (AC-12): the persisted file contains only channel ids +
    timestamps + shutdown_ts — no message content, no message ids."""
    cls = _load_state_class(tmp_path, monkeypatch)
    s = cls(persist_interval_s=0.0)
    s.mark_channel_active("chan-X", now=5.0)
    s.flush(shutdown_ts=6.0)

    state_file = tmp_path / "gateway" / "discord_restart_recovery.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert set(data.keys()) == {"active_channels", "shutdown_ts"}
    assert data["active_channels"] == {"chan-X": 5.0}
    assert data["shutdown_ts"] == 6.0


def test_corrupt_state_file_fails_soft(tmp_path, monkeypatch):
    """A corrupt state file loads soft to empty (fail-open), no crash."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = tmp_path / "gateway"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "discord_restart_recovery.json").write_text("{not valid json")

    from plugins.platforms.discord.adapter import _DiscordRestartRecoveryState
    s = _DiscordRestartRecoveryState()
    assert s.recent_channels(lookback_s=10_000, now=1.0) == []
    assert s.shutdown_ts is None
