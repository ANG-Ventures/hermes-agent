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


def test_discord_connect_reloads_the_registry():  # noqa: source-proxy ordering inside connect(), which needs a live Discord gateway; the reload itself is exercised in this file
    """connect() must re-read the registry before it starts serving (source contract)."""
    import inspect

    from plugins.platforms.discord.adapter import DiscordAdapter

    src = inspect.getsource(DiscordAdapter.connect)
    assert "self._threads.reload_async()" in src
    assert src.index("reload_async") < src.index("intents = Intents.default()")


def test_fresh_mark_of_an_old_disk_id_is_not_trimmed(state):
    """A new mark of an ID near the head of the restored file must land as newest."""
    state.write_text(json.dumps(["m"]))
    tracker = ThreadParticipationTracker("discord", max_tracked=3)
    state.write_text(json.dumps(["t", "u", "v"]))

    tracker.mark("t")

    assert _disk(state)[-1] == "t"
    assert "t" in tracker


def test_overlapping_saves_from_two_trackers_keep_both_ids(state, monkeypatch):
    """Both persists read the old file first unless the transaction is serialized per path."""
    import threading

    import gateway.platforms.helpers as helpers

    state.write_text("[]")
    one = ThreadParticipationTracker("discord")
    two = ThreadParticipationTracker("discord")
    both_merged = threading.Barrier(2, timeout=1)
    real_write = helpers.atomic_json_write

    def write_after_both_merged(*args, **kwargs):
        try:
            both_merged.wait()
        except threading.BrokenBarrierError:
            pass  # serialized: the other save cannot reach here until this one lands
        return real_write(*args, **kwargs)

    monkeypatch.setattr(helpers, "atomic_json_write", write_after_both_merged)

    async def main():
        await asyncio.gather(one.mark_async("x"), two.mark_async("y"))

    asyncio.run(main())
    assert set(_disk(state)) == {"x", "y"}


def test_save_waits_for_the_cross_process_file_lock(state):
    """A sibling process holding ``<file>.lock`` must hold this tracker's persist off."""
    import threading

    from tools.skill_usage import skill_file_lock

    tracker = ThreadParticipationTracker("discord")
    lock_path = state.parent / (state.name + ".lock")
    holding, release = threading.Event(), threading.Event()

    def sibling():
        with skill_file_lock(lock_path):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=sibling)
    holder.start()
    assert holding.wait(5)
    saver = threading.Thread(target=tracker.mark, args=("a",))
    saver.start()
    saver.join(0.3)
    assert saver.is_alive() and not state.exists(), "persist ran while another holder had the file lock"
    release.set()
    saver.join(5)
    holder.join(5)
    assert _disk(state) == ["a"]


def test_re_mark_of_an_id_trimmed_while_pending_is_kept(state):
    """Pending a,b,c + a reload trims a; re-marking a must persist it as newest."""
    tracker = ThreadParticipationTracker("discord", max_tracked=2)
    for thread_id in ("a", "b", "c"):
        tracker._remember(thread_id)  # accepted, persist still owed
    tracker.reload()
    assert "a" not in tracker

    tracker.mark("a")

    assert _disk(state) == ["c", "a"]
    assert "a" in tracker


def test_re_mark_during_an_in_flight_write_stays_owed(state, monkeypatch):
    """A discard + re-mark of x while x's write is landing must not be cleared by it."""
    import gateway.platforms.helpers as helpers

    tracker = ThreadParticipationTracker("discord", max_tracked=2)
    real_write = helpers.atomic_json_write
    fired = []

    def write_with_interleaved_marks(*args, **kwargs):
        real_write(*args, **kwargs)
        if not fired:
            fired.append(True)
            for thread_id in ("y", "z"):
                tracker._remember(thread_id)
            with tracker._lock:  # discard_async's in-memory half
                del tracker._threads["x"]
                tracker._fresh.pop("x", None)
                tracker._discarded.add("x")
            tracker._remember("x")

    monkeypatch.setattr(helpers, "atomic_json_write", write_with_interleaved_marks)
    tracker.mark("x")
    monkeypatch.setattr(helpers, "atomic_json_write", real_write)

    tracker.mark("w")  # next persist carries every still-owed mark

    persisted = _disk(state)
    assert persisted[-2:] == ["x", "w"], persisted


def test_failed_persist_is_retried_by_the_next_mark(state, monkeypatch):
    """A lock timeout / write error must not leave the mark accepted but never saved."""
    import gateway.platforms.helpers as helpers

    tracker = ThreadParticipationTracker("discord")
    real_write = helpers.atomic_json_write

    def failing_write(*args, **kwargs):
        raise OSError(36, "Resource deadlock avoided")  # msvcrt LK_LOCK timeout shape

    monkeypatch.setattr(helpers, "atomic_json_write", failing_write)
    with pytest.raises(OSError):
        asyncio.run(tracker.mark_async("a"))
    assert not state.exists()
    monkeypatch.setattr(helpers, "atomic_json_write", real_write)

    asyncio.run(tracker.mark_async("a"))  # same thread, next message

    assert _disk(state) == ["a"]



def test_fresh_mark_trimmed_from_memory_does_not_delete_it_from_disk(state):
    """Prism 2cac297da412 on #2007: an ID can sit in ``_fresh`` (its persist is still owed) while a
    trim or reload already dropped it from ``_threads``. The merge must then leave it in the on-disk
    order, not exclude it: it is excluded from disk only when memory will re-append it. Oracle: the
    durable file. State is set up directly (the shape is reached via a failed save + trim)."""
    state.write_text(json.dumps(["p", "r", "s"]))      # a sibling writer persisted p; r, s are live
    tracker = ThreadParticipationTracker("discord", max_tracked=3)
    assert set(tracker._threads) == {"p", "r", "s"}
    # the owed mark of `p` survived a trim that dropped `p` from memory (failed persist, then marks
    # of r, s, t with capacity 3 is one way there); reproduce that memory state exactly:
    tracker._fresh = {"p": 1}
    tracker._threads = dict.fromkeys(["r", "s"])
    tracker._save()                                    # merge disk [p,r,s] + memory [r,s], capacity 3
    persisted = _disk(state)
    assert "p" in persisted, f"`p` was on disk with capacity to spare and memory never re-appended it: {persisted}"
    assert persisted == ["p", "r", "s"]
    assert "p" not in tracker._fresh, "a dropped fresh mark is no longer owed"
