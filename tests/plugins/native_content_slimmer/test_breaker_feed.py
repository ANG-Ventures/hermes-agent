from __future__ import annotations

import inspect
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from plugins.native_content_slimmer.breaker import (
    BREAKER_STATE_CLOSED,
    BREAKER_STATE_NOT_READY,
    BREAKER_STATE_OPEN,
    ExpansionRateCircuitBreaker,
)
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore
from plugins.native_content_slimmer.strategies import registry as strategy_registry
from plugins.native_content_slimmer.strategies.base import CompressedView
from model_tools import handle_function_call
from plugins.native_content_slimmer.tools import EXPAND_ARTIFACT_NAME, register_tools
from tools.registry import registry as tool_registry


class _RegisteredToolContext:
    def register_tool(self, **kwargs: Any) -> None:
        tool_registry.register(override=True, **kwargs)


class _RecordingCompressor:
    def __init__(self, view_text: str = "COMPRESSED VIEW") -> None:
        self.view_text = view_text
        self.calls: list[tuple[str, dict[str, object]]] = []

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView:
        self.calls.append((raw, dict(params)))
        return CompressedView(
            view_text=self.view_text,
            view_bytes=len(self.view_text.encode("utf-8")),
            strategy_name="fake_compact",
        )


class _ObservingBreaker(ExpansionRateCircuitBreaker):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observe_callers: list[str] = []

    def observe(self, lane: Any, *, expanded: bool = True):  # type: ignore[no-untyped-def]
        self.observe_callers.append(inspect.stack()[1].function)
        return super().observe(lane, expanded=expanded)


@pytest.fixture(autouse=True)
def _strategy_registry() -> Iterator[None]:
    strategy_strategy = _RecordingCompressor()
    strategy_registry.clear_registry_for_tests()
    strategy_registry.register_compressor(
        tool_name="web_extract",
        content_class="text",
        compressor=strategy_strategy,
        eval_run_id="eval-pass-fixture",
        threshold="GO",
        strategy_name="fake_compact",
    )
    yield
    strategy_registry.clear_registry_for_tests()


@pytest.fixture
def _restore_expand_artifact_tool() -> Iterator[None]:
    previous = tool_registry.get_entry(EXPAND_ARTIFACT_NAME)
    tool_registry.deregister(EXPAND_ARTIFACT_NAME)
    try:
        yield
    finally:
        tool_registry.deregister(EXPAND_ARTIFACT_NAME)
        if previous is not None:
            tool_registry.register(
                name=previous.name,
                toolset=previous.toolset,
                schema=previous.schema,
                handler=previous.handler,
                check_fn=previous.check_fn,
                requires_env=previous.requires_env,
                is_async=previous.is_async,
                description=previous.description,
                emoji=previous.emoji,
                max_result_size_chars=previous.max_result_size_chars,
                dynamic_schema_overrides=previous.dynamic_schema_overrides,
                override=True,
            )


def _raw(label: str = "feed") -> str:
    return f"{label}-HEAD\n" + (f"{label} useful evidence line\n" * 900) + f"{label}-TAIL\n"


def _cfg(*, mode: str = "shadow", compression_mode: str = "canary") -> NativeContentSlimmerConfig:
    return NativeContentSlimmerConfig(
        enabled=True,
        mode=mode,
        compression_mode=compression_mode,
        compression_canary_percent=100.0,
        min_bytes=100,
        preview_bytes=120,
        artifact_gc_after_write_every=0,
    )


def _lane() -> tuple[str, str, str]:
    return ("web_extract", "text", "fake_compact")


def _warm_breaker(*, observe: bool = False) -> ExpansionRateCircuitBreaker:
    cls = _ObservingBreaker if observe else ExpansionRateCircuitBreaker
    breaker = cls(window_size=10, min_samples=3, trip_threshold=0.75)
    for _ in range(3):
        breaker.record_result(_lane(), expanded=False)
    assert breaker.evaluate(_lane()).state == BREAKER_STATE_CLOSED
    return breaker


