from __future__ import annotations

import hmac
import shlex
from dataclasses import dataclass
from hashlib import sha256


MARKER_TOKEN = "HERMES_ARTIFACT_COMPACTED"
_PREVIEW_START = "--- PREVIEW START ---"
_PREVIEW_END = "--- PREVIEW END ---"


@dataclass(frozen=True)
class MarkerEntry:
    session_id: str
    tool_call_id: str
    raw_sha256: str
    artifact_id: str
    original_bytes: int
    signature: str
    marker: str


@dataclass(frozen=True)
class ParsedMarker:
    fields: dict[str, str]
    preview: str


@dataclass(frozen=True)
class MarkerVerification:
    ok: bool
    reason: str
    entry: MarkerEntry | None = None
    parsed: ParsedMarker | None = None


class MarkerLedger:
    """Out-of-band auth ledger keyed by the exact slimmed result identity."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], MarkerEntry] = {}

    def record(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        raw_sha256: str,
        artifact_id: str,
        original_bytes: int,
        signature: str,
        marker: str,
    ) -> MarkerEntry:
        entry = MarkerEntry(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_sha256=raw_sha256,
            artifact_id=artifact_id,
            original_bytes=original_bytes,
            signature=signature,
            marker=marker,
        )
        self._entries[(session_id, tool_call_id, raw_sha256)] = entry
        return entry

    def lookup(self, *, session_id: str, tool_call_id: str, raw_sha256: str) -> MarkerEntry | None:
        return self._entries.get((session_id, tool_call_id, raw_sha256))


def make_marker_signature(
    *,
    session_id: str,
    tool_call_id: str,
    raw_sha256: str,
    artifact_id: str,
    original_bytes: int,
    secret: bytes | str,
) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    payload = "\n".join([session_id, tool_call_id, raw_sha256, artifact_id, str(original_bytes)])
    return hmac.new(key, payload.encode("utf-8"), sha256).hexdigest()


def build_authenticated_marker(
    *,
    session_id: str,
    tool_call_id: str,
    artifact_id: str,
    tool_name: str,
    raw_sha256: str,
    original_bytes: int,
    shown_bytes: int,
    omitted_bytes: int,
    preview: str,
    secret: bytes | str,
    ledger: MarkerLedger,
) -> str:
    signature = make_marker_signature(
        session_id=session_id,
        tool_call_id=tool_call_id,
        raw_sha256=raw_sha256,
        artifact_id=artifact_id,
        original_bytes=original_bytes,
        secret=secret,
    )
    marker = _render_marker(
        session_id=session_id,
        tool_call_id=tool_call_id,
        artifact_id=artifact_id,
        tool_name=tool_name,
        raw_sha256=raw_sha256,
        original_bytes=original_bytes,
        shown_bytes=shown_bytes,
        omitted_bytes=omitted_bytes,
        signature=signature,
        preview=preview,
    )
    ledger.record(
        session_id=session_id,
        tool_call_id=tool_call_id,
        raw_sha256=raw_sha256,
        artifact_id=artifact_id,
        original_bytes=original_bytes,
        signature=signature,
        marker=marker,
    )
    return marker


def parse_marker(text: str) -> ParsedMarker | None:
    stripped = text.strip()
    if not stripped.startswith(f"[{MARKER_TOKEN} ") or not stripped.endswith(f"[/{MARKER_TOKEN}]"):
        return None

    lines = stripped.splitlines()
    if not lines or not lines[0].endswith("]"):
        return None

    try:
        tokens = shlex.split(lines[0][1:-1])
    except ValueError:
        return None
    if not tokens or tokens[0] != MARKER_TOKEN:
        return None

    fields: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            return None
        key, value = token.split("=", 1)
        fields[key] = value

    preview = _extract_preview(stripped)
    if preview is None:
        return None
    return ParsedMarker(fields=fields, preview=preview)


def verify_marker_auth(text: str, *, secret: bytes | str, ledger: MarkerLedger) -> MarkerVerification:
    parsed = parse_marker(text)
    if parsed is None:
        return MarkerVerification(False, "not_a_marker")

    required = {"session_id", "tool_call_id", "raw_sha256", "id", "original_bytes", "sig"}
    if not required.issubset(parsed.fields):
        return MarkerVerification(False, "malformed_marker", parsed=parsed)

    try:
        original_bytes = int(parsed.fields["original_bytes"])
    except ValueError:
        return MarkerVerification(False, "malformed_marker", parsed=parsed)

    expected_sig = make_marker_signature(
        session_id=parsed.fields["session_id"],
        tool_call_id=parsed.fields["tool_call_id"],
        raw_sha256=parsed.fields["raw_sha256"],
        artifact_id=parsed.fields["id"],
        original_bytes=original_bytes,
        secret=secret,
    )
    if not hmac.compare_digest(expected_sig, parsed.fields["sig"]):
        return MarkerVerification(False, "bad_hmac", parsed=parsed)

    entry = ledger.lookup(
        session_id=parsed.fields["session_id"],
        tool_call_id=parsed.fields["tool_call_id"],
        raw_sha256=parsed.fields["raw_sha256"],
    )
    if entry is None:
        return MarkerVerification(False, "missing_ledger", parsed=parsed)

    if (
        entry.artifact_id != parsed.fields["id"]
        or entry.original_bytes != original_bytes
        or not hmac.compare_digest(entry.signature, parsed.fields["sig"])
    ):
        return MarkerVerification(False, "ledger_mismatch", entry=entry, parsed=parsed)

    return MarkerVerification(True, "ok", entry=entry, parsed=parsed)


def _render_marker(
    *,
    session_id: str,
    tool_call_id: str,
    artifact_id: str,
    tool_name: str,
    raw_sha256: str,
    original_bytes: int,
    shown_bytes: int,
    omitted_bytes: int,
    signature: str,
    preview: str,
) -> str:
    fields = [
        "lossy=false",
        f'id="{_escape_marker_value(artifact_id)}"',
        f'tool="{_escape_marker_value(tool_name)}"',
        f"original_bytes={original_bytes}",
        f"shown_bytes={shown_bytes}",
        f"omitted_bytes={omitted_bytes}",
        f'raw_sha256="{_escape_marker_value(raw_sha256)}"',
        f'tool_call_id="{_escape_marker_value(tool_call_id)}"',
        f'session_id="{_escape_marker_value(session_id)}"',
        f'sig="{_escape_marker_value(signature)}"',
        'expand_tool="expand_artifact"',
    ]
    header = f"[{MARKER_TOKEN} {' '.join(fields)}]"
    return "\n".join(
        [
            header,
            (
                "This is a preview, not the full tool result. If exact content, omitted lines, "
                "tail assertions, JSON fields,"
            ),
            f"or error details matter, call expand_artifact({{'id':'{artifact_id}'}}).",
            _PREVIEW_START,
            preview,
            _PREVIEW_END,
            f"[/{MARKER_TOKEN}]",
        ]
    )


def _escape_marker_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _extract_preview(text: str) -> str | None:
    try:
        start = text.index(_PREVIEW_START) + len(_PREVIEW_START)
        end = text.index(_PREVIEW_END, start)
    except ValueError:
        return None
    return text[start:end].strip("\n")
