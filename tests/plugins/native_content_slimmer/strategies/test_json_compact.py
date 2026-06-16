from __future__ import annotations

import ast
import importlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.breaker import ExpansionRateCircuitBreaker
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore, raw_byte_len
from plugins.native_content_slimmer.tools import handle_expand_artifact
from plugins.native_content_slimmer.strategies import registry
from plugins.native_content_slimmer.strategies.base import run_with_timeout_guard

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "json"
DOCKER_PS_FIXTURE = FIXTURE_DIR / "docker_ps_80_one_unhealthy.json"


def _load_fixture() -> str:
    return DOCKER_PS_FIXTURE.read_text(encoding="utf-8")


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


@pytest.fixture(autouse=True)
def reset_registry() -> Iterator[None]:
    registry.clear_registry_for_tests()
    yield
    registry.clear_registry_for_tests()


def _json_compact_module():
    import plugins.native_content_slimmer.strategies.json_compact as json_compact

    return importlib.reload(json_compact)


def test_json_compact_self_registers_json_lanes() -> None:
    json_compact = _json_compact_module()
    json_compact.register()

    for tool_name in ("web_extract", "terminal", "terminal-json"):
        selection = registry.select_compressor(tool_name=tool_name, content_class="json")
        assert selection is not None
        assert selection.strategy_name == "json_compact"
        assert selection.eval_run_id
        assert selection.threshold == "GO"

    assert registry.select_compressor(tool_name="web_extract", content_class="text") is None


def test_docker_ps_unhealthy_state_and_all_object_keys_survive_compaction() -> None:
    json_compact = _json_compact_module()
    raw = _load_fixture()
    original = json.loads(raw)

    view = json_compact.JsonCompactCompressor().compress(raw, params={})

    assert view is not None
    assert view.strategy_name == "json_compact"
    assert view.lossy_view is True
    assert view.recoverable is True
    assert view.view_bytes == raw_byte_len(view.view_text)
    compressed = json.loads(view.view_text)

    original_container_keys = [set(container) for container in original]
    compressed_container_keys = [set(container) for container in compressed]
    assert compressed_container_keys == original_container_keys

    states = [container["State"] for container in compressed]
    assert states.count("unhealthy") == 1
    assert states[37] == "unhealthy"
    assert compressed[37]["Names"] == "container-037"
    assert compressed[37]["Status"] == "Up 3 hours (health: unhealthy)"

    assert compressed[37]["Command"].startswith("…")
    assert "chars" in compressed[37]["Command"]
    assert "--very-long-flag value --very-long-flag" not in view.view_text

    for before, after in zip(_walk_dicts(original), _walk_dicts(compressed), strict=True):
        assert set(after) == set(before)


def test_compressed_marker_expands_to_byte_exact_original(tmp_path: Path) -> None:
    json_compact = _json_compact_module()
    json_compact.register()
    raw = _load_fixture()
    store = ArtifactStore(tmp_path / "artifacts")
    breaker = ExpansionRateCircuitBreaker()
    for _ in range(20):
        breaker.record_result(("web_extract", "json", "json_compact"), expanded=False)
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(
            enabled=True,
            mode="active_lossless",
            compression_mode="active",
            min_bytes=100,
            preview_bytes=120,
        ),
        store=store,
        secret=b"json-compact-test-secret",
        breaker=breaker,
    )

    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-json-compact",
        tool_call_id="call-json-compact",
    )

    assert replacement is not None
    parsed = parse_marker(replacement)
    assert parsed is not None
    assert parsed.fields["strategy"] == "json_compact"
    assert parsed.fields["lossy_view"] == "true"
    assert parsed.fields["recoverable"] == "true"
    assert '"State": "unhealthy"' in parsed.preview
    assert "[... omitted" not in parsed.preview

    expanded = json.loads(
        handle_expand_artifact(
            {"id": parsed.fields["id"], "max_bytes": 200_000, "range": None},
            session_id="sess-json-compact",
            store=store,
        )
    )
    assert expanded["ok"] is True
    assert expanded["content"] == raw


def test_json_compact_is_byte_deterministic_across_100_runs() -> None:
    json_compact = _json_compact_module()
    raw = _load_fixture()
    compressor = json_compact.JsonCompactCompressor()

    views: list[str] = []
    for _ in range(100):
        view = compressor.compress(raw, params={})
        assert view is not None
        views.append(view.view_text)

    assert len(set(views)) == 1


def test_json_compact_imports_no_model_network_or_provider_clients() -> None:
    strategy_path = (
        Path(__file__).parents[4]
        / "plugins"
        / "native_content_slimmer"
        / "strategies"
        / "json_compact.py"
    )
    tree = ast.parse(strategy_path.read_text(encoding="utf-8"))
    denied = {
        "anthropic",
        "litellm",
        "openai",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "subprocess",
    }
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])

    assert imports.isdisjoint(denied)


def test_json_bomb_and_control_chars_fail_open_within_50ms() -> None:
    json_compact = _json_compact_module()
    compressor = json_compact.JsonCompactCompressor()
    cases = [
        "[" * 4096 + "0" + "]" * 4096,
        '{"bad": "value \x00"}',
    ]

    for raw in cases:
        started = time.perf_counter()
        direct = compressor.compress(raw, params={})
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert direct is None
        assert elapsed_ms < 50
        assert run_with_timeout_guard(compressor, raw, params={}, timeout_ms=50) is None
