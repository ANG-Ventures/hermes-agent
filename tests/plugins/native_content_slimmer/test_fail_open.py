from __future__ import annotations

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.store import ArtifactStore


class ExplodingStore:
    def write_artifact(self, **_: object) -> dict[str, object]:
        raise RuntimeError("store unavailable")


def _large_payload() -> str:
    return "HEAD\n" + ("line\n" * 3000) + "TAIL\n"


def test_tool_result_hook_fails_open_when_artifact_write_raises() -> None:
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=ExplodingStore(),
    )

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload(),
        status="success",
        session_id="sess-1",
        tool_call_id="call-1",
    )

    assert replacement is None
    assert hooks.shadow_records == []
    assert hooks.failures
    assert "store unavailable" in hooks.failures[-1]


def test_terminal_hook_fails_open_when_artifact_write_raises() -> None:
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=ExplodingStore(),
    )

    replacement = hooks.transform_terminal_output(
        command="python generate_big_output.py",
        output=_large_payload(),
        returncode=0,
        task_id="task-1",
        session_id="sess-terminal-fail-open",
        tool_call_id="call-terminal-fail-open",
        env_type="local",
    )

    assert replacement is None
    assert hooks.shadow_records == []
    assert hooks.failures
    assert "store unavailable" in hooks.failures[-1]


def test_hook_fails_open_when_classifier_raises(monkeypatch, tmp_path) -> None:
    import plugins.native_content_slimmer.hook as hook_module

    def boom(**_: object) -> object:
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(hook_module, "classify_tool_result", boom)
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="shadow"),
        store=ArtifactStore(tmp_path / "artifacts"),
    )

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_large_payload(),
        status="success",
        session_id="sess-1",
        tool_call_id="call-1",
    )

    assert replacement is None
    assert hooks.shadow_records == []
    assert hooks.failures
    assert "classifier unavailable" in hooks.failures[-1]
