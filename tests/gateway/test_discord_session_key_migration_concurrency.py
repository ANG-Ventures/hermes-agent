"""Discord session-key migration must resolve chat types with bounded concurrency.

On a live store with 311 distinct Discord chat_ids, serial `get_chat_info`
lookups delayed the first boot-resume decision to 68-75s after gateway start
(the startup-restore 30s watchdog fired on every boot). These tests pin the
concurrency, the dedupe, and the error-isolation behavior.
"""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


class FakeDiscordAdapter:
    def __init__(self, *, fail_ids=(), error_ids=(), delay=0.05):
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._fail_ids = set(fail_ids)
        self._error_ids = set(error_ids)
        self._delay = delay

    async def get_chat_info(self, chat_id):
        self.calls.append(chat_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            if chat_id in self._fail_ids:
                raise RuntimeError(f"boom {chat_id}")
            if chat_id in self._error_ids:
                return {"error": "x"}
            return {"type": "thread"}
        finally:
            self.in_flight -= 1


def _entries():
    entries = [
        SimpleNamespace(session_key=f"agent:main:discord:thread:{i}:{i}", origin=None)
        for i in range(40)
    ]
    # duplicates of existing chat_ids -- must not produce extra lookups
    entries.append(SimpleNamespace(session_key="agent:main:discord:thread:0:0", origin=None))
    entries.append(SimpleNamespace(session_key="agent:main:discord:thread:1:1", origin=None))
    # non-discord key -- must be filtered out
    entries.append(SimpleNamespace(session_key="agent:main:telegram:chat:999:999", origin=None))
    return entries


def _make_runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    recorded = {}

    def _migrate(chat_types):
        recorded["chat_types"] = dict(chat_types)
        return 0

    runner.session_store = SimpleNamespace(
        snapshot_entries=lambda: _entries(),
        migrate_discord_session_keys=_migrate,
    )
    return runner, recorded


def test_lookups_run_with_bounded_concurrency():
    adapter = FakeDiscordAdapter()
    runner, _ = _make_runner(adapter)

    start = time.monotonic()
    asyncio.run(runner._migrate_discord_session_keys())
    elapsed = time.monotonic() - start

    # Serial would be >= 40 * 0.05 = 2.0s
    assert elapsed < 1.0, f"lookups appear serial (elapsed={elapsed:.2f}s)"
    assert adapter.max_in_flight > 1, "no concurrency observed"
    assert adapter.max_in_flight <= 8, f"unbounded concurrency: {adapter.max_in_flight}"


def test_store_receives_distinct_discord_chat_ids_only():
    adapter = FakeDiscordAdapter()
    runner, recorded = _make_runner(adapter)

    asyncio.run(runner._migrate_discord_session_keys())

    expected = {str(i): "thread" for i in range(40)}
    assert recorded["chat_types"] == expected
    assert sorted(adapter.calls) == sorted(expected)
    assert len(adapter.calls) == 40, "each chat_id must be looked up exactly once"


def test_one_failing_lookup_does_not_abort_the_others(caplog):
    adapter = FakeDiscordAdapter(fail_ids={"7"})
    runner, recorded = _make_runner(adapter)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        asyncio.run(runner._migrate_discord_session_keys())

    chat_types = recorded["chat_types"]
    assert "7" not in chat_types
    assert len(chat_types) == 39
    assert all(v == "thread" for v in chat_types.values())
    assert any(
        "Discord session migration lookup failed" in r.message for r in caplog.records
    )


def test_error_payload_is_not_recorded():
    adapter = FakeDiscordAdapter(error_ids={"3"})
    runner, recorded = _make_runner(adapter)

    asyncio.run(runner._migrate_discord_session_keys())

    assert "3" not in recorded["chat_types"]
    assert len(recorded["chat_types"]) == 39
