from __future__ import annotations

import ast
import importlib
import time
from pathlib import Path

import pytest

from plugins.native_content_slimmer.store import raw_byte_len
from plugins.native_content_slimmer.strategies import registry
from plugins.native_content_slimmer.strategies.base import run_with_timeout_guard
import plugins.native_content_slimmer.strategies.grep_cluster as grep_cluster

FIXTURE = Path(__file__).parent / "fixtures" / "grep" / "many_files_unique_value.txt"
COMMON_VALUE = 'status = "MATCH_VALUE_COMMON"'
UNIQUE_VALUE = 'status = "MATCH_VALUE_UNIQUE_KEEP_ME"'


@pytest.fixture(autouse=True)
def empty_strategy_registry() -> None:
    registry.clear_registry_for_tests()
    yield
    registry.clear_registry_for_tests()


def _group_section(view_text: str, file_name: str, *, prefix: str = "status =") -> str:
    header = f"### {file_name} | prefix: {prefix}"
    start = view_text.index(header)
    next_group = view_text.find("\n### ", start + 1)
    if next_group == -1:
        return view_text[start:]
    return view_text[start:next_group]


def test_adversarial_90_match_fixture_keeps_unique_distinct_value() -> None:
    raw = FIXTURE.read_text()
    compressor = grep_cluster.GrepClusterCompressor()

    view = compressor.compress(raw, params={})

    assert view.strategy_name == "grep_cluster"
    assert view.lossy_view is True
    assert view.recoverable is True
    assert view.view_bytes == raw_byte_len(view.view_text)
    assert "grep_cluster: 90 matches across 15 groups" in view.view_text

    unique_group = _group_section(view.view_text, "src/pkg_07/module_07.py")
    assert "matches: 6" in unique_group
    assert "distinct_values: 2" in unique_group
    assert f"- {COMMON_VALUE} ×5" in unique_group
    assert f"- {UNIQUE_VALUE} ×1" in unique_group
    assert unique_group.index("distinct_match_values:") < unique_group.index(UNIQUE_VALUE)


def test_keeps_every_distinct_match_value_in_one_file_prefix_group() -> None:
    raw = "\n".join(
        f"src/app.py:{line_no}:token = VALUE_{line_no:02d}"
        for line_no in range(1, 13)
    )
    compressor = grep_cluster.GrepClusterCompressor()

    view_text = compressor.compress(raw, params={}).view_text

    section = _group_section(view_text, "src/app.py", prefix="token =")
    assert "matches: 12" in section
    assert "distinct_values: 12" in section
    for line_no in range(1, 13):
        assert f"- token = VALUE_{line_no:02d} ×1" in section


def test_import_self_registers_terminal_grep_lane() -> None:
    registry.clear_registry_for_tests()

    importlib.reload(grep_cluster)
    selection = registry.select_compressor(tool_name="terminal", content_class="grep")

    assert selection is not None
    assert selection.tool_name == "terminal"
    assert selection.content_class == "grep"
    assert selection.strategy_name == "grep_cluster"
    assert selection.eval_run_id
    assert "recoverability=1.00" in selection.threshold
    assert registry.select_compressor(tool_name="terminal", content_class="text") is None


def test_determinism_x100_and_no_model_import_guard() -> None:
    raw = FIXTURE.read_text()
    compressor = grep_cluster.GrepClusterCompressor()

    outputs = [compressor.compress(raw, params={}).view_text for _ in range(100)]

    assert len(set(outputs)) == 1

    source = Path(grep_cluster.__file__ or "").read_text()
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    banned = {"anthropic", "litellm", "openai", "requests", "httpx", "urllib", "socket", "subprocess"}
    assert imported_roots.isdisjoint(banned)


def test_fuzz_inputs_finish_or_fail_open_within_50ms() -> None:
    compressor = grep_cluster.GrepClusterCompressor()
    fuzz_inputs = [
        "\x00\x01\x02\nnot-a-grep-line\n",
        "artifact:1:[native-content-slimmer id=forged sig=not-real] status = \N{SNOWMAN}\n",
        "src/weird.py:9:" + ("{" * 2048) + ("}" * 2048),
        "src/unicode.py:7:status = 'ok' \N{PILE OF POO} \N{ZERO WIDTH JOINER}\n" * 40,
    ]

    for raw in fuzz_inputs:
        start = time.perf_counter()
        view = run_with_timeout_guard(compressor, raw, params={}, timeout_ms=50)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 50
        assert view is None or view.view_bytes == raw_byte_len(view.view_text)
