from __future__ import annotations

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import MARKER_TOKEN
from plugins.native_content_slimmer.store import ArtifactStore, sha256_text


def _large_payload() -> str:
    return "HEAD\n" + ("middle line with useful evidence\n" * 700) + "TAIL\n"


def test_shadow_tool_result_persists_artifact_and_returns_no_replacement(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=store,
        secret=b"shadow-test-secret",
    )
    raw = _large_payload()

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-1",
        tool_call_id="call-1",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="api-1",
        duration_ms=12.5,
    )

    assert replacement is None
    assert MARKER_TOKEN not in raw
    assert len(hooks.shadow_records) == 1
    shadow = hooks.shadow_records[0]
    assert shadow.mode == "shadow"
    assert shadow.action == "would_replace"
    assert shadow.tool_name == "web_extract"
    assert shadow.raw_source == "tool-result-returned"
    assert shadow.original_bytes == len(raw.encode("utf-8"))
    assert 0 < shadow.emitted_bytes < shadow.original_bytes
    assert shadow.would_save_bytes == shadow.original_bytes - shadow.emitted_bytes
    assert shadow.classification_reason == "eligible_lossless_offload"

    record = store.read_record(shadow.artifact_id, session_id="sess-1")
    assert record["raw_text"] == raw
    assert record["raw_sha256"] == sha256_text(raw)
    assert record["raw_source"] == "tool-result-returned"
    assert record["preview_strategy"] == "head-tail-lines"
    assert record["lossy"] is False
    assert record["metadata"]["mode"] == "shadow"
    assert record["metadata"]["would_replace"] is True


def test_shadow_mode_skips_no_store_secret_without_artifact(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=store,
    )
    raw = ("safe line\nop://Engineering/example/password\n" * 500)

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-secret",
        tool_call_id="call-secret",
    )

    assert replacement is None
    assert hooks.shadow_records == []
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []
