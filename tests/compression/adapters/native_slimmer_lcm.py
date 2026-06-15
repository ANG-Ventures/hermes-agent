"""PRD #3-style adapters for native-slimmer and native-slimmer+LCM.

This module is intentionally test-local. It gives the compression battery a thin
adapter seam without modifying the external battery project under
/Users/alexgierczyk/.hermes/projects/context-compression-eval/battery.
"""

from __future__ import annotations

import importlib.util
import importlib
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import MARKER_TOKEN, parse_marker
from plugins.native_content_slimmer.store import ArtifactStore, sha256_text

WORKTREE_ROOT = Path(__file__).resolve().parents[3]
LCM_PLUGIN_DIR = WORKTREE_ROOT / "staging" / "lcm-profile" / "plugins" / "hermes-lcm"
HERMES_HOME_REPO = WORKTREE_ROOT.parents[1]
BATTERY_PROJECT_ROOT = HERMES_HOME_REPO / "projects" / "context-compression-eval"


class CompressionResult:
    def __init__(
        self,
        *,
        mode: str,
        shown: str,
        marker: str,
        artifact_id: str,
        raw_sha256: str,
        raw_bytes: int,
        shown_bytes: int,
        metadata: Mapping[str, Any],
    ) -> None:
        self.mode = mode
        self.shown = shown
        self.marker = marker
        self.artifact_id = artifact_id
        self.raw_sha256 = raw_sha256
        self.raw_bytes = raw_bytes
        self.shown_bytes = shown_bytes
        self.metadata = dict(metadata)

    @property
    def artifact_id_recoverable(self) -> bool:
        return bool(self.metadata.get("artifact_id_recoverable"))

    @property
    def lcm_marker_survived(self) -> bool:
        return bool(self.metadata.get("lcm_marker_survived"))


class ScoreResult:
    def __init__(self, *, passed: bool, reason: str) -> None:
        self.passed = bool(passed)
        self.reason = reason


class VerdictResult:
    def __init__(self, *, verdict: str, reason: str, details: Mapping[str, Any]) -> None:
        self.verdict = verdict
        self.reason = reason
        self.details = dict(details)


