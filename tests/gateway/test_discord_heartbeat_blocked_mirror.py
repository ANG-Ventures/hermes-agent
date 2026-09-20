"""discord.py's heartbeat-blocked detector must reach gateway.log.

discord.py's keep-alive thread already logs ``heartbeat blocked for more than
N seconds`` on the ``discord.gateway`` logger, optionally with the loop
thread's traceback appended to the same message. ``discord.gateway`` is not in
gateway.log's component allowlist, so those records only ever land in
errors.log and operators reading the main gateway log never connect them to
the ``latency_exceeded`` forced reconnects they cause.

The adapter installs a mirror handler that re-emits the detection through its
own (gateway-component) logger as a single structured line.
"""

from __future__ import annotations

import logging

import pytest

import plugins.platforms.discord.adapter as discord_platform


ADAPTER_LOGGER = "plugins.platforms.discord.adapter"

# The exact message shape discord.py's KeepAliveHandler.run emits.
BLOCKED_MESSAGE = "Shard ID None heartbeat blocked for more than 10 seconds."


@pytest.fixture
def mirror_installed(monkeypatch):
    """Install the mirror on a clean process-global flag, then tear it down."""
    monkeypatch.setattr(
        discord_platform, "_HEARTBEAT_BLOCKED_MIRROR_INSTALLED", False, raising=False
    )
    discord_logger = logging.getLogger("discord.gateway")
    before = list(discord_logger.handlers)
    discord_platform._install_discord_heartbeat_blocked_mirror()
    try:
        yield discord_logger
    finally:
        for handler in list(discord_logger.handlers):
            if handler not in before:
                discord_logger.removeHandler(handler)


def _emit(discord_logger: logging.Logger, message: str) -> None:
    """Push a synthetic record through the real ``discord.gateway`` logger."""
    record = discord_logger.makeRecord(
        "discord.gateway", logging.WARNING, __file__, 0, message, (), None
    )
    discord_logger.handle(record)


def _mirrored_lines(caplog) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == ADAPTER_LOGGER and "PHASE=event_loop_blocked" in rec.getMessage()
    ]


def test_blocked_warning_is_mirrored_with_seconds_and_repo_site(
    mirror_installed, caplog
):
    """A blocked warning with a traceback yields seconds + innermost repo frame."""
    repo_root = discord_platform._discord_repo_root()
    message = (
        BLOCKED_MESSAGE
        + "\nLoop thread traceback (most recent call last):\n"
        + f'  File "{repo_root}/gateway/run.py", line 100, in _outer\n'
        + "    inner()\n"
        + f'  File "{repo_root}/plugins/platforms/discord/adapter.py", line 4242, in _blocking_call\n'
        + "    subprocess.run(cmd)\n"
        + '  File "/usr/lib/python3.11/subprocess.py", line 550, in run\n'
        + "    with Popen(*popenargs) as process:\n"
        + '  File "/opt/venv/lib/python3.11/site-packages/discord/gateway.py", line 170, in run\n'
        + "    self.block_msg\n"
    )

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        _emit(mirror_installed, message)

    lines = _mirrored_lines(caplog)
    assert lines, "heartbeat-blocked warning was not mirrored"

    structured = lines[0]
    assert "PHASE=event_loop_blocked" in structured
    assert "platform=discord" in structured
    assert "seconds=10" in structured
    # Deepest REPO frame wins: the stdlib subprocess.py and the site-packages
    # discord/gateway.py frames below it must not be selected.
    assert (
        f"site={repo_root}/plugins/platforms/discord/adapter.py:4242 _blocking_call"
        in structured
    )
    assert "subprocess.py" not in structured
    assert "site-packages" not in structured

    # The traceback text itself is preserved (same record or a second one).
    assert any("Loop thread traceback" in line for line in lines)


def test_blocked_warning_without_traceback_reports_unknown_site(
    mirror_installed, caplog
):
    """discord.py could not grab the loop stack -> site=unknown, still mirrored."""
    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        _emit(mirror_installed, BLOCKED_MESSAGE)

    lines = _mirrored_lines(caplog)
    assert len(lines) == 1
    assert "seconds=10" in lines[0]
    assert "site=unknown" in lines[0]


def test_unrelated_discord_gateway_record_is_not_mirrored(mirror_installed, caplog):
    """Only heartbeat-blocked records are mirrored; ordinary chatter is not."""
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_LOGGER):
        _emit(mirror_installed, "Shard ID None has connected to Gateway.")

    assert _mirrored_lines(caplog) == []


def test_install_is_idempotent(monkeypatch):
    """Repeated adapter connects must not stack duplicate mirror handlers."""
    monkeypatch.setattr(
        discord_platform, "_HEARTBEAT_BLOCKED_MIRROR_INSTALLED", False, raising=False
    )
    discord_logger = logging.getLogger("discord.gateway")
    before = list(discord_logger.handlers)
    try:
        assert discord_platform._install_discord_heartbeat_blocked_mirror() is True
        assert discord_platform._install_discord_heartbeat_blocked_mirror() is False

        added = [h for h in discord_logger.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], discord_platform._DiscordHeartbeatBlockedMirror)
    finally:
        for handler in list(discord_logger.handlers):
            if handler not in before:
                discord_logger.removeHandler(handler)


def test_connect_installs_the_mirror(monkeypatch):
    """The adapter's connect() path is what arms the mirror in production.

    Driving the real ``DiscordAdapter.connect`` with discord.py reported
    unavailable exercises the install call and returns immediately, so this
    stays a runtime test with no source reading and no network.
    """
    import asyncio

    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(
        discord_platform, "_HEARTBEAT_BLOCKED_MIRROR_INSTALLED", False, raising=False
    )
    monkeypatch.setattr(discord_platform, "DISCORD_AVAILABLE", False, raising=False)

    discord_logger = logging.getLogger("discord.gateway")
    before = list(discord_logger.handlers)

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    # ``name`` is a read-only property derived from ``platform``.
    from gateway.config import Platform

    adapter.platform = Platform.DISCORD

    def _noop_fatal(*_args, **_kwargs):
        return None

    adapter._set_fatal_error = _noop_fatal  # type: ignore[method-assign]

    try:
        assert asyncio.run(DiscordAdapter.connect(adapter)) is False
        added = [h for h in discord_logger.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], discord_platform._DiscordHeartbeatBlockedMirror)
    finally:
        for handler in list(discord_logger.handlers):
            if handler not in before:
                discord_logger.removeHandler(handler)
