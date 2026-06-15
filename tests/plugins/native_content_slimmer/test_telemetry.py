from __future__ import annotations

import pytest

from plugins.blackbox.native_slimmer_schema import (
    RAW_BYTES_SOURCE_NATIVE_EXACT,
    RAW_SOURCE_POST_RTK,
    RAW_SOURCE_PRE_TRUNCATION_TERMINAL,
    RAW_SOURCE_TOOL_CONTRACT_BOUNDED,
    RAW_SOURCE_TOOL_RESULT_RETURNED,
    TOKEN_ESTIMATE_KIND,
    TOKENIZER_LABEL_UTF8_BYTES_DIV_4,
    VALID_RAW_SOURCES,
    build_native_slimmer_event,
    estimate_tokens_from_bytes,
    rollup_native_slimmer_events,
)
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore, raw_byte_len, sha256_text


def _large_payload(label: str = "telemetry") -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 1000) + f"{label}-TAIL\n"


def test_active_replacement_emits_blackbox_telemetry_with_labeled_token_estimates(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=store,
        secret=b"telemetry-test-secret",
    )
    raw = _large_payload()

    replacement = hooks.transform_terminal_output(
        command="python emit.py",
        output=raw,
        returncode=0,
        task_id="task-terminal",
        session_id="sess-terminal",
        tool_call_id="call-terminal",
        turn_id="turn-terminal",
        api_request_id="api-terminal",
        duration_ms=11,
    )

    assert replacement is not None
    assert len(hooks.telemetry_records) == 1
    event = hooks.telemetry_records[0]
    assert event["compressor"] == "native-slimmer"
    assert event["mode"] == "active_lossless"
    assert event["action"] == "replace"
    assert event["tool_name"] == "terminal"
    assert event["raw_source"] == RAW_SOURCE_PRE_TRUNCATION_TERMINAL
    assert event["raw_bytes_source"] == RAW_BYTES_SOURCE_NATIVE_EXACT
    assert event["tokenizer_label"] == TOKENIZER_LABEL_UTF8_BYTES_DIV_4
    assert event["token_estimate_kind"] == TOKEN_ESTIMATE_KIND
    assert event["original_bytes"] == raw_byte_len(raw)
    assert event["emitted_bytes"] == raw_byte_len(replacement)
    assert event["saved_bytes"] == max(0, raw_byte_len(raw) - raw_byte_len(replacement))
    assert event["original_tokens_est"] == estimate_tokens_from_bytes(raw_byte_len(raw))
    assert event["emitted_tokens_est"] == estimate_tokens_from_bytes(raw_byte_len(replacement))
    assert event["saved_tokens_est"] == max(0, event["original_tokens_est"] - event["emitted_tokens_est"])
    assert event["raw_sha256"] == sha256_text(raw)
    assert event["lossy"] is False
    assert event["classification_reason"] == "eligible_lossless_offload"
    parsed = parse_marker(replacement)
    assert parsed is not None
    assert event["artifact_id"] == parsed.fields["id"]


def test_raw_source_enum_accepts_only_p0_values() -> None:
    for raw_source in VALID_RAW_SOURCES:
        event = build_native_slimmer_event(
            mode="active_lossless",
            action="replace",
            tool_name="web_extract",
            session_id="sess",
            tool_call_id=f"call-{raw_source}",
            artifact_id=f"art_sess_call_{raw_source.replace('-', '_')}",
            raw_sha256="a" * 64,
            raw_source=raw_source,
            original_bytes=100,
            emitted_bytes=40,
            classification_reason="eligible_lossless_offload",
        )
        assert event["raw_source"] == raw_source

    assert VALID_RAW_SOURCES == {
        RAW_SOURCE_PRE_TRUNCATION_TERMINAL,
        RAW_SOURCE_POST_RTK,
        RAW_SOURCE_TOOL_RESULT_RETURNED,
        RAW_SOURCE_TOOL_CONTRACT_BOUNDED,
    }
    with pytest.raises(ValueError, match="raw_source"):
        build_native_slimmer_event(
            mode="active_lossless",
            action="replace",
            tool_name="web_extract",
            session_id="sess",
            tool_call_id="call",
            artifact_id="art_sess_call_bad",
            raw_sha256="a" * 64,
            raw_source="pre-tool-raw",
            original_bytes=100,
            emitted_bytes=40,
            classification_reason="eligible_lossless_offload",
        )


