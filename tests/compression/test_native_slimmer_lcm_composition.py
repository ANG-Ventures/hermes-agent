from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from plugins.native_content_slimmer.marker import MARKER_TOKEN, parse_marker

ADAPTER_PATH = Path(__file__).resolve().parent / "adapters" / "native_slimmer_lcm.py"
HERMES_HOME_REPO = Path(__file__).resolve().parents[4]
BATTERY_ROOT = HERMES_HOME_REPO / "projects" / "context-compression-eval" / "battery"


def _load_adapter_module():
    spec = importlib.util.spec_from_file_location("native_slimmer_lcm_adapter", ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_fixture() -> str:
    return (
        "deploy log header\n"
        + ("noise line: worker heartbeat ok\n" * 260)
        + "FINAL_DECISION=GO-LCM evidence=artifact-id-must-survive\n"
        + ("noise line: summarizer filler ok\n" * 260)
        + "deploy log tail\n"
    )


def test_native_slimmer_marker_survives_lcm_summary_and_expands_raw(tmp_path) -> None:
    module = _load_adapter_module()
    adapter = module.NativeSlimmerLCMAdapter(mode="native-slimmer+lcm", artifact_root=tmp_path / "artifacts")
    raw = _raw_fixture()

    result = adapter.compress(raw, task={"id": "composition-survival"})

    assert result.mode == "native-slimmer+lcm"
    assert result.artifact_id
    assert MARKER_TOKEN in result.shown
    assert result.artifact_id in result.shown
    assert "FINAL_DECISION=GO-LCM" not in result.shown
    assert "summary_inspected_raw=false" in result.shown
    assert result.metadata["lcm_engine_used"] is True
    assert result.metadata["lcm_marker_survived"] is True
    assert result.metadata["lcm_grep_found_id"] is True
    assert result.metadata["lcm_expand_found_id"] is True
    assert result.metadata["artifact_id_recoverable"] is True

    parsed = parse_marker(result.marker)
    assert parsed is not None
    assert parsed.fields["id"] == result.artifact_id

    expanded = adapter.expand(result.artifact_id)
    assert expanded == raw


def test_native_slimmer_battery_adapter_scores_recovery_against_raw(tmp_path) -> None:
    if not BATTERY_ROOT.is_dir():
        pytest.skip(f"PRD #3 battery not present at {BATTERY_ROOT}")
    module = _load_adapter_module()
    adapter = module.NativeSlimmerLCMAdapter(mode="native-slimmer", artifact_root=tmp_path / "artifacts")
    raw = _raw_fixture()
    task = {
        "id": "native-recovery-raw-cite",
        "raw": raw,
        "oracle": {
            "expected": "GO-LCM",
            "acceptable": ["go-lcm"],
            "match": "contains",
            "forbid": ["NO-GO"],
            "require_cite": True,
        },
    }

    compressed = adapter.compress(raw, task=task)
    assert compressed.artifact_id in compressed.shown
    assert compressed.artifact_id_recoverable is True

    answer = {
        "answer": "GO-LCM",
        "evidence": "FINAL_DECISION=GO-LCM evidence=artifact-id-must-survive",
    }
    scored = adapter.score_answer(answer, task)
    assert scored.passed is True
    assert scored.reason == "ok"

    lying_answer = {
        "answer": "GO-LCM",
        "evidence": "EXPAND returned FINAL_DECISION=GO-LCM evidence=artifact-id-must-survive",
    }
    lying_scored = adapter.score_answer(lying_answer, task)
    assert lying_scored.passed is False
    assert "RAW" in lying_scored.reason or "raw" in lying_scored.reason


def test_live_rollout_verdict_requires_real_session_gate(tmp_path) -> None:
    module = _load_adapter_module()
    adapter = module.NativeSlimmerLCMAdapter(mode="native-slimmer+lcm", artifact_root=tmp_path / "artifacts")
    raw = _raw_fixture()

    adapter.compress(raw, task={"id": "verdict"})
    verdict = adapter.live_rollout_verdict(real_session_expand_rate=None, baseline_pass_rate=1.0)

    assert verdict.verdict == "NO-GO"
    assert "real-session" in verdict.reason
    assert verdict.details["unit_composition_passed"] is True
