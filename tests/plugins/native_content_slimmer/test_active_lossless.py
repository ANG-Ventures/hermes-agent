from __future__ import annotations

import json

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import MARKER_TOKEN, parse_marker, verify_marker_auth
from plugins.native_content_slimmer.store import ArtifactStore, sha256_text


def _large_payload(label: str = "active") -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 900) + f"{label}-TAIL\n"


def _active_hooks(tmp_path, **kwargs) -> NativeContentSlimmerHooks:
    return NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=kwargs.pop("store", ArtifactStore(tmp_path / "artifacts")),
        secret=kwargs.pop("secret", b"active-lossless-test-secret"),
        **kwargs,
    )


def test_active_lossless_tool_result_returns_marker_after_verified_artifact(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = _active_hooks(tmp_path, store=store)
    raw = _large_payload()

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-active",
        tool_call_id="call-active",
        task_id="task-active",
        turn_id="turn-active",
        api_request_id="api-active",
        duration_ms=7.5,
    )

    assert replacement is not None
    assert replacement.startswith(f"[{MARKER_TOKEN} ")
    assert "lossy=false" in replacement
    assert 'expand_tool="expand_artifact"' in replacement
    assert "call expand_artifact" in replacement
    assert raw not in replacement

    parsed = parse_marker(replacement)
    assert parsed is not None
    artifact_id = parsed.fields["id"]
    record = store.read_record(artifact_id, session_id="sess-active")
    assert record["raw_text"] == raw
    assert record["raw_sha256"] == sha256_text(raw)
    assert record["raw_source"] == "tool-result-returned"
    assert record["lossy"] is False
    assert record["metadata"]["mode"] == "active_lossless"
    assert record["metadata"]["would_replace"] is False

    expanded = store.expand_artifact(artifact_id, session_id="sess-active")
    assert expanded["ok"] is True
    assert expanded["content"] == raw
    assert expanded["raw_sha256"] == sha256_text(raw)

    verification = verify_marker_auth(replacement, secret=b"active-lossless-test-secret", ledger=hooks.ledger)
    assert verification.ok is True


def test_active_lossless_requires_enabled_config(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=False, mode="active_lossless"),
        store=store,
        secret=b"disabled-active-test-secret",
    )

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("disabled"),
        status="success",
        session_id="sess-disabled",
        tool_call_id="call-disabled",
    )

    assert replacement is None
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []


class CorruptAfterWriteStore(ArtifactStore):
    def write_artifact(self, **kwargs):  # type: ignore[no-untyped-def]
        record = super().write_artifact(**kwargs)
        path = self.path_for(record["artifact_id"], session_id=record["session_id"])
        corrupted = dict(record)
        corrupted["raw_text"] = "corrupted after write-back verification"
        path.write_text(json.dumps(corrupted), encoding="utf-8")
        return record


def test_active_lossless_emits_no_marker_when_readback_hash_verification_fails(tmp_path) -> None:
    hooks = _active_hooks(tmp_path, store=CorruptAfterWriteStore(tmp_path / "artifacts"))

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("corrupt"),
        status="success",
        session_id="sess-corrupt",
        tool_call_id="call-corrupt",
    )

    assert replacement is None
    assert hooks.failures
    assert "hash mismatch" in hooks.failures[-1]
    assert hooks.telemetry_records == []


def test_active_lossless_reuses_verified_marker_without_duplicate_artifact_or_telemetry(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = _active_hooks(tmp_path, store=store)
    raw = _large_payload("reuse")

    first = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-reuse",
        tool_call_id="call-reuse",
    )
    second = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-reuse",
        tool_call_id="call-reuse",
    )

    assert first is not None
    assert second == first
    assert len(hooks.telemetry_records) == 1
    assert len(list((tmp_path / "artifacts").glob("**/*.json"))) == 1
