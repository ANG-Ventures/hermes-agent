from __future__ import annotations

import re
from collections.abc import Mapping

from . import registry
from .base import CompressedView

STRATEGY_NAME = "log_dedup"
EVAL_RUN_ID = "phase1b-log-dedup-adversarial-v1"
THRESHOLD = "GO"
DEFAULT_MIN_RUN_LINES = 4
DEFAULT_MAX_INPUT_BYTES = 1_000_000
DEFAULT_MAX_LINES = 20_000
DEFAULT_CONTROL_SAMPLE_CHARS = 4096
DEFAULT_MAX_CONTROL_RATIO = 0.10

_SEVERITY_RE = re.compile(r"\b(?:FATAL|ERROR|CRITICAL|panic|PANIC|Traceback)\b")
_ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
_SPACE_RE = re.compile(r"\s+")
_CONTROL_CHARS = frozenset(chr(value) for value in range(32)) - {"\n", "\r", "\t"}


class LogDedupCompressor:
    strategy_name = STRATEGY_NAME

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView | None:
        text = raw if isinstance(raw, str) else str(raw)
        if _should_fail_open(text, params):
            return None

        lines = text.splitlines()
        if len(lines) < _int_param(params, "min_run_lines", DEFAULT_MIN_RUN_LINES):
            return None

        min_run_lines = max(2, _int_param(params, "min_run_lines", DEFAULT_MIN_RUN_LINES))
        out: list[str] = []
        run: list[str] = []
        run_sig = ""

        def flush_run() -> None:
            nonlocal run, run_sig
            if not run:
                return
            out.extend(_render_run(run, min_run_lines=min_run_lines))
            run = []
            run_sig = ""

        for line in lines:
            if _is_must_keep(line):
                flush_run()
                out.append(line)
                continue
            sig = _signature(line)
            if run and sig == run_sig:
                run.append(line)
                continue
            flush_run()
            run = [line]
            run_sig = sig
        flush_run()

        view_text = "\n".join(out)
        if text.endswith("\n") and view_text:
            view_text += "\n"
        if _byte_len(view_text) >= _byte_len(text):
            return None
        return CompressedView(
            view_text=view_text,
            view_bytes=_byte_len(view_text),
            lossy_view=True,
            recoverable=True,
            strategy_name=STRATEGY_NAME,
        )


def register_default_lanes() -> tuple[registry.StrategySelection, ...]:
    compressor = LogDedupCompressor()
    return tuple(
        registry.register_compressor(
            tool_name=tool_name,
            content_class="log",
            compressor=compressor,
            eval_run_id=EVAL_RUN_ID,
            threshold=THRESHOLD,
            strategy_name=STRATEGY_NAME,
        )
        for tool_name in ("terminal", "web_extract")
    )


def _render_run(lines: list[str], *, min_run_lines: int) -> list[str]:
    if len(lines) < min_run_lines:
        return list(lines)
    if len(lines) == 1:
        return list(lines)
    marker = f"«{lines[0]}» ×{len(lines)}"
    if lines[0] == lines[-1]:
        return [lines[0], marker]
    return [lines[0], marker, lines[-1]]


def _signature(line: str) -> str:
    normalized = _ISO_TS_RE.sub("<ts>", line)
    normalized = _TIME_RE.sub("<time>", normalized)
    normalized = _UUID_RE.sub("<uuid>", normalized)
    normalized = _HEX_RE.sub("<hex>", normalized)
    normalized = _NUMBER_RE.sub("<n>", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _is_must_keep(line: str) -> bool:
    return bool(_SEVERITY_RE.search(line))


def _should_fail_open(text: str, params: Mapping[str, object]) -> bool:
    max_input_bytes = _int_param(params, "max_input_bytes", DEFAULT_MAX_INPUT_BYTES)
    if _byte_len(text) > max_input_bytes:
        return True
    max_lines = _int_param(params, "max_lines", DEFAULT_MAX_LINES)
    if text.count("\n") + 1 > max_lines:
        return True
    sample_chars = _int_param(params, "control_sample_chars", DEFAULT_CONTROL_SAMPLE_CHARS)
    sample = text[: max(0, sample_chars)]
    if sample:
        control_count = sum(1 for char in sample if char in _CONTROL_CHARS)
        max_ratio = _float_param(params, "max_control_ratio", DEFAULT_MAX_CONTROL_RATIO)
        if control_count / len(sample) > max_ratio:
            return True
    return False


def _int_param(params: Mapping[str, object], key: str, default: int) -> int:
    value = params.get(key, default)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float_param(params: Mapping[str, object], key: str, default: float) -> float:
    value = params.get(key, default)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _lossless_view(text: str) -> CompressedView:
    return CompressedView(
        view_text=text,
        view_bytes=_byte_len(text),
        lossy_view=False,
        recoverable=True,
        strategy_name=STRATEGY_NAME,
    )


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


register_default_lanes()