class NativeSlimmerLCMAdapter:
    """Battery adapter for PRD #2 Layer A alone or Layer A composed with LCM.

    Interface matches PRD #3's declared seam: ``name``, ``compress(raw)``, and
    ``expand(id)``. The +LCM mode runs the staged hermes-lcm engine with a
    deterministic summary seam so tests stay offline while still exercising LCM
    DAG compaction and raw-message retrieval.
    """

    VALID_MODES = frozenset({"native-slimmer", "native-slimmer+lcm"})

    def __init__(
        self,
        *,
        mode: str = "native-slimmer",
        artifact_root: str | Path | None = None,
        session_id: str = "battery-native-slimmer-session",
        secret: bytes | str = b"native-slimmer-lcm-battery-test-secret",
    ) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"unsupported native slimmer battery mode: {mode}")
        self.mode = mode
        self.name = mode
        self.session_id = session_id
        self.artifact_root = Path(artifact_root) if artifact_root is not None else Path(
            tempfile.mkdtemp(prefix="native-slimmer-artifacts-")
        )
        self.store = ArtifactStore(self.artifact_root)
        self.hooks = NativeContentSlimmerHooks(
            NativeContentSlimmerConfig(enabled=True, mode="active_lossless"),
            store=self.store,
            secret=secret,
        )
        self.last_result: CompressionResult | None = None

    def compress(self, raw: str, task: Mapping[str, Any] | None = None) -> CompressionResult:
        task = dict(task or {})
        raw_text = str(raw or "")
        task_id = str(task.get("id") or "battery-task")
        tool_call_id = f"{self.mode}-{sha256_text(task_id + raw_text)[:12]}"

        marker = self.hooks.transform_tool_result(
            tool_name="web_extract",
            result=raw_text,
            status="success",
            session_id=self.session_id,
            tool_call_id=tool_call_id,
            task_id=task_id,
            turn_id=f"turn-{task_id}",
            api_request_id=f"api-{task_id}",
        )
        if marker is None:
            result = CompressionResult(
                mode=self.mode,
                shown=raw_text,
                marker="",
                artifact_id="",
                raw_sha256=sha256_text(raw_text),
                raw_bytes=len(raw_text.encode("utf-8")),
                shown_bytes=len(raw_text.encode("utf-8")),
                metadata={
                    "compressed": False,
                    "artifact_id_recoverable": False,
                    "lcm_marker_survived": False,
                    "reason": "native slimmer did not emit a marker",
                },
            )
            self.last_result = result
            return result

        parsed = parse_marker(marker)
        if parsed is None:
            raise RuntimeError("native slimmer returned an unparsable marker")
        artifact_id = parsed.fields["id"]
        expanded = self.expand(artifact_id)
        artifact_id_recoverable = expanded == raw_text

        metadata: dict[str, Any] = {
            "compressed": True,
            "artifact_id_recoverable": artifact_id_recoverable,
            "lcm_marker_survived": self.mode == "native-slimmer",
            "lcm_engine_used": False,
            "summary_inspected_raw": False,
        }
        shown = marker
        if self.mode == "native-slimmer+lcm":
            shown, lcm_metadata = self._summarize_marker_with_lcm(
                marker=marker,
                artifact_id=artifact_id,
                tool_call_id=tool_call_id,
                task_id=task_id,
            )
            metadata.update(lcm_metadata)

        result = CompressionResult(
            mode=self.mode,
            shown=shown,
            marker=marker,
            artifact_id=artifact_id,
            raw_sha256=parsed.fields["raw_sha256"],
            raw_bytes=int(parsed.fields["original_bytes"]),
            shown_bytes=len(shown.encode("utf-8")),
            metadata=metadata,
        )
        self.last_result = result
        return result

    def expand(self, artifact_id: str) -> str | None:
        if not artifact_id:
            return None
        expanded = self.store.expand_artifact(artifact_id, session_id=self.session_id)
        if not expanded.get("ok"):
            return None
        content = expanded.get("content")
        return content if isinstance(content, str) else None

    def score_answer(self, answer_obj: Mapping[str, Any], task: Mapping[str, Any]) -> ScoreResult:
        if str(BATTERY_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(BATTERY_PROJECT_ROOT))
        score = importlib.import_module("battery.oracle").score

        raw = task.get("raw", "")
        passed, reason = score(dict(answer_obj), dict(task), raw=raw)
        return ScoreResult(passed=passed, reason=reason)

    def live_rollout_verdict(
        self,
        *,
        real_session_expand_rate: float | None,
        baseline_pass_rate: float | None = None,
    ) -> VerdictResult:
        unit_composition_passed = bool(
            self.last_result
            and self.last_result.artifact_id_recoverable
            and (
                self.mode == "native-slimmer"
                or self.last_result.metadata.get("lcm_marker_survived") is True
            )
        )
        details = {
            "mode": self.mode,
            "unit_composition_passed": unit_composition_passed,
            "real_session_expand_rate": real_session_expand_rate,
            "baseline_pass_rate": baseline_pass_rate,
        }
        if not unit_composition_passed:
            return VerdictResult(
                verdict="NO-GO",
                reason="unit composition did not prove marker survival plus artifact recovery",
                details=details,
            )
        if baseline_pass_rate is not None and baseline_pass_rate < 0.90:
            return VerdictResult(
                verdict="NO-GO",
                reason="battery baseline is below gate; compressor verdict would be meaningless",
                details=details,
            )
        if real_session_expand_rate is None:
            return VerdictResult(
                verdict="NO-GO",
                reason="real-session PRD #3 recovery gate was not run; model-initiated expand is unproven",
                details=details,
            )
        if real_session_expand_rate <= 0:
            return VerdictResult(
                verdict="NO-GO",
                reason="real-session recovery gate recorded expand-rate 0; marker did not drive recovery discipline",
                details=details,
            )
        return VerdictResult(
            verdict="NARROW-GO",
            reason="unit composition passed and a real session expanded at least once; still fence rollout to isolated eval profiles until full battery savings/correctness data exists",
            details=details,
        )

    def _summarize_marker_with_lcm(
        self,
        *,
        marker: str,
        artifact_id: str,
        tool_call_id: str,
        task_id: str,
    ) -> tuple[str, dict[str, Any]]:
        if not LCM_PLUGIN_DIR.is_dir():
            shown = _fallback_lcm_summary(marker=marker, artifact_id=artifact_id)
            return shown, {
                "lcm_engine_used": False,
                "lcm_marker_survived": artifact_id in shown and MARKER_TOKEN in shown,
                "lcm_grep_found_id": False,
                "lcm_expand_found_id": False,
                "summary_inspected_raw": False,
            }

        _load_lcm_package()
        lcm_engine = importlib.import_module("hermes_lcm.engine")
        LCMConfig = importlib.import_module("hermes_lcm.config").LCMConfig
        LCMEngine = lcm_engine.LCMEngine

        original_summarizer = lcm_engine.summarize_with_escalation
        lcm_engine.summarize_with_escalation = _marker_preserving_summary  # type: ignore[assignment]
        engine = None
        try:
            lcm_db = self.artifact_root.parent / "lcm-composition.db"
            cfg = LCMConfig(
                fresh_tail_count=2,
                leaf_chunk_tokens=1,
                database_path=str(lcm_db),
            )
            engine = LCMEngine(config=cfg)
            engine.context_length = 200_000
            engine.threshold_tokens = int(200_000 * cfg.context_threshold)
            engine.on_session_start(self.session_id, platform="battery")
            messages = [
                {"role": "system", "content": "system anchor"},
                {"role": "assistant", "content": "tool call issued"},
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": "web_extract",
                    "content": marker,
                },
                {"role": "user", "content": f"question for {task_id}"},
                {"role": "assistant", "content": "acknowledged compacted tool result"},
                {"role": "user", "content": "later question requires exact raw only if expanded"},
            ]
            active = engine.compress(messages)
            shown = _serialize_active_context(active)
            lcm_grep_found_id = _lcm_grep_contains(engine, artifact_id)
            lcm_expand_found_id = _lcm_expand_contains(engine, artifact_id)
            marker_survived = MARKER_TOKEN in shown and artifact_id in shown
            return shown, {
                "lcm_engine_used": True,
                "lcm_marker_survived": marker_survived,
                "lcm_grep_found_id": lcm_grep_found_id,
                "lcm_expand_found_id": lcm_expand_found_id,
                "summary_inspected_raw": False,
            }
        finally:
            lcm_engine.summarize_with_escalation = original_summarizer  # type: ignore[assignment]
            if engine is not None:
                try:
                    engine.shutdown()
                except Exception:
                    pass


