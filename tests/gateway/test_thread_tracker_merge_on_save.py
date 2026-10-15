"""The thread-participation registry must MERGE with disk on save, never clobber it.

2026-10-13: a gateway's in-memory ``discord_threads.json`` registry held 4 IDs
while the file on disk held 377 (a restore had landed after the tracker read the
file at construction).  The next ``mark`` wrote the 4-entry snapshot whole and
the 373 others were gone.  The oracle in every test here is the DURABLE FILE.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from gateway.platforms.helpers import ThreadParticipationTracker


@pytest.fixture()
def state(tmp_path, monkeypatch):
    path = tmp_path / "discord_threads.json"
    monkeypatch.setattr(ThreadParticipationTracker, "_state_path", lambda self: path, raising=True)
    return path


def _disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_four_in_memory_ids_do_not_clobber_377_on_disk(state):
    """Reproduces the 2026-10-13 incident shape exactly: 4 in memory, 377 on disk."""
    state.write_text(json.dumps([f"m{i}" for i in range(3)]))
    tracker = ThreadParticipationTracker("discord")  # reads 3 at construction
    restored = [f"r{i}" for i in range(377)]
    state.write_text(json.dumps(restored))  # the restore lands after construction

    tracker.mark("m3")  # in-memory registry is now 4 IDs

    persisted = _disk(state)
    assert set(restored) <= set(persisted), f"lost {377 - len(set(restored) & set(persisted))} on-disk IDs"
    assert {"m0", "m1", "m2", "m3"} <= set(persisted)
    assert len(persisted) == 381
    assert persisted[-1] == "m3", "the newest mark must be the most recent entry (trim keeps the tail)"
    assert "r0" in tracker, "the merged on-disk IDs must also be live in memory"


def test_save_writes_0600(state):
    tracker = ThreadParticipationTracker("discord")
    tracker.mark("a")
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o600


def test_reload_merges_disk_into_memory_without_writing(state):
    state.write_text(json.dumps(["a"]))
    tracker = ThreadParticipationTracker("discord")
    tracker._threads["mem-only"] = None
    state.write_text(json.dumps(["a", "b"]))
    before = state.read_bytes()

    asyncio.run(tracker.reload_async())

    assert "b" in tracker and "mem-only" in tracker and "a" in tracker
    assert state.read_bytes() == before, "reload must not write"


def test_discard_is_not_resurrected_by_the_merge(state):
    state.write_text(json.dumps(["a", "b", "c"]))
    tracker = ThreadParticipationTracker("discord")

    asyncio.run(tracker.discard_async("b"))
    assert _disk(state) == ["a", "c"]
    tracker.mark("d")
    assert _disk(state) == ["a", "c", "d"]
    assert "b" not in tracker


def test_re_mark_after_discard_persists(state):
    state.write_text(json.dumps(["a", "b"]))
    tracker = ThreadParticipationTracker("discord")
    asyncio.run(tracker.discard_async("b"))
    tracker.mark("b")
    assert _disk(state) == ["a", "b"]


def test_two_trackers_on_one_file_keep_each_others_ids(state):
    one = ThreadParticipationTracker("discord")
    two = ThreadParticipationTracker("discord")
    one.mark("x")
    two.mark("y")
    one.mark("z")
    assert set(_disk(state)) == {"x", "y", "z"}


def test_merged_set_is_still_capped_keeping_the_newest(state):
    state.write_text(json.dumps([f"r{i}" for i in range(10)]))
    tracker = ThreadParticipationTracker("discord", max_tracked=5)
    tracker.mark("new")
    persisted = _disk(state)
    assert len(persisted) == 5 and persisted[-1] == "new"


def test_corrupt_file_degrades_to_empty_and_is_rewritten(state):
    state.write_text("{not json")
    tracker = ThreadParticipationTracker("discord")
    tracker.mark("a")
    assert _disk(state) == ["a"]


def test_discord_connect_reloads_the_registry():
    """connect() must re-read the registry before it starts serving (source contract)."""
    import inspect

    from plugins.platforms.discord.adapter import DiscordAdapter

    src = inspect.getsource(DiscordAdapter.connect)
    assert "self._threads.reload_async()" in src
    assert src.index("reload_async") < src.index("intents = Intents.default()")
