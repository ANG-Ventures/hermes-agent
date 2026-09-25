"""A still-running update marker must reach a terminal state across gateway restarts.

Within one boot the watcher caps the wait by writing exit_code=124 after its deadline. Across
boots it does not: a gateway restarting faster than that deadline re-arms the watcher every time,
so a marker whose update process died deferred forever. Ported from fork PR #582.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from tests.gateway.test_update_command import _make_runner


def _write_pending(home, **age):
    pending_path = home / ".update_pending.json"
    pending_path.write_text(json.dumps({
        "platform": "telegram", "chat_id": "111", "user_id": "222",
        "timestamp": (datetime.now() - timedelta(**age)).isoformat(),
    }))
    return pending_path


@pytest.mark.asyncio
async def test_still_running_marker_expires_after_max_age(tmp_path):
    runner = _make_runner()
    home = tmp_path / "hermes"
    home.mkdir()
    pending_path = _write_pending(home, days=4)  # no .update_exit_code: update never finished
    mock_adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: mock_adapter}
    with patch("gateway.run._hermes_home", home):
        result = await runner._send_update_notification()
    assert result is True  # definitive: the startup caller stops rescheduling
    mock_adapter.send.assert_not_called()
    assert not pending_path.exists()
    assert not (home / ".update_pending.claimed.json").exists()


@pytest.mark.asyncio
async def test_still_running_marker_within_max_age_still_defers(tmp_path):
    runner = _make_runner()
    home = tmp_path / "hermes"
    home.mkdir()
    pending_path = _write_pending(home, minutes=10)
    mock_adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: mock_adapter}
    with patch("gateway.run._hermes_home", home):
        result = await runner._send_update_notification()
    assert result is False
    mock_adapter.send.assert_not_called()
    assert pending_path.exists()
    assert not (home / ".update_pending.claimed.json").exists()


@pytest.mark.asyncio
async def test_still_running_marker_without_timestamp_keeps_deferring(tmp_path):
    """Unknown age fails open: never discard a marker we cannot date."""
    runner = _make_runner()
    home = tmp_path / "hermes"
    home.mkdir()
    pending_path = home / ".update_pending.json"
    pending_path.write_text(json.dumps({"platform": "telegram", "chat_id": "111"}))
    runner.adapters = {Platform.TELEGRAM: AsyncMock()}
    with patch("gateway.run._hermes_home", home):
        result = await runner._send_update_notification()
    assert result is False
    assert pending_path.exists()
