"""Tests for /update gateway slash command.

Tests both the _handle_update_command handler (spawns update process) and
the _send_update_notification startup hook (sends results after restart).
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890", thread_id=None):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    return runner


# ---------------------------------------------------------------------------
# _handle_update_command
# ---------------------------------------------------------------------------


class TestHandleUpdateCommand:
    """Tests for GatewayRunner._handle_update_command."""

    @pytest.mark.asyncio
    async def test_no_git_directory(self, tmp_path):
        """Returns an error when .git does not exist."""
        runner = _make_runner()
        event = _make_event()
        # Point _hermes_home to tmp_path and project_root to a dir without .git
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run.Path") as MockPath:
            # Path(__file__).parent.parent.resolve() -> fake_root
            MockPath.return_value = MagicMock()
            MockPath.__truediv__ = Path.__truediv__
            # Easier: just patch the __file__ resolution in the method
            pass

        # Simpler approach — mock at method level using a wrapper
        runner = _make_runner()

        with patch("gateway.run._hermes_home", tmp_path):
            # The handler does Path(__file__).parent.parent.resolve()
            # We need to make project_root / '.git' not exist.
            # Since Path(__file__) resolves to the real gateway/run.py,
            # project_root will be the real hermes-agent dir (which HAS .git).
            # Patch Path to control this.
            original_path = Path

            class FakePath(type(Path())):
                pass

            # Actually, simplest: just patch the specific file attr.
            # The _handle_update_command handler lives in gateway/slash_commands.py
            # (extracted from run.py in the god-file decomposition); it resolves
            # project_root via Path(__file__).parent.parent, so fake that file.
            fake_file = str(fake_root / "gateway" / "slash_commands.py")
            (fake_root / "gateway").mkdir(parents=True)
            (fake_root / "gateway" / "slash_commands.py").touch()

            with patch("gateway.slash_commands.__file__", fake_file):
                result = await runner._handle_update_command(event)

        assert "Not a git repository" in result


    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_fallback(self):
        """_resolve_hermes_bin falls back to sys.executable argv when which fails."""
        import sys
        from gateway.run import _resolve_hermes_bin

        fake_spec = MagicMock()
        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec):
            result = _resolve_hermes_bin()

        assert result == [sys.executable, "-m", "hermes_cli.main"]


    @pytest.mark.asyncio
    async def test_writes_pending_marker(self, tmp_path):
        """Writes .update_pending.json with correct platform and chat info."""
        runner = _make_runner()
        event = _make_event(platform=Platform.TELEGRAM, chat_id="99999")
        event.message_id = "m-update"

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/hermes" if x == "hermes" else "/usr/bin/setsid"), \
             patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        pending_path = hermes_home / ".update_pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        assert data["platform"] == "telegram"
        assert data["chat_id"] == "99999"
        assert data["chat_type"] == "dm"
        assert data["message_id"] == "m-update"
        assert "timestamp" in data
        assert not (hermes_home / ".update_exit_code").exists()


    @pytest.mark.asyncio
    async def test_fallback_when_no_setsid(self, tmp_path):
        """Falls back to start_new_session=True when setsid is not available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()

        def which_no_setsid(x):
            if x == "hermes":
                return "/usr/bin/hermes"
            if x == "setsid":
                return None
            return None

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=which_no_setsid), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # Verify plain bash -c fallback (no nohup, no setsid)
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "bash"
        assert "nohup" not in call_args[2]
        assert ".update_exit_code" in call_args[2]
        # start_new_session=True should be in kwargs
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True
        assert "Starting Hermes update" in result


# ---------------------------------------------------------------------------
# Platform allowlist gate
# ---------------------------------------------------------------------------


