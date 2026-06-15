from __future__ import annotations

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.store import ArtifactStore


def _large_payload(label: str) -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle\n" * 1600) + f"{label}-TAIL\n"


def test_terminal_uses_pre_truncation_hook_and_generic_hook_skips_terminal(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=store,
    )
    terminal_raw = _large_payload("terminal")

    terminal_replacement = hooks.transform_terminal_output(
        command="python big_output.py",
        output=terminal_raw,
        returncode=0,
        task_id="terminal-task",
        env_type="local",
    )

    assert terminal_replacement is None
    assert len(hooks.shadow_records) == 1
    terminal_record = hooks.shadow_records[0]
    assert terminal_record.tool_name == "terminal"
    assert terminal_record.raw_source == "pre-truncation-terminal"
    stored_terminal = store.read_record(
        terminal_record.artifact_id,
        session_id=terminal_record.session_id,
    )
    assert stored_terminal["raw_text"] == terminal_raw
    assert stored_terminal["raw_source"] == "pre-truncation-terminal"

    generic_terminal_replacement = hooks.transform_tool_result(
        tool_name="terminal",
        result=_large_payload("post-truncation-terminal-json"),
        status="success",
        session_id="sess-1",
        tool_call_id="call-terminal-post",
    )

    assert generic_terminal_replacement is None
    assert len(hooks.shadow_records) == 1


def test_generic_hook_records_non_terminal_tool_result_with_as_returned_source(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=store,
    )
    web_raw = _large_payload("web")

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=web_raw,
        status="success",
        session_id="sess-web",
        tool_call_id="call-web",
        task_id="task-web",
    )

    assert replacement is None
    assert len(hooks.shadow_records) == 1
    record = hooks.shadow_records[0]
    assert record.tool_name == "web_extract"
    assert record.raw_source == "tool-result-returned"
    stored = store.read_record(record.artifact_id, session_id="sess-web")
    assert stored["raw_text"] == web_raw
    assert stored["raw_source"] == "tool-result-returned"


def test_generic_hook_skips_read_file_because_it_is_pagination_bounded(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=store,
    )

    replacement = hooks.transform_tool_result(
        tool_name="read_file",
        result=_large_payload("read-file-page"),
        status="success",
        session_id="sess-read",
        tool_call_id="call-read",
    )

    assert replacement is None
    assert hooks.shadow_records == []
    assert list((tmp_path / "artifacts").glob("**/*.json")) == []
