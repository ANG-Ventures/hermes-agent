from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


DEFAULT_ALLOW_TOOLS = frozenset({"terminal", "web_extract", "browser_console"})
DEFAULT_DENY_TOOLS = frozenset({
    "discord_admin",
    "ha_call_service",
    "memory",
    "mem0_conclude",
    "send_message",
})
DEFAULT_DENY_ON_STATUS = frozenset({"error"})
DEFAULT_MIN_BYTES = 12_000
DEFAULT_PREVIEW_BYTES = 2_500


@dataclass(frozen=True)
class Classification:
    eligible: bool
    reason: str
    raw_bytes: int
    content_class: str
    preview: str | None = None
    secret_match: str | None = None


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("onepassword", re.compile(r"op://", re.IGNORECASE)),
    ("bearer", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("aws", re.compile(r"\b(?:A3T|AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("pem", re.compile(r"-{5}BEGIN [A-Z0-9 ]*PRIVATE KEY-{5}", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\b")),
    ("cookie", re.compile(r"\b(?:set-cookie|cookie)\s*:", re.IGNORECASE)),
    (
        "dsn",
        re.compile(r"\b[a-z][a-z0-9+.-]{1,31}://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    ),
)


def contains_secret(text: str) -> str | None:
    """Return the first configured no-store secret class present in text."""

    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def classify_tool_result(
    *,
    tool_name: str,
    result: str,
    status: str | None = "success",
    min_bytes: int = DEFAULT_MIN_BYTES,
    preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    allow_tools: Iterable[str] | None = DEFAULT_ALLOW_TOOLS,
    deny_tools: Iterable[str] | None = DEFAULT_DENY_TOOLS,
    deny_on_status: Iterable[str] | None = DEFAULT_DENY_ON_STATUS,
) -> Classification:
    """Classify whether a tool result may be losslessly offloaded.

    Secret-looking content is always no-store/pass-through in v0.1, before any
    size/tool/status eligibility check.
    """

    raw_bytes = len(result.encode("utf-8"))
    secret_match = contains_secret(result)
    if secret_match is not None:
        return Classification(
            eligible=False,
            reason="secret_classified_no_store",
            raw_bytes=raw_bytes,
            content_class="secret",
            preview=None,
            secret_match=secret_match,
        )

    denied_tools = set(deny_tools or ())
    if tool_name in denied_tools:
        return Classification(False, "tool_denied", raw_bytes, _content_class(result))

    denied_statuses = {value.lower() for value in (deny_on_status or ())}
    if status is not None and status.lower() in denied_statuses:
        return Classification(False, "status_denied", raw_bytes, _content_class(result))

    allowed_tools = None if allow_tools is None else set(allow_tools)
    if allowed_tools is not None and tool_name not in allowed_tools:
        return Classification(False, "tool_not_allowed", raw_bytes, _content_class(result))

    if raw_bytes < min_bytes:
        return Classification(False, "below_min_bytes", raw_bytes, _content_class(result))

    return Classification(
        eligible=True,
        reason="eligible_lossless_offload",
        raw_bytes=raw_bytes,
        content_class=_content_class(result),
        preview=deterministic_preview(result, preview_bytes=preview_bytes),
    )


def deterministic_preview(text: str, *, preview_bytes: int = DEFAULT_PREVIEW_BYTES) -> str:
    """Return a deterministic head/tail preview with an explicit omission note."""

    raw = text.encode("utf-8")
    if preview_bytes <= 0 or len(raw) <= preview_bytes:
        return text

    head_bytes = max(1, int(preview_bytes * 0.4))
    tail_bytes = max(1, preview_bytes - head_bytes)
    if head_bytes + tail_bytes >= len(raw):
        return text

    omitted = len(raw) - head_bytes - tail_bytes
    head = _decode_utf8_prefix(raw, head_bytes)
    tail = _decode_utf8_suffix(raw, tail_bytes)
    return f"{head}\n\n[... omitted {omitted} bytes ...]\n\n{tail}"


def _decode_utf8_prefix(raw: bytes, byte_count: int) -> str:
    return raw[:byte_count].decode("utf-8", errors="ignore")


def _decode_utf8_suffix(raw: bytes, byte_count: int) -> str:
    return raw[-byte_count:].decode("utf-8", errors="ignore")


def _content_class(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    return "text"
