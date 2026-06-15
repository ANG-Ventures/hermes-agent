from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from plugins.native_content_slimmer.store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    default_artifact_root,
    sha256_text,
)


def test_default_artifact_root_is_profile_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))

    assert default_artifact_root() == tmp_path / "profile-home" / "compression" / "artifacts"


def test_write_artifact_uses_atomic_json_file_and_read_verifies_hash(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    raw = "line 1\nline 2\n" + ("tail\n" * 8)

    record = store.write_artifact(
        session_id="session/one",
        tool_call_id="tool:call:1",
        raw_text=raw,
        tool_name="terminal",
        tool_status="success",
        task_id="task-1",
        turn_id="turn-1",
        raw_source="pre-truncation-terminal",
        preview_strategy="head-tail-lines",
        preview_bytes=12,
        omitted_bytes=7,
        classification_reason="large terminal success over threshold",
    )

    assert record["schema_version"] == 1
    assert record["artifact_id"].startswith("art_session_one_tool_call_1_")
    assert record["raw_sha256"] == sha256_text(raw)
    assert record["raw_bytes"] == len(raw.encode("utf-8"))
    assert record["raw_text"] == raw
    assert record["lossy"] is False

    path = store.path_for(record["artifact_id"], session_id="session/one")
    assert path.exists()
    assert path.suffix == ".json"
    assert not list(path.parent.glob("*.tmp.*"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == record

    loaded = store.read_record(record["artifact_id"], session_id="session/one")
    assert loaded == record

    expanded = store.expand_artifact(record["artifact_id"], session_id="session/one")
    assert expanded == {
        "id": record["artifact_id"],
        "ok": True,
        "raw_sha256": sha256_text(raw),
        "bytes_returned": len(raw.encode("utf-8")),
        "truncated": False,
        "range": {"start": 0, "end": len(raw.encode("utf-8"))},
        "content": raw,
    }


def test_write_artifact_is_idempotent_for_same_session_call_and_hash(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    raw = "same payload"

    first = store.write_artifact(session_id="s", tool_call_id="c", raw_text=raw)
    first_path = store.path_for(first["artifact_id"], session_id="s")
    first_mtime = first_path.stat().st_mtime_ns

    second = store.write_artifact(session_id="s", tool_call_id="c", raw_text=raw)

    assert second == first
    assert store.path_for(second["artifact_id"], session_id="s") == first_path
    assert first_path.stat().st_mtime_ns == first_mtime


def test_write_artifact_avoids_id_collision_without_overwriting_existing_file(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    first = store.write_artifact(
        session_id="s",
        tool_call_id="c",
        raw_text="first payload",
        artifact_id="art_forced_collision",
    )
    second = store.write_artifact(
        session_id="s",
        tool_call_id="c",
        raw_text="second payload",
        artifact_id="art_forced_collision",
    )

    assert first["artifact_id"] == "art_forced_collision"
    assert second["artifact_id"] == "art_forced_collision_1"
    assert store.read_record(first["artifact_id"], session_id="s")["raw_text"] == "first payload"
    assert store.read_record(second["artifact_id"], session_id="s")["raw_text"] == "second payload"


def test_read_record_rejects_hash_mismatch(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="s", tool_call_id="c", raw_text="original")
    path = store.path_for(record["artifact_id"], session_id="s")
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["raw_text"] = "corrupted"
    path.write_text(json.dumps(corrupted), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        store.read_record(record["artifact_id"], session_id="s")

    expanded = store.expand_artifact(record["artifact_id"], session_id="s")
    assert expanded["ok"] is False
    assert expanded["error"] == "hash_mismatch"


def test_artifact_id_validation_blocks_path_traversal(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactNotFoundError, match="invalid artifact id"):
        store.path_for("../art_escape", session_id="s")

    assert store.expand_artifact("../art_escape", session_id="s") == {
        "id": "../art_escape",
        "ok": False,
        "error": "not_found",
    }


def test_expand_artifact_caps_returned_content_by_utf8_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="s", tool_call_id="c", raw_text="αβγδε")

    expanded = store.expand_artifact(record["artifact_id"], session_id="s", max_bytes=5)

    assert expanded["ok"] is True
    assert expanded["truncated"] is True
    assert expanded["bytes_returned"] <= 5
    assert expanded["content"].encode("utf-8") == "αβ".encode("utf-8")


def test_created_at_can_be_supplied_for_lifecycle_records(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    created = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")

    record = store.write_artifact(session_id="s", tool_call_id="c", raw_text="old", created_at=created)

    assert record["created_at"] == created