def _open_breaker() -> ExpansionRateCircuitBreaker:
    breaker = ExpansionRateCircuitBreaker(window_size=10, min_samples=3, trip_threshold=0.25)
    for _ in range(3):
        breaker.record_result(_lane(), expanded=True)
    assert breaker.evaluate(_lane()).state == BREAKER_STATE_OPEN
    return breaker


def _select_via_live_entrypoint(hooks: NativeContentSlimmerHooks, *, raw: str, session_id: str, call_id: str) -> str | None:
    return hooks.select_replacement(
        tool_name="web_extract",
        raw_text=raw,
        raw_source="tool-result-returned",
        status="success",
        session_id=session_id,
        tool_call_id=call_id,
        task_id="task-feed",
        turn_id="turn-feed",
        api_request_id="api-feed",
        duration_ms=3,
        metadata={},
    )


def _compressed_artifact(tmp_path: Path, breaker: ExpansionRateCircuitBreaker | None = None) -> tuple[ArtifactStore, str, ExpansionRateCircuitBreaker]:
    store = ArtifactStore(tmp_path / "artifacts")
    observe_breaker = breaker or ExpansionRateCircuitBreaker(window_size=10, min_samples=3, trip_threshold=0.75)
    hooks = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=store,
        secret=b"breaker-feed-test-secret",
        breaker=_warm_breaker(),
    )
    marker = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_raw("compressed"),
        status="success",
        session_id="sess-feed",
        tool_call_id="call-compressed",
        task_id="task-feed",
        turn_id="turn-feed",
        api_request_id="api-feed",
    )
    parsed = parse_marker(marker or "")
    assert parsed is not None
    assert parsed.fields["strategy"] == "fake_compact"
    return store, parsed.fields["id"], observe_breaker


def test_expansion_feeds_breaker_through_live_tool_dispatch(tmp_path: Path, _restore_expand_artifact_tool: None) -> None:
    breaker = _ObservingBreaker(window_size=10, min_samples=3, trip_threshold=0.75)
    store, artifact_id, _ = _compressed_artifact(tmp_path, breaker=breaker)
    register_tools(_RegisteredToolContext(), breaker=breaker, store=store)

    for idx in range(4):
        result = json.loads(
            handle_function_call(
                EXPAND_ARTIFACT_NAME,
                {"id": artifact_id},
                session_id="sess-feed",
                tool_call_id=f"expand-{idx}",
                skip_pre_tool_call_hook=True,
            )
        )
        assert result["ok"] is True, result
        assert result["content"].startswith("compressed-HEAD")
        assert breaker.evaluate(_lane()).sample_count == idx + 1

    assert breaker.evaluate(_lane()).expansion_count == 4
    assert breaker.observe_callers == ["expand_artifact_tool"] * 4


@pytest.mark.parametrize("breaker", [ExpansionRateCircuitBreaker(), _open_breaker()])
def test_not_ready_and_open_suppress_replacement_at_live_selection_entrypoint(tmp_path: Path, breaker: ExpansionRateCircuitBreaker) -> None:
    hooks = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"breaker-feed-test-secret",
        breaker=breaker,
    )

    replacement = _select_via_live_entrypoint(
        hooks,
        raw=_raw("suppress"),
        session_id="sess-suppress",
        call_id=f"call-{breaker.evaluate(_lane()).state.lower()}",
    )

    assert replacement is None
    assert breaker.evaluate(_lane()).state in {BREAKER_STATE_NOT_READY, BREAKER_STATE_OPEN}
    assert hooks.telemetry_records[-1]["classification_reason"].startswith("compression_breaker_")


