from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Mapping

from ..store import raw_byte_len
from .base import CompressedView
from .registry import register_compressor

STRATEGY_NAME = "grep_cluster"
EVAL_RUN_ID = "prd5-phase2b-grep-cluster-adversarial-fixture"
THRESHOLD = "GO full=20/20 adversarial>=7/8 recoverability=1.00"
DEFAULT_MAX_REPRESENTATIVE_LINES = 3

_GREP_LINE_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:(?P<body>.*)$")
_VALUE_SEPARATOR_RE = re.compile(r"\s*(?:=|:|=>)\s*")
_VALUE_TOKEN_RE = re.compile(
    r"("  # value-like spans used only for fallback prefix grouping
    r"\"[^\"]*\""
    r"|'[^']*'"
    r"|\b0x[0-9A-Fa-f]+\b"
    r"|\b\d+(?:\.\d+)?\b"
    r")"
)


@dataclass(frozen=True)
class _ParsedLine:
    file_name: str
    line_number: str | None
    body: str
    prefix: str
    value: str


@dataclass
class _Group:
    file_name: str
    prefix: str
    entries: list[_ParsedLine] = field(default_factory=list)
    value_counts: Counter[str] = field(default_factory=Counter)

    def add(self, entry: _ParsedLine) -> None:
        self.entries.append(entry)
        self.value_counts[entry.value] += 1


class GrepClusterCompressor:
    strategy_name = STRATEGY_NAME

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView:
        max_representatives = _positive_int(
            params.get("max_representative_lines"),
            default=DEFAULT_MAX_REPRESENTATIVE_LINES,
        )
        entries = [_parse_line(line) for line in raw.splitlines() if line]
        groups: dict[tuple[str, str], _Group] = {}
        for entry in entries:
            key = (entry.file_name, entry.prefix)
            group = groups.get(key)
            if group is None:
                group = _Group(file_name=entry.file_name, prefix=entry.prefix)
                groups[key] = group
            group.add(entry)

        view_text = _render_groups(groups, total_matches=len(entries), max_representatives=max_representatives)
        return CompressedView(
            view_text=view_text,
            view_bytes=raw_byte_len(view_text),
            lossy_view=True,
            recoverable=True,
            strategy_name=STRATEGY_NAME,
        )


def register() -> None:
    register_compressor(
        tool_name="terminal",
        content_class="grep",
        compressor=GrepClusterCompressor(),
        eval_run_id=EVAL_RUN_ID,
        threshold=THRESHOLD,
        strategy_name=STRATEGY_NAME,
        params={"max_representative_lines": DEFAULT_MAX_REPRESENTATIVE_LINES},
    )


def _parse_line(line: str) -> _ParsedLine:
    match = _GREP_LINE_RE.match(line)
    if match is not None:
        file_name = match.group("file")
        line_number = match.group("line")
        body = match.group("body")
        value = body.strip()
        return _ParsedLine(
            file_name=file_name,
            line_number=line_number,
            body=body,
            prefix=_prefix_for_body(body),
            value=value or "<empty>",
        )

    file_name, body = _split_fileless_grep_line(line)
    value = body.strip()
    return _ParsedLine(
        file_name=file_name,
        line_number=None,
        body=body,
        prefix=_prefix_for_body(body),
        value=value or "<empty>",
    )


def _split_fileless_grep_line(line: str) -> tuple[str, str]:
    if ":" not in line:
        return "<unknown>", line
    file_name, body = line.split(":", 1)
    return file_name or "<unknown>", body


def _prefix_for_body(body: str) -> str:
    stripped = body.strip()
    if not stripped:
        return "<blank>"
    separator = _VALUE_SEPARATOR_RE.search(stripped)
    if separator is not None and separator.start() <= 80:
        token = stripped[: separator.end()].rstrip()
        if token.endswith("="):
            return token
        if token.endswith(":"):
            return token
        if token.endswith("=>"):
            return token
        return token.rstrip(" :=>")
    compact = _VALUE_TOKEN_RE.sub("…", stripped).strip()
    if compact and compact != stripped:
        return compact[:80].rstrip()
    parts = stripped.split(maxsplit=3)
    if len(parts) >= 3:
        return " ".join(parts[:2])
    return stripped[:80].rstrip()


def _render_groups(
    groups: dict[tuple[str, str], _Group],
    *,
    total_matches: int,
    max_representatives: int,
) -> str:
    lines = [f"grep_cluster: {total_matches} matches across {len(groups)} groups"]
    for _, group in sorted(groups.items(), key=lambda item: item[0]):
        lines.extend(
            [
                "",
                f"### {group.file_name} | prefix: {group.prefix}",
                f"matches: {len(group.entries)}",
                f"distinct_values: {len(group.value_counts)}",
                "representative_lines:",
            ]
        )
        for entry in group.entries[:max_representatives]:
            location = f"L{entry.line_number}" if entry.line_number is not None else "L?"
            lines.append(f"  - {location}: {entry.body.strip()}")
        if len(group.entries) > max_representatives:
            lines.append(f"  - ... {len(group.entries) - max_representatives} more lines in group")
        lines.append("distinct_match_values:")
        for value, count in sorted(group.value_counts.items(), key=lambda item: item[0]):
            lines.append(f"  - {value} ×{count}")
    return "\n".join(lines) + "\n"


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


register()
