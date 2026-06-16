from __future__ import annotations

import ast
import importlib
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from plugins.blackbox.native_slimmer_schema import RAW_SOURCE_TOOL_RESULT_RETURNED
from plugins.native_content_slimmer.classifier import COMPRESS_OFFLOAD, Classification
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore, raw_byte_len, sha256_text
from plugins.native_content_slimmer.strategies import registry
from plugins.native_content_slimmer.strategies import diff_collapse
from plugins.native_content_slimmer.strategies.base import run_with_timeout_guard


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "diff"
ADVERSARIAL_LARGE_HUNK = FIXTURE_DIR / "adversarial_large_hunk.diff"


@pytest.fixture(autouse=True)
def registered_diff_strategy() -> Iterator[None]:
    registry.clear_registry_for_tests()
    diff_collapse.register()
    yield
    registry.clear_registry_for_tests()


def _large_git_diff() -> str:
    return ADVERSARIAL_LARGE_HUNK.read_text(encoding="utf-8")


def _diff_classification(raw: str) -> Classification:
    return Classification(
        eligible=True,
        reason="eligible_compress_offload",
        raw_bytes=raw_byte_len(raw),
        content_class="diff",
        preview=None,
        outcome=COMPRESS_OFFLOAD,
        recommended_strategy="diff_collapse",
    )


def test_adversarial_large_diff_keeps_critical_change_collapses_context_and_recovers_raw(tmp_path: Path) -> None:
    raw = _large_git_diff()
    compressor = diff_collapse.DiffCollapseCompressor()

    view = compressor.compress(raw, params={"context_lines": 2, "min_collapse_lines": 8})

    assert "@@ -1,361 +1,361 @@" in view.view_text
    assert "-    enable_admin_bypass = True  # CRITICAL_SECURITY_BUG" in view.view_text
    assert "+    enable_admin_bypass = False  # CRITICAL_SECURITY_FIX" in view.view_text
    assert "«178 unchanged lines»" in view.view_text
    assert view.view_text.count("«178 unchanged lines»") == 2
    assert " context before 0000: filler that should collapse" not in view.view_text
    assert " context before 0178: filler that should collapse" in view.view_text
    assert " context after 0001: filler that should collapse" in view.view_text
    assert raw_byte_len(view.view_text) < raw_byte_len(raw)

    store = ArtifactStore(tmp_path / "artifacts")
    hooks = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless", min_bytes=1, preview_bytes=120),
        store=store,
        secret=b"diff-collapse-test-secret",
    )
    marker = hooks._persist_and_build_marker(
        tool_name="terminal",
        raw_text=raw,
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        status="success",
        session_id="sess-diff-collapse",
        tool_call_id="call-diff-collapse",
        task_id="task-diff-collapse",
        turn_id="turn-diff-collapse",
        api_request_id="req-diff-collapse",
        duration_ms=None,
        metadata={},
        classification=_diff_classification(raw),
    )

    assert marker is not None
    parsed = parse_marker(marker)
    assert parsed is not None
    assert parsed.fields["strategy"] == "diff_collapse"
    assert parsed.fields["lossy_view"] == "true"
    assert parsed.fields["recoverable"] == "true"
    assert "+    enable_admin_bypass = False  # CRITICAL_SECURITY_FIX" in parsed.preview
    assert "«178 unchanged lines»" in parsed.preview

    expanded = store.expand_artifact(parsed.fields["id"], session_id="sess-diff-collapse")
    assert expanded["ok"] is True
    assert expanded["content"] == raw
    assert expanded["raw_sha256"] == sha256_text(raw)


def test_diff_collapse_is_byte_deterministic_x100_and_never_drops_changed_lines() -> None:
    raw = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -10,14 +10,16 @@ def handler():",
            *[f" unchanged pre {idx}" for idx in range(20)],
            "-    old_value = compute_old()",
            "+    new_value = compute_new()",
            "+    literal_plus_line = '+++ content, not a file header'",
            "-    literal_minus_line = '--- content, not a file header'",
            *[f" unchanged post {idx}" for idx in range(20)],
            "",
        ]
    )
    changed_lines = [line for line in raw.splitlines() if line.startswith(('+', '-'))]
    compressor = diff_collapse.DiffCollapseCompressor()

    outputs = [compressor.compress(raw, params={"context_lines": 1, "min_collapse_lines": 4}).view_text for _ in range(100)]

    assert len(set(outputs)) == 1
    for changed_line in changed_lines:
        assert changed_line in outputs[0]


def test_diff_collapse_self_registers_terminal_diff_lane() -> None:
    registry.clear_registry_for_tests()
    importlib.reload(diff_collapse)

    selection = registry.select_compressor(tool_name="terminal", content_class="diff")

    assert selection is not None
    assert selection.tool_name == "terminal"
    assert selection.content_class == "diff"
    assert selection.strategy_name == "diff_collapse"
    assert selection.eval_run_id
    assert selection.threshold == "GO"


def test_diff_collapse_imports_no_model_or_network_clients() -> None:
    source = Path(diff_collapse.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden_roots = {
        "anthropic",
        "httpx",
        "openai",
        "requests",
        "urllib",
    }
    forbidden_modules = {
        "agent.auxiliary_client",
        "hermes_cli.providers",
    }
    imported_roots = {name.split(".", 1)[0] for name in imports}
    assert imported_roots.isdisjoint(forbidden_roots)
    assert imports.isdisjoint(forbidden_modules)


def test_diff_collapse_fuzz_input_completes_inside_50ms_guard() -> None:
    fuzz_lines = []
    for idx in range(600):
        fuzz_lines.append(f" weird context {idx:04d} \x00 Ω ≈ marker-lookalike [native-content-slimmer id={idx}]")
    raw = "\n".join(
        [
            "diff --git a/fuzz.txt b/fuzz.txt",
            "--- a/fuzz.txt",
            "+++ b/fuzz.txt",
            "@@ -1,602 +1,602 @@",
            *fuzz_lines,
            "-control chars before \x00\x01\x02",
            "+control chars after \x00\x01\x02 plus unicode 🧪",
            "",
        ]
    )
    compressor = diff_collapse.DiffCollapseCompressor()

    start = time.perf_counter()
    view = compressor.compress(raw, params={"context_lines": 1, "min_collapse_lines": 4})
    elapsed_ms = (time.perf_counter() - start) * 1000
    guarded = run_with_timeout_guard(
        compressor,
        raw,
        params={"context_lines": 1, "min_collapse_lines": 4},
        timeout_ms=50,
    )

    assert elapsed_ms < 50
    assert guarded is not None
    assert view.view_text == guarded.view_text
    assert "+control chars after" in view.view_text
