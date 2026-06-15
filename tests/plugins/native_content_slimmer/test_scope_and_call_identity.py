from __future__ import annotations

import logging

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig, load_slimmer_config
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore


def _large_payload(label: str = "scope") -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 900) + f"{label}-TAIL\n"


def _active_hooks(tmp_path, cfg: NativeContentSlimmerConfig | None = None) -> NativeContentSlimmerHooks:
    return NativeContentSlimmerHooks(
        cfg or NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"scope-and-identity-test-secret",
    )


def test_missing_session_id_fails_closed_without_shared_bucket(tmp_path) -> None:
    hooks = _active_hooks(tmp_path)
    raw_a = _large_payload("missing-session-a")
    raw_b = _large_payload("missing-session-b")

    first = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw_a,
        status="success",
        session_id="",
        tool_call_id="call-a",
    )
    second = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw_b,
        status="success",
        session_id=None,
        tool_call_id="call-b",
    )

    assert first is None
    assert second is None
    assert hooks.skip_reasons[-2:] == ["no_session_scope", "no_session_scope"]
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []


def test_missing_tool_call_id_fails_closed_instead_of_content_hashing(tmp_path) -> None:
    hooks = _active_hooks(tmp_path)

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("missing-call"),
        status="success",
        session_id="sess-missing-call",
        tool_call_id="",
        turn_id="turn-a",
        api_request_id="api-a",
    )

    assert replacement is None
    assert hooks.skip_reasons[-1] == "no_tool_call_id"
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []


def test_same_content_distinct_real_tool_calls_do_not_collapse(tmp_path) -> None:
    hooks = _active_hooks(tmp_path)
    raw = _large_payload("same-content")

    first = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-distinct-calls",
        tool_call_id="call-one",
    )
    second = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-distinct-calls",
        tool_call_id="call-two",
    )

    assert first is not None
    assert second is not None
    first_parsed = parse_marker(first)
    second_parsed = parse_marker(second)
    assert first_parsed is not None
    assert second_parsed is not None
    assert first_parsed.fields["id"] != second_parsed.fields["id"]
    assert len(list((tmp_path / "artifacts").glob("**/*.json"))) == 2


def test_same_tool_call_id_with_different_content_warns_and_uses_counter(tmp_path, caplog) -> None:
    hooks = _active_hooks(tmp_path)
    caplog.set_level(logging.WARNING, logger="plugins.native_content_slimmer.hook")

    first = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("call-reuse-first"),
        status="success",
        session_id="sess-reused-call",
        tool_call_id="call-reused",
    )
    second = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload("call-reuse-second"),
        status="success",
        session_id="sess-reused-call",
        tool_call_id="call-reused",
    )

    assert first is not None
    assert second is not None
    first_parsed = parse_marker(first)
    second_parsed = parse_marker(second)
    assert first_parsed is not None
    assert second_parsed is not None
    assert second_parsed.fields["id"] == f"{first_parsed.fields['id']}_1"
    assert "tool_call_id reused with different raw_sha256" in caplog.text


def test_read_file_hard_denied_even_when_config_allow_lists_it(tmp_path) -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "active_lossless",
                    "min_bytes": 1,
                    "preview_bytes": 32,
                    "allow_tools": ["read_file"],
                    "deny_tools": [],
                    "deny_on_status": [],
                }
            }
        }
    )
    assert "read_file" in cfg.allow_tools
    hooks = _active_hooks(tmp_path, cfg)

    replacement = hooks.transform_tool_result(
        tool_name="read_file",
        result=_large_payload("read-file-allow-listed"),
        status="success",
        session_id="sess-read-file",
        tool_call_id="call-read-file",
    )

    assert replacement is None
    assert hooks.skip_reasons[-1] == "read_file_contract_bounded"
    assert hooks.telemetry_records == []
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []
