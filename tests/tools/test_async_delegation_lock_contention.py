"""async-delegations.lock contention must not lose a dispatched child (t_86324a22).

2026-11-09 02:26 (Studio default gateway): a delegate_task background dispatch died in 9 s with
``submission telemetry failed: TimeoutError: timed out acquiring .../async-delegations.lock``
after the parent had already been told the child was running. The registry was 34.5 MB: settled
``done`` records of up to 6.16 MB (full terminal results + the inherited parent transcript in
``source.tasks[].materialized_prefill_messages``) that compaction never shrank, and every lock
holder loads -- and every writer rewrites + fsyncs -- the whole file.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import threading
import time
from pathlib import Path

import pytest

from tools import async_delegation as ad
from tools import async_delegation_store as store

from tests.tools.test_async_delegation_persistence import (  # noqa: F401
    _isolated_registry,
    _load,
    _spec,
)

BIG_BYTES = 6_000_000


def _big_done_record(delegation_id: str, size: int = BIG_BYTES) -> dict:
    """A settled, delivered ``done`` record shaped like the live 6.16 MB one."""
    now = time.time()
    blob = "x" * (size // 3)
    return {
        "delegation_id": delegation_id, "state": "done", "created_at": now - 120, "updated_at": now - 60,
        "profile": "default",
        "source": {"kind": "single", "shared_context": None, "tasks": [{
            "goal": "summarize", "role": "leaf", "context": "ctx",
            "materialized_prefill_messages": [{"role": "user", "content": blob}],
        }]},
        "execution": {"model": "m"}, "route": {"session_key": "k"},
        "attempt": {"attempt_id": f"{delegation_id}:g0:abcd1234", "generation": 0, "redispatch_count": 0,
                    "owner_boot_id": "100:1.0", "started_at": now - 120, "submitted_at": now - 119,
                    "last_interrupted_at": None, "last_error": None},
        "terminal": {"status": "completed", "completed_at": now - 60, "error": None,
                     "result": {"summary": blob, "results": [{"task_index": 0, "status": "completed",
                                                             "summary": blob, "tool_trace": []}]}},
        "outbox": [{"event_id": f"{delegation_id}:terminal:g0", "type": "async_delegation",
                    "state": "delivered", "queued_boot_id": "100:1.0", "created_at": now - 60,
                    "delivered_at": now - 59, "drop_reason": None,
                    "payload": {"type": "async_delegation", "delegation_id": delegation_id,
                                "event_id": f"{delegation_id}:terminal:g0",
                                "attempt_id": f"{delegation_id}:g0:abcd1234", "attempt_generation": 0,
                                "profile": "default", "status": "completed", "completed_at": now - 60,
                                "summary": "short"}}],
    }


def _seed(records: dict) -> None:
    ad._write_registry_for_tests({"schema_version": 1, "updated_at": time.time(), "records": records})
    store._REGISTRY_CACHE.clear()


def _hold_lock(seconds: float) -> threading.Thread:
    """Another holder (a sibling gateway) keeps the registry lock for ``seconds``."""
    lpath = store.registry_path().with_suffix(".lock")
    started = threading.Event()

    def run():
        with lpath.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            started.set()
            time.sleep(seconds)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    return thread


def test_settled_oversized_record_is_offloaded_to_a_sidecar_with_backup():
    _seed({"big": _big_done_record("big")})
    assert store.registry_path().stat().st_size > BIG_BYTES * 0.9

    record, why = store.persist_dispatch(
        delegation_id="new", durable_spec=_spec(), boot_id="100:1.0", dispatched_at=time.time())
    assert why is None and record is not None

    path = store.registry_path()
    after = path.stat().st_size
    assert after < 64 * 1024, f"registry still {after} bytes after a dispatch"
    assert list(path.parent.glob(f"{path.name}.pre-compact-*")), "no backup before the migration"

    stub = _load()["records"]["big"]
    full = store.read_offloaded_record(path, stub)
    assert full is not None, "sidecar missing or failed its hash"
    assert full["terminal"]["result"]["summary"] == "x" * (BIG_BYTES // 3)
    assert full["source"]["tasks"][0]["materialized_prefill_messages"]
    # The stub keeps identity, so canonical consumers still accept it. With the registry path the
    # full result comes back from the sidecar; without it, None (the SQLite mirror keeps its own
    # full result rather than being overwritten with the preview).
    registry = store.read_registry()
    assert store.canonical_terminal(registry, "big")["execution_result"] is None
    full_result = store.canonical_terminal(registry, "big", path=path)["execution_result"]
    assert full_result["summary"] == "x" * (BIG_BYTES // 3)
    assert stub["terminal"]["result"]["results"][0]["status"] == "completed"
    assert "materialized_prefill_messages" not in stub["source"]["tasks"][0]


def test_offload_is_idempotent_and_skips_pending_records():
    pending = _big_done_record("pending")
    pending["outbox"][0]["state"] = "pending"
    _seed({"big": _big_done_record("big"), "pending": pending})
    for i in range(2):
        store.persist_dispatch(delegation_id=f"n{i}", durable_spec=_spec(), boot_id="100:1.0",
                               dispatched_at=time.time())
    path = store.registry_path()
    assert sorted(p.name for p in store.sidecar_dir(path).iterdir()) == ["big.json"]
    records = _load()["records"]
    assert "offloaded" not in records["pending"], "a pending delivery must keep its full body"
    marker = records["big"]["offloaded"]
    assert marker["sha256"] == hashlib.sha256((path.parent / marker["sidecar"]).read_bytes()).hexdigest()


def test_pruned_offloaded_record_removes_its_sidecar():
    _seed({"big": _big_done_record("big")})
    store.persist_dispatch(delegation_id="n0", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    path = store.registry_path()
    assert (store.sidecar_dir(path) / "big.json").exists()
    store.persist_dispatch(delegation_id="n1", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time(), max_records=1)
    assert "big" not in _load()["records"]
    assert not (store.sidecar_dir(path) / "big.json").exists()


def test_failed_registry_write_keeps_the_pruned_records_sidecar(monkeypatch):
    """Prism de9f5c835503: prune unlinked the sidecar before the registry write committed. A write
    that raises leaves the on-disk registry pointing at a sidecar that no longer exists."""
    _seed({"big": _big_done_record("big")})
    store.persist_dispatch(delegation_id="n0", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    path = store.registry_path()
    sidecar = store.sidecar_dir(path) / "big.json"
    assert sidecar.exists()
    # Age the stub past retention so the next prune drops it.
    with store.locked_registry() as registry:
        registry["records"]["big"]["updated_at"] = time.time() - store.TERMINAL_RETENTION_SECONDS - 60

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    real_write = store._write
    monkeypatch.setattr(store, "_write", boom)
    _, why = store.persist_dispatch(delegation_id="n1", durable_spec=_spec(), boot_id="100:1.0",
                                    dispatched_at=time.time())
    assert why == "registry_error"
    monkeypatch.setattr(store, "_write", real_write)
    store._REGISTRY_CACHE.clear()
    assert sidecar.exists(), "sidecar deleted although the registry write failed"
    registry = store.read_registry()
    assert "big" in registry["records"]
    full = store.canonical_terminal(registry, "big", path=path)["execution_result"]
    assert full["summary"] == "x" * (BIG_BYTES // 3)


def test_pruned_sidecar_is_removed_once_the_write_commits():
    _seed({"big": _big_done_record("big")})
    store.persist_dispatch(delegation_id="n0", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    path = store.registry_path()
    with store.locked_registry() as registry:
        registry["records"]["big"]["updated_at"] = time.time() - store.TERMINAL_RETENTION_SECONDS - 60
    store.persist_dispatch(delegation_id="n1", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    assert "big" not in _load()["records"]
    assert not (store.sidecar_dir(path) / "big.json").exists()


def test_same_id_replacement_keeps_its_sidecar_after_the_queued_unlink():
    """Prism c41dd8d3a75b: one transaction prunes an expired offloaded ``big`` and offloads a new
    ``big`` to the same ``big.json``; the queued unlink must not delete the committed sidecar."""
    _seed({"big": _big_done_record("big")})
    store.persist_dispatch(delegation_id="n0", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    path = store.registry_path()
    with store.locked_registry() as registry:
        registry["records"]["big"]["updated_at"] = time.time() - store.TERMINAL_RETENTION_SECONDS - 60
    replacement = _big_done_record("big", size=300_000)
    text = "y" * 120_000
    replacement["terminal"]["result"]["summary"] = text
    with store.locked_registry(path=path) as registry:
        store._prune(registry, time.time(), path)
        assert "big" not in registry["records"]
        registry["records"]["big"] = replacement
        assert store._offload_oversized(registry, path) == 1
    store._REGISTRY_CACHE.clear()
    stub = _load()["records"]["big"]
    assert (path.parent / stub["offloaded"]["sidecar"]).exists(), "replacement sidecar deleted"
    full = store.canonical_terminal(store.read_registry(), "big", path=path)["execution_result"]
    assert full is not None and full["summary"] == text


def test_offloaded_lone_surrogate_result_reads_back_in_full():
    """Prism: the sidecar was encoded with surrogatepass (not UTF-8) and read back with a strict
    decode, so a lone surrogate made the full body unreadable after the record was stubbed. The
    registry write itself refuses a lone surrogate (strict checksum), so this drives the offload
    pass directly on the in-memory registry it receives."""
    _seed({})
    path = store.registry_path()
    record = _big_done_record("big", size=3000)
    text = "\ud800" + "x" * 40000
    record["terminal"]["result"]["summary"] = text
    registry = {"schema_version": 1, "updated_at": time.time(), "records": {"big": record}}
    assert store._offload_oversized(registry, path) == 1
    stub = registry["records"]["big"]
    raw = (path.parent / stub["offloaded"]["sidecar"]).read_bytes()
    assert raw.isascii()
    full = store.read_offloaded_record(path, stub)
    assert full is not None, "sidecar unreadable"
    assert full["terminal"]["result"]["summary"] == text


def test_sidecar_dir_creation_not_durable_offloads_nothing(monkeypatch):
    """Prism: the first mkdir of <registry>.sidecars/ must be durable in the registry's parent
    before any stub replaces a full record."""
    _seed({"big": _big_done_record("big")})
    path = store.registry_path()
    assert store._backup_before_first_compaction(path)  # the backup is not under test here
    real = store._fsync_directory

    def parent_fsync_fails_once_sidecars_exist(directory):
        if Path(directory) == path.parent and store.sidecar_dir(path).exists():
            return False
        return real(directory)

    monkeypatch.setattr(store, "_fsync_directory", parent_fsync_fails_once_sidecars_exist)
    store.persist_dispatch(delegation_id="n0", durable_spec=_spec(), boot_id="100:1.0",
                           dispatched_at=time.time())
    monkeypatch.setattr(store, "_fsync_directory", real)
    stub = _load()["records"]["big"]
    assert "offloaded" not in stub, "record stubbed although the sidecar dir is not durable"
    assert stub["terminal"]["result"]["summary"] == "x" * (BIG_BYTES // 3)


def test_dispatch_survives_a_sibling_holding_the_lock_past_one_timeout(monkeypatch):
    """Red before the fix: mark_submitted gave up after one 5 s wait and the child never ran,
    though the parent had already been told it was running."""
    ran = threading.Event()
    done = threading.Event()
    holders: list[threading.Thread] = []
    hold_s = getattr(store, "LOCK_TIMEOUT_SECONDS", 5.0) + 1.5

    def runner():
        ran.set()
        return {"status": "completed", "summary": "done"}

    class SiblingHoldsLockExecutor:
        """A sibling gateway takes the lock right before this worker's submission write."""

        def submit(self, fn):
            holders.append(_hold_lock(hold_s))
            threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
            return None

    monkeypatch.setattr(ad, "_get_executor", lambda workers: SiblingHoldsLockExecutor())
    result = ad.dispatch_async_delegation_batch(
        goals=["continue the report"], context=None, toolsets=None, role="leaf",
        model="test-model", session_key="agent:main:telegram:dm:123", parent_session_id="parent-1",
        runner=runner, durable_spec=_spec(), current_boot_id="100:1.0",
    )
    assert result["status"] == "dispatched", result
    assert done.wait(40), "worker never finished"
    for holder in holders:
        holder.join(10)
    record = _load()["records"][result["delegation_id"]]
    assert ran.is_set(), f"child never ran: {record.get('terminal')}"
    assert record["attempt"]["submitted_at"] is not None
    assert record["state"] == "done"


def test_mark_submitted_gives_up_after_its_budget(monkeypatch):
    rec, _ = store.persist_dispatch(delegation_id="d", durable_spec=_spec(), boot_id="100:1.0",
                                    dispatched_at=time.time())
    monkeypatch.setattr(store, "LOCK_TIMEOUT_SECONDS", 0.2)
    holder = _hold_lock(2.0)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        store.mark_submitted("d", rec["attempt"]["attempt_id"], budget_seconds=0.7)
    elapsed = time.monotonic() - t0
    holder.join(5)
    assert 0.6 <= elapsed < 1.9, elapsed
    assert store.mark_submitted("d", rec["attempt"]["attempt_id"]) is True


def test_lock_acquisition_logs_wait_hold_and_registry_bytes(caplog, monkeypatch):
    monkeypatch.setattr(store, "LOCK_SLOW_LOG_MS", 0.0)
    _seed({})
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        with store.locked_registry():
            pass
    lines = [r.getMessage() for r in caplog.records if "async_delegation_registry_lock" in r.getMessage()]
    assert lines, "no lock timing line"
    assert all(k in lines[-1] for k in ("wait_ms=", "hold_ms=", "registry_bytes=", "acquired=True"))
