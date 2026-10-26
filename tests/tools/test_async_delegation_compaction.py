"""Registry hot path (Phase 2): cached reads keyed on file stat, compaction of delivered bodies.

The fixture mirrors the live registry's shape (147 records, ~150 delivered outbox events whose
payload carries the full batch ``results`` with tool traces, a duplicate archive in
``terminal.result``, 2 pending events) with synthetic text only.
"""
from __future__ import annotations

import json
import os
import queue
import random
import sys
import time
from pathlib import Path

import pytest

from tests.tools import test_async_delegation_terminal_receipts as helpers
from tools import async_delegation as ad
from tools import async_delegation_store as store
from tools.process_registry_notifications import format_process_notification

N_RECORDS = 147
DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_cache()
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    yield
    ad._reset_for_tests()
    _clear_cache()


def _clear_cache() -> None:
    getattr(store, "_REGISTRY_CACHE", {}).clear()


def _compacted(payload) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("payload_sha256"), str)


def _text(rng: random.Random, size: int) -> str:
    words = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")
    out, n = [], 0
    while n < size:
        w = rng.choice(words)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)[:size]


def _child(rng: random.Random, index: int, size: int) -> dict:
    return {
        "task_index": index, "status": "completed", "model": "test-model", "exit_reason": "completed",
        "summary": _text(rng, int(size * 0.4)),
        "tool_trace": [{"tool": "terminal", "args": _text(rng, 200), "result": _text(rng, 600)}
                       for _ in range(max(1, int(size * 0.6) // 860))],
        "duration_seconds": 12.5, "api_calls": 4,
    }


def _record(rng: random.Random, i: int, now: float, *, state: str, age: float, pending: bool,
            payload_kb: int) -> dict:
    delegation_id = f"deleg_{i:08x}"
    attempt_id = f"{delegation_id}:g0:abcd1234"
    goal = f"synthetic goal {i} " + _text(rng, 1500)
    children = [_child(rng, 0, payload_kb * 1024)]
    updated = now - age
    record = {
        "delegation_id": delegation_id, "state": state, "created_at": updated - 300,
        "updated_at": updated, "profile": "default",
        "source": {"kind": "single", "tasks": [{"goal": goal, "role": "leaf"}],
                   "shared_context": _text(rng, 2000)},
        "execution": {"model": "test-model", "provider": "test-provider",
                      "toolsets": ["terminal", "file", "web"]},
        "route": {"session_key": f"sk-{i}", "parent_session_id": f"parent-{i}", "platform": "telegram"},
        "attempt": {"attempt_id": attempt_id, "generation": 0, "redispatch_count": 0,
                    "owner_boot_id": "100:1.0", "started_at": updated - 300, "submitted_at": updated - 299,
                    "last_interrupted_at": None, "last_error": None},
        "terminal": None, "outbox": [],
        "lifecycle": [{"event": "spawned", "ts": updated - 300}],
    }
    if state == "running":
        if pending:  # a restart notice still owed to the parent
            record["outbox"].append({
                "event_id": f"{delegation_id}:restarted:g0", "type": "async_delegation_restarted",
                "state": "pending", "queued_boot_id": None, "created_at": now - 60,
                "delivered_at": None, "drop_reason": None,
                "payload": {"type": "async_delegation_restarted", "delegation_id": delegation_id,
                            "attempt_id": attempt_id, "attempt_generation": 0, "redispatch_count": 0,
                            "event_id": f"{delegation_id}:restarted:g0", "goal": goal, "goals": [goal],
                            "profile": "default"}})
        return record
    status = "completed" if state == "done" else "error"
    event_id = f"{delegation_id}:terminal:g0"
    payload = {
        "type": "async_delegation", "delegation_id": delegation_id, "attempt_id": attempt_id,
        "attempt_generation": 0, "redispatch_count": 0, "died_before_start": False,
        "session_key": f"sk-{i}", "parent_session_id": f"parent-{i}", "platform": "telegram",
        "profile": "default", "goal": goal, "goals": [goal], "context": record["source"]["shared_context"],
        "toolsets": ["terminal", "file", "web"], "role": "leaf", "model": "test-model",
        "status": status, "summary": None, "error": None, "api_calls": 4, "duration_seconds": 12.5,
        "dispatched_at": updated - 300, "completed_at": updated, "exit_reason": None,
        "is_batch": True, "results": children, "total_duration_seconds": 12.5, "event_id": event_id,
    }
    record["terminal"] = {"status": status, "completed_at": updated, "error": None,
                          "result": {"results": json.loads(json.dumps(children)),
                                     "total_duration_seconds": 12.5, "live_transcripts": []}}
    record["outbox"].append({
        "event_id": event_id, "type": "async_delegation",
        "state": "pending" if pending else "delivered", "queued_boot_id": "100:1.0",
        "created_at": updated, "delivered_at": None if pending else updated + 5,
        "drop_reason": None, "payload": payload})
    return record


def _build_registry(now: float) -> dict:
    """147 records: 142 done/failed (+ restart notices), 3 running, 2 pending events."""
    rng = random.Random(1234)
    records = {}
    for i in range(N_RECORDS):
        if i < 3:
            rec = _record(rng, i, now, state="running", age=120, pending=(i == 0), payload_kb=0)
        elif i == 3:  # a fresh completion not yet delivered
            rec = _record(rng, i, now, state="done", age=30, pending=True, payload_kb=60)
        else:
            age = 3600 if i < 20 else 2 * DAY + i * 600  # most are past the 24 h compaction age
            rec = _record(rng, i, now, state="failed" if i % 40 == 0 else "done", age=age,
                          pending=False, payload_kb=20 + (i * 37) % 141)
            if i % 16 == 0:  # delivered restart notices like the live file's
                rec["outbox"].insert(0, {
                    "event_id": f"{rec['delegation_id']}:restarted:g0", "type": "async_delegation_restarted",
                    "state": "delivered", "queued_boot_id": "100:1.0", "created_at": rec["created_at"],
                    "delivered_at": rec["created_at"] + 1, "drop_reason": None,
                    "payload": {"type": "async_delegation_restarted", "delegation_id": rec["delegation_id"],
                                "attempt_id": rec["attempt"]["attempt_id"], "attempt_generation": 0,
                                "event_id": f"{rec['delegation_id']}:restarted:g0",
                                "goal": rec["source"]["tasks"][0]["goal"],
                                "goals": [rec["source"]["tasks"][0]["goal"]], "profile": "default"}})
        records[rec["delegation_id"]] = rec
    return {"schema_version": store.SCHEMA_VERSION, "updated_at": now, "records": records}


def _seed(now: float) -> tuple[Path, dict]:
    registry = _build_registry(now)
    path = store.registry_path()
    store._prepare_directory(path)
    store._write(path, registry)
    _clear_cache()  # as if a fresh process started on this file
    return path, registry


def _pending_events(registry: dict) -> dict[str, str]:
    return {
        event["event_id"]: json.dumps(event, sort_keys=True)
        for record in registry["records"].values()
        for event in record.get("outbox") or []
        if event.get("state") == "pending"
    }


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


# (a) ──────────────────────────────────────────────────────────────────────────

def test_unchanged_registry_parses_and_checksums_once(monkeypatch):
    path, _ = _seed(time.time())
    assert path.stat().st_size > 8_000_000, path.stat().st_size
    loads, checksums = [], []
    real_loads, real_checksum = store.json.loads, store._record_checksum
    monkeypatch.setattr(store.json, "loads", lambda *a, **k: loads.append(1) or real_loads(*a, **k))
    monkeypatch.setattr(store, "_record_checksum",
                        lambda r: checksums.append(1) or real_checksum(r))
    first = None
    for _ in range(100):
        with store.locked_registry(write=False) as registry:
            assert len(registry["records"]) == N_RECORDS
            first = first if first is not None else registry
            assert registry is first  # the shared read-only snapshot
    assert len(loads) == 1
    assert len(checksums) == N_RECORDS  # exactly one checksum pass


def test_writer_gets_private_copy_never_the_cache():
    _seed(time.time())
    with store.locked_registry(write=False) as cached:
        pass
    with store.locked_registry() as writable:
        assert writable is not cached
        writable["records"]["deleg_00000000"]["state"] = "mutated-in-flight"
        assert cached["records"]["deleg_00000000"]["state"] == "running"
        writable["records"]["deleg_00000000"]["state"] = "running"


# (b) ──────────────────────────────────────────────────────────────────────────

def test_own_write_refreshes_cache():
    _seed(time.time())
    with store.locked_registry(write=False) as before:
        assert before["records"]["deleg_00000001"]["attempt"]["last_error"] is None
    with store.locked_registry() as registry:
        registry["records"]["deleg_00000001"]["attempt"]["last_error"] = "own-write"
    with store.locked_registry(write=False) as after:
        assert after["records"]["deleg_00000001"]["attempt"]["last_error"] == "own-write"
        assert "_invalid_record_ids" not in after


def test_other_process_replace_forces_reparse(monkeypatch):
    path, _ = _seed(time.time())
    with store.locked_registry(write=False):
        pass
    # Another process: different object graph, written with a fresh checksum, atomically replaced.
    other = json.loads(path.read_text())
    other["records"]["deleg_00000002"]["attempt"]["last_error"] = "other-process"
    other["records"]["deleg_00000002"]["integrity"] = store._record_checksum(other["records"]["deleg_00000002"])
    tmp = path.with_name("other.tmp")
    tmp.write_text(json.dumps(other, indent=2))
    os.replace(tmp, path)
    loads = []
    real_loads = store.json.loads
    monkeypatch.setattr(store.json, "loads", lambda *a, **k: loads.append(1) or real_loads(*a, **k))
    with store.locked_registry(write=False) as registry:
        assert registry["records"]["deleg_00000002"]["attempt"]["last_error"] == "other-process"
    assert len(loads) == 1


def test_same_size_same_mtime_rewrite_caught_by_content_hash():
    path, _ = _seed(time.time())
    with store.locked_registry(write=False):
        pass
    st = path.stat()
    raw = path.read_bytes()
    needle = b'"last_error": null'
    at = raw.index(needle)
    forged = raw[:at] + b'"last_error": 1234' + raw[at + len(needle):]
    assert len(forged) == len(raw)
    with open(path, "r+b") as fh:  # in place: same inode, same size
        fh.write(forged)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime tick
    with store.locked_registry(write=False) as registry:
        assert registry.get("_invalid_record_ids"), "unsealed edit must be re-validated"


# (c) ──────────────────────────────────────────────────────────────────────────

def test_compaction_shrinks_registry_and_preserves_pending_and_integrity():
    now = time.time()
    path, _ = _seed(now)
    original_size = path.stat().st_size
    pending_before = _pending_events(json.loads(path.read_text()))
    assert len(pending_before) == 2
    with store.locked_registry() as registry:
        store._prune(registry, now, path)
    compacted_size = path.stat().st_size
    assert compacted_size <= 0.30 * original_size, (compacted_size, original_size)
    backups = list(path.parent.glob(f"{path.name}.pre-compact-*"))
    assert len(backups) == 1 and backups[0].stat().st_size == original_size

    # Simulated restart: drop every in-memory trace, strict load validates all integrity seals (I4).
    _clear_cache()
    reloaded = store.read_registry()
    assert len(reloaded["records"]) == N_RECORDS
    assert "_invalid_record_ids" not in reloaded
    assert _pending_events(reloaded) == pending_before  # I5: byte-identical

    for record in reloaded["records"].values():
        for event in record["outbox"]:
            if event["state"] == "delivered":
                assert _compacted(event["payload"])
                assert "results" not in event["payload"]
                assert event["payload"]["event_id"] == event["event_id"]
        if record["state"] in {"done", "failed"}:
            store.canonical_terminal(reloaded, record["delegation_id"])  # identity still validates
            children = record["terminal"]["result"]["results"]
            assert children and all(child["summary"] for child in children)  # result text kept (Q2)

    # A second compaction run is a no-op and never writes a second backup.
    with store.locked_registry() as registry:
        assert store._compact(registry, now, path) == 0
    assert len(list(path.parent.glob(f"{path.name}.pre-compact-*"))) == 1


def test_compaction_leaves_recent_terminal_bodies_alone():
    now = time.time()
    path, _ = _seed(now)
    with store.locked_registry() as registry:
        store._prune(registry, now, path)
    record = store.read_registry()["records"]["deleg_00000005"]  # 1 h old, delivered
    assert "tool_trace" in record["terminal"]["result"]["results"][0]
    assert _compacted(record["outbox"][0]["payload"])


# (d) ──────────────────────────────────────────────────────────────────────────

def test_flipped_byte_on_disk_is_quarantined_after_cache_hit():
    path, _ = _seed(time.time())
    with store.locked_registry(write=False) as registry:
        assert "_invalid_record_ids" not in registry
    raw = path.read_bytes()
    at = raw.index(b"synthetic goal 7 ") + len(b"synthetic goal 7 ")
    flipped = raw[:at] + (b"Z" if raw[at:at + 1] != b"Z" else b"Y") + raw[at + 1:]
    path.write_bytes(flipped)
    _bump_mtime(path)
    with store.locked_registry(write=False) as registry:
        assert registry["_invalid_record_ids"] == ["deleg_00000007"]
    with pytest.raises(store.RegistryError, match="deleg_00000007 failed integrity"):
        store.read_registry()
    # The quarantined record is carried through a write untouched, still quarantined.
    with store.locked_registry() as registry:
        pass
    _clear_cache()
    with store.locked_registry(write=False) as registry:
        assert registry["_invalid_record_ids"] == ["deleg_00000007"]


# (e) ──────────────────────────────────────────────────────────────────────────

def test_completion_delivered_then_compacted_still_renders_result(monkeypatch):
    identifier, worker, result = helpers.dispatch(monkeypatch)
    worker()
    event = helpers.process_registry.completion_queue.get_nowait()
    claim = ad.claim_event_delivery(event, "renderer")
    assert claim
    ad.complete_event_delivery(event, claim)

    canonical = helpers.registry_record(identifier)
    stored = canonical["outbox"][0]
    assert stored["state"] == "delivered"
    assert _compacted(stored["payload"]), "ack must compact the delivered body"
    assert "summary" not in stored["payload"]
    assert canonical["terminal"]["result"]["summary"] == "receipt-nonce"

    # Restart path: recovery re-syncs the SQLite mirror from the (now compacted) canonical record.
    _clear_cache()
    ad.recover_abandoned_delegations()
    assert ad.claim_completion_delivery(identifier, "late-claim", event=event) is False  # settled
    mirror = helpers.row(identifier)
    assert mirror["delivery_state"] == "delivered"
    mirror_event = json.loads(mirror["event_json"])
    assert mirror_event["summary"] == "receipt-nonce", "the mirror keeps the full envelope"
    assert "receipt-nonce" in format_process_notification(mirror_event)
    assert ad.get_durable_delegation(identifier)["result"] == result


# Prism 9d8a4707d7f0 / 2b1de7b230df / 2f3977f2d1cf ─────────────────────────────

def test_in_place_rewrite_with_restored_mtime_is_not_served_stale():
    """Past the hash window, a same-size in-place rewrite that restores the old mtime (os.utime) keeps
    (mtime, size, inode) equal; the kernel-owned ctime still moves, so the cache must miss."""
    path, _ = _seed(time.time())
    old = time.time() - 60
    os.utime(path, (old, old))
    _clear_cache()
    with store.locked_registry(write=False) as registry:  # cached well past the hash window
        assert "_invalid_record_ids" not in registry
    with store.locked_registry(write=False):
        pass  # a confirmed hit: from here on hits skip the content hash
    st = path.stat()
    raw = path.read_bytes()
    needle = b'"last_error": null'
    at = raw.index(needle)
    forged = raw[:at] + b'"last_error": 1234' + raw[at + len(needle):]
    assert len(forged) == len(raw)
    with open(path, "r+b") as fh:  # in place: same inode, same size
        fh.write(forged)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # the old mtime, past the hash window
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == (st.st_mtime_ns, st.st_ino)
    with store.locked_registry(write=False) as registry:
        assert registry.get("_invalid_record_ids"), "the rewritten bytes must be re-read and re-validated"


def test_failed_backup_leaves_no_artifact_and_blocks_compaction(monkeypatch):
    now = time.time()
    path, _ = _seed(now)
    real_fsync = os.fsync

    def disk_full(fd):  # ENOSPC on the backup copy only; the registry's own writes still succeed
        if sys._getframe(1).f_code.co_name == "_backup_before_first_compaction":
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", disk_full)
    with store.locked_registry() as registry:
        store._prune(registry, now, path)
    assert not list(path.parent.glob(f"{path.name}.pre-compact-*")), "no partial backup may be published"
    assert not [p for p in path.parent.iterdir() if "pre-compact" in p.name or "precompact" in p.name]
    on_disk = store.read_registry()
    assert not any(_compacted(e["payload"]) for r in on_disk["records"].values() for e in r["outbox"]), \
        "without a backup nothing may be compacted"

    monkeypatch.setattr(store.os, "fsync", real_fsync)
    original_size = path.stat().st_size
    with store.locked_registry() as registry:
        store._prune(registry, now, path)
    backups = list(path.parent.glob(f"{path.name}.pre-compact-*"))
    assert len(backups) == 1 and backups[0].stat().st_size == original_size
    assert oct(backups[0].stat().st_mode & 0o777) == "0o600"


def test_compacted_resync_onto_empty_mirror_row_never_reopens_delivery(monkeypatch):
    """A compacted canonical envelope resynced onto a mirror row whose event_json is gone must leave
    the row settled (``delivered``), never ``pending`` with a body-less replay source."""
    identifier, worker, result = helpers.dispatch(monkeypatch)
    worker()
    event = helpers.process_registry.completion_queue.get_nowait()
    claim = ad.claim_event_delivery(event, "renderer")
    ad.complete_event_delivery(event, claim)
    assert _compacted(helpers.registry_record(identifier)["outbox"][0]["payload"])
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET event_json=NULL WHERE delegation_id=?", (identifier,))
    _clear_cache()
    ad.recover_abandoned_delegations()
    assert ad.claim_completion_delivery(identifier, "late", event=event) is False
    assert helpers.row(identifier)["delivery_state"] == "delivered"
    q = queue.Queue()
    ad.restore_undelivered_completions(q)
    assert q.empty(), "a delivered completion is never replayed"
    assert ad.get_durable_delegation(identifier)["result"] == result
