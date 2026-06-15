from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import plugins.native_content_slimmer.gc as gc_module
from plugins.native_content_slimmer.gc import ArtifactCandidate, _delete_candidate, collect_garbage, gc_status_path
from plugins.native_content_slimmer.health import check_artifact_store_health
from plugins.native_content_slimmer.store import ArtifactStore


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_gc_removes_expired_artifacts_and_leaves_gone_tombstone(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    old = store.write_artifact(
        session_id="ended",
        tool_call_id="old",
        raw_text="old payload",
        created_at=_iso(now - timedelta(days=3)),
    )
    fresh = store.write_artifact(
        session_id="ended",
        tool_call_id="fresh",
        raw_text="fresh payload",
        created_at=_iso(now),
    )

    result = collect_garbage(store.root, ttl_seconds=24 * 60 * 60, now=now)

    assert result["deleted_count"] == 1
    assert result["deleted"][0]["artifact_id"] == old["artifact_id"]
    assert result["deleted"][0]["reason"] == "ttl_expired"
    assert not store.path_for(old["artifact_id"], session_id="ended").exists()
    assert store.path_for(fresh["artifact_id"], session_id="ended").exists()

    tombstone = store.tombstone_path_for(old["artifact_id"], session_id="ended")
    assert tombstone.exists()
    tombstone_record = json.loads(tombstone.read_text(encoding="utf-8"))
    assert tombstone_record["artifact_id"] == old["artifact_id"]
    assert tombstone_record["reason"] == "ttl_expired"

    gone = store.expand_artifact(old["artifact_id"], session_id="ended")
    assert gone["ok"] is False
    assert gone["error"] == "gone"
    assert gone["reason"] == "ttl_expired"


def test_gc_ttl_respects_recent_last_expanded_at_not_created_at(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    recent_use = store.write_artifact(
        session_id="ended",
        tool_call_id="recent-use",
        raw_text="recently used payload",
        created_at=_iso(now - timedelta(days=30)),
        last_expanded_at=_iso(now - timedelta(milliseconds=1)),
    )

    result = collect_garbage(store.root, ttl_seconds=0.01, now=now)

    assert result["deleted_count"] == 0
    assert store.path_for(recent_use["artifact_id"], session_id="ended").exists()


def test_gc_size_cap_preserves_recently_expanded_inactive_artifacts(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    stale = store.write_artifact(
        session_id="ended-a",
        tool_call_id="stale",
        raw_text="a" * 600,
        created_at=_iso(now - timedelta(days=2)),
        last_expanded_at=_iso(now - timedelta(days=2)),
    )
    recent = store.write_artifact(
        session_id="ended-b",
        tool_call_id="recent",
        raw_text="b" * 600,
        created_at=_iso(now - timedelta(days=2)),
        last_expanded_at=_iso(now - timedelta(seconds=1)),
    )

    result = collect_garbage(store.root, max_bytes=1, now=now)

    assert not store.path_for(stale["artifact_id"], session_id="ended-a").exists()
    assert store.path_for(recent["artifact_id"], session_id="ended-b").exists()
    assert stale["artifact_id"] in {item["artifact_id"] for item in result["deleted"]}
    assert recent["artifact_id"] not in {item["artifact_id"] for item in result["deleted"]}
    assert result["over_cap"] is True


def test_delete_candidate_writes_tombstone_before_unlink_so_crash_reports_gone(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    record = store.write_artifact(
        session_id="ended",
        tool_call_id="crash",
        raw_text="payload that is about to be evicted",
        created_at=_iso(now - timedelta(days=2)),
    )
    path = store.path_for(record["artifact_id"], session_id="ended")
    candidate = ArtifactCandidate(
        artifact_id=record["artifact_id"],
        session_id="ended",
        path=path,
        size=path.stat().st_size,
        created_at=now - timedelta(days=2),
        last_expanded_at=now - timedelta(days=2),
    )
    original_unlink = type(path).unlink

    def crash_after_artifact_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == path:
            original_unlink(self, *args, **kwargs)
            raise RuntimeError("simulated crash after unlink")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "unlink", crash_after_artifact_unlink)

    with pytest.raises(RuntimeError, match="simulated crash after unlink"):
        _delete_candidate(store, candidate, reason="ttl_expired", now=now)

    gone = store.expand_artifact(record["artifact_id"], session_id="ended")
    assert gone["ok"] is False
    assert gone["error"] == "gone"
    assert gone["reason"] == "ttl_expired"


def test_gc_size_cap_eviction_never_removes_active_session_artifacts(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    inactive_old = store.write_artifact(
        session_id="ended-a",
        tool_call_id="old",
        raw_text="a" * 600,
        created_at=_iso(now - timedelta(hours=3)),
    )
    inactive_new = store.write_artifact(
        session_id="ended-b",
        tool_call_id="new",
        raw_text="b" * 600,
        created_at=_iso(now - timedelta(hours=2)),
    )
    active = store.write_artifact(
        session_id="active",
        tool_call_id="keep",
        raw_text="c" * 600,
        created_at=_iso(now - timedelta(minutes=30)),
    )
    active_size = store.artifact_file_size(active["artifact_id"], session_id="active")

    result = collect_garbage(
        store.root,
        max_bytes=active_size,
        active_session_id="active",
        now=now,
    )

    assert store.path_for(active["artifact_id"], session_id="active").exists()
    assert not store.path_for(inactive_old["artifact_id"], session_id="ended-a").exists()
    assert not store.path_for(inactive_new["artifact_id"], session_id="ended-b").exists()
    assert {item["reason"] for item in result["deleted"]} == {"size_cap"}
    assert result["active_session_bytes"] == active_size
    assert result["bytes_after"] == active_size
    assert result["over_cap"] is False


def test_gc_ttl_eviction_never_removes_active_session_artifacts(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    inactive = store.write_artifact(
        session_id="ended",
        tool_call_id="old",
        raw_text="inactive old payload",
        created_at=_iso(now - timedelta(days=30)),
        last_expanded_at=_iso(now - timedelta(days=30)),
    )
    active = store.write_artifact(
        session_id="active",
        tool_call_id="keep",
        raw_text="active old payload",
        created_at=_iso(now - timedelta(days=30)),
        last_expanded_at=_iso(now - timedelta(days=30)),
    )
    active_size = store.artifact_file_size(active["artifact_id"], session_id="active")

    result = collect_garbage(
        store.root,
        ttl_seconds=24 * 60 * 60,
        active_session_id="active",
        now=now,
    )

    assert store.path_for(active["artifact_id"], session_id="active").exists()
    assert not store.path_for(inactive["artifact_id"], session_id="ended").exists()
    assert store.expand_artifact(active["artifact_id"], session_id="active")["ok"] is True
    assert active["artifact_id"] not in {item["artifact_id"] for item in result["deleted"]}
    assert inactive["artifact_id"] in {item["artifact_id"] for item in result["deleted"]}
    assert result["active_session_bytes"] == active_size


def test_gc_candidate_scan_does_not_re_read_unchanged_artifacts(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.write_artifact(session_id="ended-a", tool_call_id="a", raw_text="a" * 600)
    second = store.write_artifact(session_id="ended-b", tool_call_id="b", raw_text="b" * 600)
    read_counts: dict[str, int] = {}

    class CountingStore(ArtifactStore):
        def read_record(self, artifact_id: str, *, session_id: str | None = None):  # type: ignore[no-untyped-def]
            read_counts[artifact_id] = read_counts.get(artifact_id, 0) + 1
            return super().read_record(artifact_id, session_id=session_id)

    monkeypatch.setattr(gc_module, "ArtifactStore", CountingStore)

    result = gc_module.collect_garbage(store.root, max_bytes=10_000_000)

    assert result["deleted_count"] == 0
    assert read_counts == {first["artifact_id"]: 1, second["artifact_id"]: 1}


def test_gc_size_cap_protects_active_session_after_path_normalization_collision(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    active = store.write_artifact(
        session_id="sess/1",
        tool_call_id="keep",
        raw_text="a" * 600,
        created_at=_iso(now - timedelta(hours=3)),
    )
    inactive = store.write_artifact(
        session_id="ended",
        tool_call_id="evict",
        raw_text="b" * 600,
        created_at=_iso(now - timedelta(hours=2)),
    )
    active_size = store.artifact_file_size(active["artifact_id"], session_id="sess/1")

    result = collect_garbage(
        store.root,
        max_bytes=active_size,
        active_session_id="sess_1",
        now=now,
    )

    assert store.path_for(active["artifact_id"], session_id="sess/1").exists()
    assert not store.path_for(inactive["artifact_id"], session_id="ended").exists()
    assert active["artifact_id"] not in {item["artifact_id"] for item in result["deleted"]}
    assert inactive["artifact_id"] in {item["artifact_id"] for item in result["deleted"]}


def test_gc_reports_over_cap_when_active_session_alone_exceeds_cap(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    active = store.write_artifact(session_id="active", tool_call_id="keep", raw_text="x" * 1000)

    result = collect_garbage(store.root, max_bytes=1, active_session_id="active")

    assert result["deleted_count"] == 0
    assert result["over_cap"] is True
    assert result["active_session_bytes"] > 1
    assert store.path_for(active["artifact_id"], session_id="active").exists()


def test_health_reports_writability_usage_last_gc_and_session_split(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    active = store.write_artifact(session_id="active", tool_call_id="a", raw_text="active")
    ended = store.write_artifact(session_id="ended", tool_call_id="e", raw_text="ended")
    active_size = store.artifact_file_size(active["artifact_id"], session_id="active")
    ended_size = store.artifact_file_size(ended["artifact_id"], session_id="ended")

    gc_result = collect_garbage(store.root, max_bytes=active_size + ended_size + 1024, active_session_id="active")
    health = check_artifact_store_health(
        store.root,
        max_bytes=active_size + ended_size + 1024,
        active_session_id="active",
    )

    assert health["ok"] is True
    assert health["status"] == "ok"
    assert health["writable"] is True
    assert health["profile_bytes"] == active_size + ended_size
    assert health["active_session_bytes"] == active_size
    assert health["ended_session_bytes"] == ended_size
    assert 0 < health["cap_usage_ratio"] < 1
    assert health["last_gc_time"] == gc_result["completed_at"]
    assert health["last_gc_error"] is None
    assert gc_status_path(store.root).exists()


def test_health_reports_unwritable_store_path(tmp_path):
    broken_root = tmp_path / "not-a-directory"
    broken_root.write_text("file blocks directory creation", encoding="utf-8")

    health = check_artifact_store_health(broken_root, max_bytes=100)

    assert health["ok"] is False
    assert health["status"] == "error"
    assert health["writable"] is False
    assert "error" in health
