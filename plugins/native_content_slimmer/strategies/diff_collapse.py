from __future__ import annotations

from collections.abc import Mapping

from .base import CompressedView
from .registry import StrategySelection, register_compressor

STRATEGY_NAME = "diff_collapse"
DEFAULT_CONTEXT_LINES = 2
DEFAULT_MIN_COLLAPSE_LINES = 8
DEFAULT_PARAMS: dict[str, int] = {
    "context_lines": DEFAULT_CONTEXT_LINES,
    "min_collapse_lines": DEFAULT_MIN_COLLAPSE_LINES,
}
EVAL_RUN_ID = "prd5-phase2a-diff-collapse-adversarial-fixture"
THRESHOLD = "GO"
_COLLAPSE_TEMPLATE = "«{count} unchanged lines»"


class DiffCollapseCompressor:
    """Collapse large unchanged regions in git diffs without dropping changes."""

    strategy_name = STRATEGY_NAME

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView:
        context_lines = _nonnegative_int(params, "context_lines", DEFAULT_CONTEXT_LINES)
        min_collapse_lines = max(1, _nonnegative_int(params, "min_collapse_lines", DEFAULT_MIN_COLLAPSE_LINES))
        view_text = _collapse_diff(raw, context_lines=context_lines, min_collapse_lines=min_collapse_lines)
        return CompressedView(
            view_text=view_text,
            view_bytes=len(view_text.encode("utf-8")),
            lossy_view=True,
            recoverable=True,
            strategy_name=STRATEGY_NAME,
        )


COMPRESSOR = DiffCollapseCompressor()


def register() -> StrategySelection:
    """Register the eval-passed terminal/diff lane for this strategy."""

    return register_compressor(
        tool_name="terminal",
        content_class="diff",
        compressor=COMPRESSOR,
        eval_run_id=EVAL_RUN_ID,
        threshold=THRESHOLD,
        strategy_name=STRATEGY_NAME,
        params=DEFAULT_PARAMS,
    )


def _collapse_diff(raw: str, *, context_lines: int, min_collapse_lines: int) -> str:
    lines = raw.splitlines()
    trailing_newline = raw.endswith("\n")
    collapsed: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _is_hunk_header(line):
            collapsed.append(line)
            idx += 1
            body: list[str] = []
            while idx < len(lines):
                candidate = lines[idx]
                if _is_hunk_header(candidate) or candidate.startswith("diff --git "):
                    break
                body.append(candidate)
                idx += 1
            collapsed.extend(
                _collapse_hunk_body(
                    body,
                    context_lines=context_lines,
                    min_collapse_lines=min_collapse_lines,
                )
            )
            continue
        collapsed.append(line)
        idx += 1
    view = "\n".join(collapsed)
    if trailing_newline:
        return view + "\n"
    return view


def _collapse_hunk_body(body: list[str], *, context_lines: int, min_collapse_lines: int) -> list[str]:
    if not body:
        return []
    changed_indices = {idx for idx, line in enumerate(body) if _is_changed_line(line)}
    keep_indices = {idx for idx, line in enumerate(body) if _is_forced_keep_line(line)}
    for changed_idx in changed_indices:
        start = max(0, changed_idx - context_lines)
        end = min(len(body), changed_idx + context_lines + 1)
        keep_indices.update(range(start, end))

    # A tiny hidden gap is more legible kept verbatim than replaced with a noisy
    # collapse marker. Large runs still collapse to the explicit count marker.
    idx = 0
    while idx < len(body):
        if idx in keep_indices or not _is_unchanged_context_line(body[idx]):
            idx += 1
            continue
        start = idx
        while idx < len(body) and idx not in keep_indices and _is_unchanged_context_line(body[idx]):
            idx += 1
        if idx - start < min_collapse_lines:
            keep_indices.update(range(start, idx))

    out: list[str] = []
    idx = 0
    while idx < len(body):
        if idx in keep_indices or not _is_unchanged_context_line(body[idx]):
            out.append(body[idx])
            idx += 1
            continue
        start = idx
        while idx < len(body) and idx not in keep_indices and _is_unchanged_context_line(body[idx]):
            idx += 1
        out.append(_collapse_marker(idx - start))
    return out


def _collapse_marker(count: int) -> str:
    return _COLLAPSE_TEMPLATE.format(count=max(0, int(count)))


def _is_hunk_header(line: str) -> bool:
    return line.startswith("@@") and "@@" in line[2:]


def _is_changed_line(line: str) -> bool:
    return line.startswith("+") or line.startswith("-")


def _is_unchanged_context_line(line: str) -> bool:
    return line == "" or line.startswith(" ")


def _is_forced_keep_line(line: str) -> bool:
    return line.startswith("\\") or (not _is_changed_line(line) and not _is_unchanged_context_line(line))


def _nonnegative_int(params: Mapping[str, object], key: str, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return default


register()