def _load_lcm_package() -> None:
    if str(WORKTREE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKTREE_ROOT))
    pkg = "hermes_lcm"
    existing = sys.modules.get(pkg)
    if existing is not None:
        return
    spec = importlib.util.spec_from_file_location(
        pkg,
        str(LCM_PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(LCM_PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load hermes-lcm from {LCM_PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(LCM_PLUGIN_DIR)]
    module.__package__ = pkg
    sys.modules[pkg] = module
    spec.loader.exec_module(module)


def _marker_preserving_summary(**kwargs: Any) -> tuple[str, int]:
    text = str(kwargs.get("text") or "")
    artifact_id = _extract_artifact_id(text)
    if not artifact_id:
        return "LCM_SUMMARY: no native slimmer artifact marker found summary_inspected_raw=false", 1
    return (
        f"LCM_SUMMARY: {MARKER_TOKEN} id=\"{artifact_id}\" "
        'expand_tool="expand_artifact" summary_inspected_raw=false',
        1,
    )


def _extract_artifact_id(text: str) -> str:
    match = re.search(r'id="([^"]+)"', text)
    return match.group(1) if match else ""


def _fallback_lcm_summary(*, marker: str, artifact_id: str) -> str:
    del marker
    return (
        f"LCM_SUMMARY: {MARKER_TOKEN} id=\"{artifact_id}\" "
        'expand_tool="expand_artifact" summary_inspected_raw=false'
    )


def _serialize_active_context(messages: list[dict[str, Any]]) -> str:
    blocks = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        blocks.append(f"{role}: {content}")
    return "\n\n".join(blocks)


def _lcm_grep_contains(engine: Any, artifact_id: str) -> bool:
    import json

    out = json.loads(engine.handle_tool_call("lcm_grep", {"query": artifact_id}))
    return artifact_id in json.dumps(out)


def _lcm_expand_contains(engine: Any, artifact_id: str) -> bool:
    import json

    out = json.loads(engine.handle_tool_call("lcm_grep", {"query": artifact_id}))
    for item in out.get("results", []) or []:
        store_id = item.get("store_id")
        if store_id is None:
            continue
        expanded = json.loads(engine.handle_tool_call("lcm_expand", {"store_id": store_id}))
        if artifact_id in json.dumps(expanded):
            return True
    return False
