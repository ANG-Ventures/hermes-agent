"""Platform reconnect backoff policy regression tests."""

from gateway.config import Platform
from gateway.run import _reconnect_backoff


def test_discord_reconnect_backoff_caps_at_two_minutes():
    assert [_reconnect_backoff(attempt, Platform.DISCORD) for attempt in range(1, 8)] == [
        30,
        60,
        120,
        120,
        120,
        120,
        120,
    ]


def test_non_discord_reconnect_backoff_keeps_five_minute_cap():
    assert [_reconnect_backoff(attempt, Platform.TELEGRAM) for attempt in range(1, 8)] == [
        30,
        60,
        120,
        240,
        300,
        300,
        300,
    ]