def test_rtk_metadata_marks_post_rtk_without_guessing_from_output(tmp_path) -> None:
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"post-rtk-test-secret",
    )

    replacement = hooks.transform_terminal_output(
        command="rtk compact command",
        output=_large_payload("post-rtk"),
        returncode=0,
        task_id="task-rtk",
        session_id="sess-rtk",
        tool_call_id="call-rtk",
        rtk_status="applied",
    )

    assert replacement is not None
    assert hooks.telemetry_records[0]["raw_source"] == RAW_SOURCE_POST_RTK


def test_read_file_remains_contract_bounded_and_records_no_fake_savings(tmp_path) -> None:
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=ArtifactStore(tmp_path / "artifacts"),
    )

    replacement = hooks.transform_tool_result(
        tool_name="read_file",
        result=_large_payload("read-file"),
        status="success",
        session_id="sess-read",
        tool_call_id="call-read",
    )

    assert replacement is None
    assert hooks.telemetry_records == []
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []


def test_rollup_deduplicates_repeated_native_slimmer_savings_key() -> None:
    event = build_native_slimmer_event(
        mode="active_lossless",
        action="replace",
        tool_name="web_extract",
        session_id="sess-rollup",
        tool_call_id="call-rollup",
        artifact_id="art_sess_rollup_call",
        raw_sha256="b" * 64,
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=400,
        emitted_bytes=100,
        classification_reason="eligible_lossless_offload",
    )

    rollup = rollup_native_slimmer_events([event, dict(event)])

    assert rollup["event_count"] == 1
    assert rollup["saved_bytes"] == event["saved_bytes"]
    assert rollup["saved_tokens_est"] == event["saved_tokens_est"]
    assert rollup["by_raw_source"][RAW_SOURCE_TOOL_RESULT_RETURNED]["events"] == 1


class ExplodingTelemetry:
    def emit(self, event):  # type: ignore[no-untyped-def]
        raise RuntimeError("blackbox unavailable")


class FailOnceTelemetry:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.calls = 0

    def emit(self, event):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("blackbox transient outage")
        self.records.append(dict(event))


def test_blackbox_telemetry_emit_failure_fails_open_without_marker(tmp_path) -> None:
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"telemetry-fail-secret",
        telemetry=ExplodingTelemetry(),
    )

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("telemetry-fail"),
        status="success",
        session_id="sess-telemetry-fail",
        tool_call_id="call-telemetry-fail",
    )

    assert replacement is None
    assert hooks.failures
    assert "blackbox unavailable" in hooks.failures[-1]


def test_telemetry_emit_failure_does_not_reuse_or_rehydrate_untelemetried_marker(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    raw = _large_payload("telemetry-restart-fail")

    process_a = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=store,
        secret=b"telemetry-restart-secret",
        telemetry=ExplodingTelemetry(),
    )
    first = process_a.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-telemetry-restart",
        tool_call_id="call-telemetry-restart",
    )
    retry_same_process = process_a.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-telemetry-restart",
        tool_call_id="call-telemetry-restart",
    )

    assert first is None
    assert retry_same_process is None

    process_b = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=store,
        secret=b"telemetry-restart-secret",
        telemetry=ExplodingTelemetry(),
    )
    after_restart = process_b.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-telemetry-restart",
        tool_call_id="call-telemetry-restart",
    )

    assert after_restart is None
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []


def test_transient_telemetry_emit_failure_does_not_permanently_poison_same_result(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    telemetry = FailOnceTelemetry()
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=store,
        secret=b"telemetry-transient-secret",
        telemetry=telemetry,
    )
    raw = _large_payload("telemetry-transient")

    first = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-telemetry-transient",
        tool_call_id="call-telemetry-transient",
    )
    second = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-telemetry-transient",
        tool_call_id="call-telemetry-transient",
    )

    assert first is None
    assert second is not None
    parsed = parse_marker(second)
    assert parsed is not None
    assert store.read_record(parsed.fields["id"], session_id="sess-telemetry-transient")["raw_text"] == raw
    assert len(telemetry.records) == 1
    assert telemetry.records[0]["raw_sha256"] == sha256_text(raw)
