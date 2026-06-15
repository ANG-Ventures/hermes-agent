from __future__ import annotations

from typing import Any

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.native_content_slimmer import register
from plugins.native_content_slimmer.marker import parse_marker


class FakeContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


def _large_payload(label: str) -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 1000) + f"{label}-TAIL\n"


def _runtime_from_hook(ctx: FakeContext):
    for name, callback in ctx.hooks:
        if name == "transform_tool_result":
            runtime = getattr(callback, "__self__", None)
            assert runtime is not None
            return runtime
    raise AssertionError("transform_tool_result hook not registered")


def test_gc_on_start_and_session_lifecycle_are_registered_and_call_gc(monkeypatch, tmp_path) -> None:
    import plugins.native_content_slimmer.hook as hook_mod

    calls: list[dict[str, Any]] = []

    def fake_collect_garbage(root=None, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"root": root, **kwargs})
        return {"ok": True, "over_cap": False, "deleted_count": 0}

    monkeypatch.setattr(hook_mod, "collect_garbage", fake_collect_garbage, raising=False)
    token = set_hermes_home_override(tmp_path / "home")
    try:
        ctx = FakeContext()
        cfg = register(
            ctx,
            config={
                "plugins": {
                    "native_content_slimmer": {
                        "enabled": True,
                        "mode": "shadow",
                        "artifact_ttl_days": 5,
                        "artifact_max_bytes_per_profile": 4096,
                        "artifact_gc_on_start": True,
                        "artifact_gc_on_session_end": True,
                        "artifact_gc_on_session_reset": True,
                        "artifact_gc_after_write_every": 0,
                    }
                }
            },
        )

        assert cfg.enabled is True
        assert calls, "gc_on_start did not call collect_garbage"
        assert calls[0]["ttl_days"] == 5
        assert calls[0]["max_bytes"] == 4096
        hook_names = [name for name, _ in ctx.hooks]
        assert "on_session_end" in hook_names
        assert "on_session_reset" in hook_names

        for name, callback in ctx.hooks:
            if name == "on_session_end":
                callback(session_id="sess-ended")
            if name == "on_session_reset":
                callback(session_id="sess-reset")

        assert [call.get("active_session_id") for call in calls[1:]] == ["sess-ended", "sess-reset"]
    finally:
        reset_hermes_home_override(token)


def test_gc_after_write_every_runs_with_active_session_id(monkeypatch, tmp_path) -> None:
    import plugins.native_content_slimmer.hook as hook_mod

    calls: list[dict[str, Any]] = []

    def fake_collect_garbage(root=None, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"root": root, **kwargs})
        return {"ok": True, "over_cap": False, "deleted_count": 0}

    monkeypatch.setattr(hook_mod, "collect_garbage", fake_collect_garbage, raising=False)
    token = set_hermes_home_override(tmp_path / "home")
    try:
        ctx = FakeContext()
        register(
            ctx,
            config={
                "plugins": {
                    "native_content_slimmer": {
                        "enabled": True,
                        "mode": "active_lossless",
                        "artifact_ttl_days": 14,
                        "artifact_max_bytes_per_profile": 1,
                        "artifact_gc_on_start": False,
                        "artifact_gc_after_write_every": 2,
                    }
                }
            },
        )
        runtime = _runtime_from_hook(ctx)

        first = runtime.transform_tool_result(
            tool_name="web_extract",
            result=_large_payload("write-one"),
            status="success",
            session_id="sess-active-gc",
            tool_call_id="call-one",
        )
        second = runtime.transform_tool_result(
            tool_name="web_extract",
            result=_large_payload("write-two"),
            status="success",
            session_id="sess-active-gc",
            tool_call_id="call-two",
        )

        assert parse_marker(first) is not None
        assert parse_marker(second) is not None
        assert len(calls) == 1
        assert calls[0]["active_session_id"] == "sess-active-gc"
        assert calls[0]["max_bytes"] == 1
        assert calls[0]["ttl_days"] == 14
    finally:
        reset_hermes_home_override(token)
