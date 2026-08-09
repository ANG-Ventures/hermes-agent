"""Tests for the optional systemd event-loop watchdog protocol."""

from __future__ import annotations

import asyncio
import socket

import pytest


def _abstract_unix_sockets_supported() -> bool:
    """True only where a LEADING-NUL (abstract namespace) bind actually works.

    hasattr(socket, "AF_UNIX") is the wrong probe: macOS and the BSDs have
    AF_UNIX but NOT Linux's abstract namespace, so the bind below raises
    FileNotFoundError and the test fails for platform reasons rather than
    behaviour. systemd's NOTIFY_SOCKET="@name" form is Linux-only by
    definition, so skipping elsewhere loses no coverage.
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        probe.bind("\0hermes-abstract-ns-probe")
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.mark.skipif(
    not _abstract_unix_sockets_supported(),
    reason="abstract-namespace Unix sockets are Linux-only (systemd @-sockets)",
)
def test_notify_supports_systemd_abstract_socket(monkeypatch):
    name = "\0hermes-test-notify"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(name)
    receiver.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "@hermes-test-notify")

    try:
        from gateway.systemd_notify import notify

        assert notify("WATCHDOG=1") is True
        assert receiver.recv(4096) == b"WATCHDOG=1"
    finally:
        receiver.close()


def test_notify_uses_nonblocking_datagram_send(monkeypatch):
    calls: list[object] = []

    class _Sender:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setblocking(self, value):
            calls.append(("setblocking", value))

        def connect(self, address):
            calls.append(("connect", address))

        def send(self, payload):
            calls.append(("send", payload))

    import gateway.systemd_notify as notify_mod

    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setattr(notify_mod.socket, "socket", lambda *_args: _Sender())

    assert notify_mod.notify("READY=1") is True
    assert calls[0] == ("setblocking", False)


@pytest.mark.asyncio
async def test_watchdog_sends_ready_heartbeat_and_stopping(monkeypatch):
    calls: list[str] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", "20000")

    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(lag_tolerance_seconds=1.0)

    assert watchdog.start() is True
    assert watchdog.ready("Gateway running") is True
    await asyncio.sleep(0.04)
    await watchdog.stop()

    assert any(message.startswith("READY=1") for message in calls)
    assert "WATCHDOG=1" in calls
    assert calls[-1] == "STOPPING=1"
    assert watchdog.unhealthy is False