def test_forced_selection_harness_enters_same_live_entrypoint_as_canary_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"breaker-feed-test-secret",
        breaker=_warm_breaker(),
    )
    callers: list[str] = []
    real_entrypoint = NativeContentSlimmerHooks.select_replacement

    def spy(self: NativeContentSlimmerHooks, **kwargs: Any) -> str | None:
        callers.append(inspect.stack()[1].function)
        return real_entrypoint(self, **kwargs)

    monkeypatch.setattr(NativeContentSlimmerHooks, "select_replacement", spy)

    production_marker = hooks.transform_tool_result(
        tool_name="web_extract",
        result=_raw("production"),
        status="success",
        session_id="sess-prod",
        tool_call_id="call-prod",
        task_id="task-feed",
        turn_id="turn-feed",
        api_request_id="api-feed",
    )
    forced_marker = _select_via_live_entrypoint(
        hooks,
        raw=_raw("forced"),
        session_id="sess-forced",
        call_id="call-forced",
    )

    assert callers == ["transform_tool_result", "_select_via_live_entrypoint"]
    assert parse_marker(production_marker or "") is not None
    assert parse_marker(forced_marker or "") is not None


def test_read_gate_and_observe_feed_are_differential_not_conflated(
    tmp_path: Path,
    _restore_expand_artifact_tool: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A: read-gate present + observe no-op + warm breaker still allows canary replacement.
    warm_noop = _warm_breaker()
    hooks_noop = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=ArtifactStore(tmp_path / "noop" / "artifacts"),
        secret=b"breaker-feed-test-secret",
        breaker=warm_noop,
    )
    monkeypatch.setattr(warm_noop, "observe", lambda *args, **kwargs: warm_noop.evaluate(_lane()))
    marker_noop = _select_via_live_entrypoint(hooks_noop, raw=_raw("noop"), session_id="sess-noop", call_id="call-noop")
    assert parse_marker(marker_noop or "") is not None
    assert warm_noop.evaluate(_lane()).sample_count == 3

    # B: read-gate present + observe live increments only after expand_artifact dispatch.
    live_observe = _ObservingBreaker(window_size=10, min_samples=3, trip_threshold=0.75)
    store, artifact_id, _ = _compressed_artifact(tmp_path / "live", breaker=live_observe)
    register_tools(_RegisteredToolContext(), breaker=live_observe, store=store)
    before = live_observe.evaluate(_lane()).sample_count
    expanded = json.loads(
        handle_function_call(
            EXPAND_ARTIFACT_NAME,
            {"id": artifact_id},
            session_id="sess-feed",
            tool_call_id="expand-live",
            skip_pre_tool_call_hook=True,
        )
    )
    assert expanded["ok"] is True
    assert live_observe.evaluate(_lane()).sample_count == before + 1

    # C: a pre-Phase-4 monkeypatch that bypasses the read gate replaces under NOT_READY;
    # the live entrypoint with the gate present suppresses that same NOT_READY state.
    not_ready = ExpansionRateCircuitBreaker()
    gated = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=ArtifactStore(tmp_path / "gated" / "artifacts"),
        secret=b"breaker-feed-test-secret",
        breaker=not_ready,
    )
    gated_marker = _select_via_live_entrypoint(gated, raw=_raw("gated"), session_id="sess-gated", call_id="call-gated")
    assert gated_marker is None
    assert not_ready.evaluate(_lane()).state == BREAKER_STATE_NOT_READY

    ungated = NativeContentSlimmerHooks(
        _cfg(mode="shadow", compression_mode="canary"),
        store=ArtifactStore(tmp_path / "ungated" / "artifacts"),
        secret=b"breaker-feed-test-secret",
        breaker=ExpansionRateCircuitBreaker(),
    )
    monkeypatch.setattr(
        ungated,
        "_apply_runtime_compression_gates",
        lambda *, classification, tool_name, raw_text, marker_key: classification,
    )
    ungated_marker = _select_via_live_entrypoint(ungated, raw=_raw("ungated"), session_id="sess-ungated", call_id="call-ungated")
    assert parse_marker(ungated_marker or "") is not None