class TestUpdateCommandPlatformGate:
    """Tests for the platform-allowlist gate at the top of
    ``_handle_update_command``.  Built-in messaging platforms are listed in
    ``_UPDATE_ALLOWED_PLATFORMS``; plugin-migrated platforms (discord,
    mattermost, teams, …) are NOT in the frozenset and rely on the
    registry's ``allow_update_command=True`` fallback.  Programmatic
    interfaces (ACP, API server, webhooks) must be blocked.
    """


    @pytest.mark.asyncio
    async def test_allows_plugin_platform_via_registry_fallback(self, monkeypatch):
        """A plugin-migrated platform (DISCORD) is no longer in
        ``_UPDATE_ALLOWED_PLATFORMS`` but must still pass the gate via
        the registry's ``allow_update_command=True`` flag.

        This test is the empirical guarantee that removing DISCORD from
        the hardcoded frozenset does not regress the /update command for
        Discord users.
        """
        from gateway.run import GatewayRunner

        # Precondition: DISCORD is NOT in the hardcoded set anymore.
        assert Platform.DISCORD not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        # Make sure the plugin registry is populated so the fallback fires.
        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        discord_entry = platform_registry.get("discord")
        assert discord_entry is not None
        assert discord_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.DISCORD)
        monkeypatch.setenv("HERMES_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        # The gate must NOT have rejected us — anything other than the
        # ``platform_not_messaging`` rejection string is acceptable here.
        # Later steps may legitimately return success ("Starting Hermes
        # update…") or fail for environment reasons.
        assert "only available from messaging platforms" not in result


    @pytest.mark.asyncio
    async def test_allows_homeassistant_via_registry_fallback(self, monkeypatch):
        """Same as DISCORD/MATTERMOST: HOMEASSISTANT is now plugin-migrated
        (PR #40709) and not in the hardcoded frozenset; the registry must
        keep /update working via ``allow_update_command=True``.
        """
        from gateway.run import GatewayRunner

        assert Platform.HOMEASSISTANT not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        ha_entry = platform_registry.get("homeassistant")
        assert ha_entry is not None
        assert ha_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.HOMEASSISTANT)
        monkeypatch.setenv("HERMES_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        assert "only available from messaging platforms" not in result


# ---------------------------------------------------------------------------
# _send_update_notification
# ---------------------------------------------------------------------------


class TestSendUpdateNotification:
    """Tests for GatewayRunner._send_update_notification."""


    @pytest.mark.asyncio
    async def test_defers_notification_while_update_still_running(self, tmp_path):
        """Returns False and keeps marker files when the update has not exited yet."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("still running")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_recovers_from_claimed_pending_file(self, tmp_path):
        """A claimed pending file from a crashed notifier is still deliverable."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        claimed_path = hermes_home / ".update_pending.claimed.json"
        claimed_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_called_once()
        assert not claimed_path.exists()

    @pytest.mark.asyncio
    async def test_sends_notification_with_output(self, tmp_path):
        """Sends update output to the correct platform and chat."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        # Write pending marker
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
            "timestamp": "2026-03-04T21:00:00",
        }
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text(
            "→ Found 3 new commit(s)\n✓ Code updated!\n✓ Update complete!"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        # Mock the adapter
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "67890"  # chat_id
        assert "Update complete" in call_args[0][1] or "update finished" in call_args[0][1].lower()


    @pytest.mark.asyncio
    async def test_cleans_up_on_error(self, tmp_path):
        """Files are cleaned up even if notification fails."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }))
        output_path.write_text("✓ Done")
        exit_code_path.write_text("0")

        # Adapter send raises
        mock_adapter = AsyncMock()
        mock_adapter.send.side_effect = RuntimeError("network error")
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        # Files should still be cleaned up (finally block)
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


    @pytest.mark.asyncio
    async def test_no_adapter_for_platform_preserves_markers(self, tmp_path):
        """A finished update whose platform is offline keeps its markers.

        When the target platform's adapter has not reconnected yet, dropping
        the completion markers would silently lose the notification. Instead the
        call defers (returns False) and leaves every marker on disk so a later
        retry can deliver once the platform is back.
        """
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_text("Done")
        exit_code_path.write_text("0")

        # Only telegram adapter available, but pending says discord
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        # No send (wrong platform offline) and the result is deferred.
        assert result is False
        mock_adapter.send.assert_not_called()
        # Markers are preserved for a later retry — NOT cleaned up.
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        # The marker stays in its canonical pending location (claim restored).
        assert not (hermes_home / ".update_pending.claimed.json").exists()


# ---------------------------------------------------------------------------
# _send_update_notification — bounded retry for undeliverable markers
# ---------------------------------------------------------------------------


def _fake_config(enabled=(Platform.TELEGRAM,), multiplex=False):
    """A minimal stand-in for GatewayConfig with just what the notifier reads."""
    from gateway.config import PlatformConfig

    cfg = SimpleNamespace(
        platforms={p: PlatformConfig(enabled=True) for p in enabled},
        multiplex_profiles=multiplex,
    )
    return cfg


def _write_markers(hermes_home, pending, *, age_seconds=0.0, exit_code="0",
                   output="Done"):
    """Write a full marker set, aged ``age_seconds`` into the past."""
    pending = dict(pending)
    pending.setdefault(
        "timestamp",
        (datetime.now() - timedelta(seconds=age_seconds)).isoformat(),
    )
    pending_path = hermes_home / ".update_pending.json"
    output_path = hermes_home / ".update_output.txt"
    exit_code_path = hermes_home / ".update_exit_code"
    pending_path.write_text(json.dumps(pending))
    output_path.write_text(output)
    exit_code_path.write_text(exit_code)
    return pending_path, output_path, exit_code_path


class TestUpdateNotificationRetryIsBounded:
    """An undeliverable update notification must reach a terminal state.

    Regression for the stranded ``.update_pending.json`` that addressed a
    NEVER-configured mattermost adapter and re-logged "adapter not connected
    yet" every 2s across reboots for 3.2 days (22,884 lines = 60% of
    gateway.log).
    """

    @pytest.mark.asyncio
    async def test_unconfigured_platform_marker_is_abandoned(self, tmp_path):
        """The live incident: mattermost is not configured, marker is 4 days old."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        # Byte-for-byte the stranded marker found on the host (fixture ids).
        pending_path, output_path, exit_code_path = _write_markers(
            hermes_home,
            {
                "platform": "mattermost", "chat_id": "67890", "chat_type": "dm",
                "user_id": "12345",
                "session_key": "agent:main:mattermost:dm:67890",
            },
            age_seconds=4 * 24 * 3600,
            exit_code="124",
        )

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        # Terminal: the caller must stop retrying.
        assert result is True
        mock_adapter.send.assert_not_called()
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_unconfigured_platform_within_grace_still_defers(self, tmp_path):
        """A fresh marker still defers — adapters may just be mid-startup."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path, output_path, exit_code_path = _write_markers(
            hermes_home,
            {"platform": "mattermost", "chat_id": "111", "user_id": "222"},
            age_seconds=30.0,
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_configured_but_disconnected_platform_still_defers(self, tmp_path):
        """The original intent: a real transient disconnect keeps its markers.

        This is the regression the existing code comment warns about — a
        configured platform that has not reconnected yet must NOT lose its
        completion notification.
        """
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path, output_path, exit_code_path = _write_markers(
            hermes_home,
            {"platform": "telegram", "chat_id": "111", "user_id": "222"},
            # Far past the unconfigured grace period — being configured is
            # what earns the long retry window, not being recent.
            age_seconds=3 * 3600,
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_configured_platform_delivers_once_adapter_returns(self, tmp_path):
        """After deferring, the same markers still deliver when the adapter is back."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        _write_markers(
            hermes_home,
            {"platform": "telegram", "chat_id": "111", "user_id": "222"},
            age_seconds=3 * 3600,
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            assert await runner._send_update_notification() is False

            mock_adapter = AsyncMock()
            runner.adapters = {Platform.TELEGRAM: mock_adapter}
            assert await runner._send_update_notification() is True

        mock_adapter.send.assert_called_once()
        assert not (hermes_home / ".update_pending.json").exists()

    @pytest.mark.asyncio
    async def test_configured_platform_marker_expires_after_max_age(self, tmp_path):
        """Even a configured platform gets a terminal state eventually."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path, output_path, exit_code_path = _write_markers(
            hermes_home,
            {"platform": "telegram", "chat_id": "111", "user_id": "222"},
            age_seconds=7 * 24 * 3600,
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_multiplex_keeps_the_long_window_for_unseen_platforms(self, tmp_path):
        """A secondary profile's platform is invisible here — never short-cap it.

        Under multiplexing a platform can be enabled only in a secondary
        profile's config.yaml, so "absent from this config" does not mean
        "not configured".
        """
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,), multiplex=True)
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path, _, _ = _write_markers(
            hermes_home,
            {"platform": "mattermost", "chat_id": "111", "user_id": "222"},
            age_seconds=3 * 3600,
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_unknown_marker_age_never_short_caps(self, tmp_path):
        """An unparseable timestamp must not shorten anyone's retry window."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path, _, _ = _write_markers(
            hermes_home,
            {"platform": "mattermost", "chat_id": "111", "user_id": "222",
             "timestamp": "not-a-timestamp"},
        )
        runner.adapters = {}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        # mtime is the fallback age source, and the file was just written.
        assert result is False
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_deferral_logging_is_throttled_not_silenced(self, tmp_path, caplog):
        """Repeated deferrals collapse to one line — but stay above DEBUG.

        The bug logged at 0.5Hz forever; lowering the level would only hide
        the loop. The first deferral must still be visible at INFO.
        """
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        _write_markers(
            hermes_home,
            {"platform": "telegram", "chat_id": "111", "user_id": "222"},
            age_seconds=60.0,
        )
        runner.adapters = {}

        caplog.set_level(logging.INFO, logger="gateway.run")
        with patch("gateway.run._hermes_home", hermes_home):
            for _ in range(20):
                assert await runner._send_update_notification() is False

        deferrals = [
            r for r in caplog.records
            if "Update notification deferred" in r.getMessage()
        ]
        assert len(deferrals) == 1, (
            f"20 deferrals produced {len(deferrals)} log lines: "
            f"{[r.getMessage() for r in deferrals]}"
        )
        assert deferrals[0].levelno >= logging.INFO

    @pytest.mark.asyncio
    async def test_unconfigured_expires_sooner_than_configured(self, tmp_path):
        """The two ceilings must actually differ, at the same marker age.

        Being unconfigured is what earns the short window. If both states share
        one ceiling, an unconfigured platform still retries for a day — which
        at a 2s poll is ~43,000 log lines, i.e. the bug at reduced volume.
        """
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        age = 3 * 3600  # past the unconfigured grace, well inside the max age

        async def _run(platform_value):
            for marker in hermes_home.glob(".update_*"):
                marker.unlink()
            _write_markers(
                hermes_home,
                {"platform": platform_value, "chat_id": "111", "user_id": "222"},
                age_seconds=age,
            )
            runner = _make_runner()
            runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
            runner.adapters = {}
            with patch("gateway.run._hermes_home", hermes_home):
                return await runner._send_update_notification()

        # mattermost is absent from the config -> unconfigured -> abandoned.
        assert await _run("mattermost") is True
        # telegram is enabled -> configured -> still deferring at the same age.
        assert await _run("telegram") is False

    @pytest.mark.asyncio
    async def test_deferral_throttle_resets_after_a_delivery(self, tmp_path, caplog):
        """A NEW deferral streak must log again, or the throttle becomes silence.

        Throttling a repeated line is fine; suppressing the first line of every
        future streak is just the DEBUG-level infinite loop wearing a hat.
        """
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        def _arm():
            _write_markers(
                hermes_home,
                {"platform": "telegram", "chat_id": "111", "user_id": "222"},
                age_seconds=60.0,
            )

        caplog.set_level(logging.INFO, logger="gateway.run")
        with patch("gateway.run._hermes_home", hermes_home):
            # Streak 1: disconnected.
            _arm()
            runner.adapters = {}
            for _ in range(5):
                assert await runner._send_update_notification() is False
            # Adapter returns and the notification is delivered.
            mock_adapter = AsyncMock()
            runner.adapters = {Platform.TELEGRAM: mock_adapter}
            assert await runner._send_update_notification() is True
            # Streak 2: a later update, same target, disconnected again.
            _arm()
            runner.adapters = {}
            for _ in range(5):
                assert await runner._send_update_notification() is False

        deferrals = [
            r for r in caplog.records
            if "Update notification deferred" in r.getMessage()
        ]
        assert len(deferrals) == 2, (
            "expected one log line per deferral streak, got "
            f"{len(deferrals)}: {[r.getMessage() for r in deferrals]}"
        )

    @pytest.mark.asyncio
    async def test_still_running_marker_expires_after_max_age(self, tmp_path):
        """The sibling deferral at the same site needs the same ceiling.

        Within one boot the watcher bounds this: after its 30-minute deadline
        it writes exit_code=124 itself. Across boots it does not — a gateway
        that restarts more often than that deadline re-arms the watcher every
        time and never reaches the write, so a pending marker whose update
        process is dead defers forever. `hermes update` does not run for a day.
        """
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
            "timestamp": (datetime.now() - timedelta(days=4)).isoformat(),
        }))
        # No .update_exit_code — the update never reported an outcome.
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_not_called()
        assert not pending_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_still_running_marker_within_max_age_still_defers(self, tmp_path):
        """A genuinely in-flight update keeps its markers and keeps deferring."""
        runner = _make_runner()
        runner.config = _fake_config(enabled=(Platform.TELEGRAM,))
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
            "timestamp": (datetime.now() - timedelta(minutes=10)).isoformat(),
        }))
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()


# ---------------------------------------------------------------------------
# /update in help and known_commands
# ---------------------------------------------------------------------------


class TestUpdateInHelp:
    """Verify /update appears in help text and known commands set."""


    def test_update_is_known_command(self):
        """The /update command is in the help text (proxy for _known_commands)."""
        # _known_commands is local to _handle_message, so we verify by
        # checking the help output includes it.
        from gateway.run import GatewayRunner
        import inspect
        source = inspect.getsource(GatewayRunner._handle_message)
        assert '"update"' in source
