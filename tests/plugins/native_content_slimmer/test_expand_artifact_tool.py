from __future__ import annotations

import json
from typing import Any

from plugins.native_content_slimmer.store import ArtifactStore
from plugins.native_content_slimmer.tools import handle_expand_artifact, register_tools


def _call(args: dict[str, Any], *, session_id: str | None, store: ArtifactStore) -> dict[str, Any]:
    return json.loads(handle_expand_artifact(args, session_id=session_id, store=store))


def test_register_tools_exposes_expand_artifact_tool() -> None:
    calls: list[dict[str, Any]] = []

    class FakeContext:
        def register_tool(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    register_tools(FakeContext())

    assert len(calls) == 1
    registered = calls[0]
    assert registered["name"] == "expand_artifact"
    assert registered["toolset"] == "native_content_slimmer"
    assert registered["schema"]["name"] == "expand_artifact"
    assert callable(registered["handler"])


def test_expand_artifact_returns_exact_raw_for_same_session(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    raw = "first line\n" + ("middle αβγ\n" * 4) + "last line\n"
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text=raw)

    result = _call({"id": record["artifact_id"], "max_bytes": 200_000, "range": None}, session_id="sess-owner", store=store)

    assert result["ok"] is True
    assert result["id"] == record["artifact_id"]
    assert result["raw_sha256"] == record["raw_sha256"]
    assert result["truncated"] is False
    assert result["content"] == raw
    assert result["bytes_returned"] == len(raw.encode("utf-8"))


def test_expand_artifact_supports_byte_range_and_max_bytes(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text="abcdefghi")

    result = _call({"id": record["artifact_id"], "max_bytes": 3, "range": {"start": 2, "end": 8}}, session_id="sess-owner", store=store)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["range"] == {"start": 2, "end": 5}
    assert result["bytes_returned"] == 3
    assert result["content"] == "cde"


def test_expand_artifact_denies_cross_session_access(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text="owned raw")

    result = _call({"id": record["artifact_id"]}, session_id="sess-other", store=store)

    assert result == {"id": record["artifact_id"], "ok": False, "error": "not_authorized"}


def test_expand_artifact_denies_when_no_trusted_session_context_even_if_args_spoof_owner(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text="owned raw")

    result = _call({"id": record["artifact_id"], "session_id": "sess-owner"}, session_id=None, store=store)

    assert result == {"id": record["artifact_id"], "ok": False, "error": "not_authorized"}


def test_expand_artifact_reports_gone_tombstone(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact_id = "art_sess_owner_call_1_deadbeef"
    store.write_tombstone(
        artifact_id=artifact_id,
        session_id="sess-owner",
        reason="ttl_expired",
        deleted_at="2026-06-14T00:00:00Z",
    )

    result = _call({"id": artifact_id}, session_id="sess-owner", store=store)

    assert result == {
        "id": artifact_id,
        "ok": False,
        "error": "gone",
        "deleted_at": "2026-06-14T00:00:00Z",
        "reason": "ttl_expired",
    }


def test_expand_artifact_reports_hash_mismatch_without_returning_corrupt_raw(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text="original raw")
    path = store.path_for(record["artifact_id"], session_id="sess-owner")
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["raw_text"] = "corrupt raw"
    path.write_text(json.dumps(corrupted), encoding="utf-8")

    result = _call({"id": record["artifact_id"]}, session_id="sess-owner", store=store)

    assert result["ok"] is False
    assert result["error"] == "hash_mismatch"
    assert "content" not in result
    assert "corrupt raw" not in json.dumps(result)


def test_expand_artifact_blocks_sensitive_legacy_content_without_returning_raw(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    sensitive_raw = "prefix " + "op://" + "vault/item/field" + " suffix"
    record = store.write_artifact(session_id="sess-owner", tool_call_id="call-1", raw_text=sensitive_raw)

    result = _call({"id": record["artifact_id"]}, session_id="sess-owner", store=store)

    assert result == {"id": record["artifact_id"], "ok": False, "error": "sensitive_content_blocked"}
    assert sensitive_raw not in json.dumps(result)
