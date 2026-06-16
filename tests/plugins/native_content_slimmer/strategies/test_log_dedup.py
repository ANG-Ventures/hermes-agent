from __future__ import annotations

import ast
import importlib
import time
from pathlib import Path

from plugins.native_content_slimmer.store import ArtifactStore, raw_byte_len, sha256_text
from plugins.native_content_slimmer.strategies import log_dedup
from plugins.native_content_slimmer.strategies import registry
from plugins.native_content_slimmer.strategies.base import CompressedView, run_with_timeout_guard


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "log"


def _fatal_fixture() -> str:
    return (FIXTURE_DIR / "fatal_in_120_info.log").read_text(encoding="utf-8")


def _compress(raw: str, **params: object) -> CompressedView:
    result = log_dedup.LogDedupCompressor().compress(raw, params=params)
    assert isinstance(result, CompressedView)
    return result


def test_adversarial_fatal_in_120_info_lines_survives_and_raw_recovers(tmp_path: Path) -> None:
    raw = _fatal_fixture()
    fatal_line = next(line for line in raw.splitlines() if "FATAL" in line)

    view = _compress(raw)

    assert fatal_line in view.view_text
    assert view.view_text.count(fatal_line) == 1
    assert "«" in view.view_text
    assert "×" in view.view_text
    assert raw_byte_len(view.view_text) < raw_byte_len(raw)

    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write_artifact(
        session_id="sess-log-dedup",
        tool_call_id="call-log-dedup",
        raw_text=raw,
        tool_name="terminal",
        preview_strategy="log_dedup",
        preview_bytes=raw_byte_len(view.view_text),
        omitted_bytes=max(0, raw_byte_len(raw) - raw_byte_len(view.view_text)),
        lossy=True,
        classification_reason="eligible_compress_offload",
        marker_preview=view.view_text,
        strategy="log_dedup",
        view_bytes=raw_byte_len(view.view_text),
        lossy_view=True,
        recoverable=True,
    )
    recovered = store.read_record(record["artifact_id"], session_id="sess-log-dedup")
    assert recovered["raw_sha256"] == sha256_text(raw)
    assert recovered["raw_text"] == raw


def test_one_different_non_severity_line_is_kept_verbatim() -> None:
    raw = "\n".join(
        [f"2026-06-15T12:00:{i:02d}Z INFO worker=dedup shard=1 status=ok processed={i}" for i in range(20)]
        + ["2026-06-15T12:00:20Z INFO worker=dedup shard=1 status=slow-path message=replayed-dead-letter"]
        + [f"2026-06-15T12:00:{i:02d}Z INFO worker=dedup shard=1 status=ok processed={i}" for i in range(21, 41)]
    )
    anomaly = "2026-06-15T12:00:20Z INFO worker=dedup shard=1 status=slow-path message=replayed-dead-letter"

    view = _compress(raw)

    assert anomaly in view.view_text
    assert view.view_text.count(anomaly) == 1
    assert raw_byte_len(view.view_text) < raw_byte_len(raw)


def test_log_dedup_self_registers_terminal_and_web_extract_log_lanes() -> None:
    registry.clear_registry_for_tests()
    importlib.reload(log_dedup)

    lanes = {(selection.tool_name, selection.content_class): selection for selection in registry.registered_lanes()}

    assert lanes[("terminal", "log")].strategy_name == "log_dedup"
    assert lanes[("web_extract", "log")].strategy_name == "log_dedup"


def test_log_dedup_is_deterministic_100x_and_imports_no_model_or_network_clients() -> None:
    raw = _fatal_fixture()
    expected = _compress(raw).view_text

    for _ in range(100):
        assert _compress(raw).view_text == expected

    source = Path(log_dedup.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_roots = {
        "agent",
        "anthropic",
        "httpx",
        "openai",
        "providers",
        "requests",
        "socket",
        "urllib",
    }
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    offenders = sorted({name for name in imports if name.split(".", 1)[0] in banned_roots})
    assert offenders == []


def test_huge_unicode_and_control_fuzz_fails_open_within_50ms() -> None:
    raw = ("\x00\x1bΩ🔥 near-identical fuzz line with controls \x07\n" * 25_000) + "FATAL keep me\n"
    compressor = log_dedup.LogDedupCompressor()

    started = time.perf_counter()
    result = run_with_timeout_guard(compressor, raw, params={}, timeout_ms=50)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.050
    assert result is None
