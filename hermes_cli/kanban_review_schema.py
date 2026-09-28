"""Single source of truth for the review-coverage record.

``kanban_request_changes`` refuses a rework verdict unless the current review
run carries a ``review_coverage: {...}`` JSON comment covering every lens in
:data:`REQUIRED_REVIEW_LENSES`. The gate (``kanban_db._validate_review_coverage``),
the tool description and the worker prompt all read the lens list from here,
so revising the lens set is a one-line change to this module.
"""
from __future__ import annotations

# Lenses a rework verdict must cover. Each is ``done`` or
# ``n/a: <applicability reason>`` in the coverage record.
REQUIRED_REVIEW_LENSES: tuple[str, ...] = ("contract", "execution", "cross-vendor", "mutation")

# Coverage fields the gate requires besides ``lenses``.
REQUIRED_COVERAGE_FIELDS: tuple[str, ...] = ("findings", "items", "review_minutes", "batch_id", "head_sha")

# ``head_sha`` is the reviewed PR head (7-40 hex), or ``n/a: <reason>`` for a
# card with no PR. Card-sourced land requests take the card's LATEST record as
# the review of record and need an APPROVE carrying it; a reviewer approval
# (``kanban_complete`` from a review run) that names ``metadata.head_sha``
# writes that APPROVE record.
HEAD_SHA_PATTERN = r"[0-9a-fA-F]{7,40}"

# Coverage fields that may be omitted (or null). ``battery`` is optional: the
# per-card battery is being retired in favour of CI-owned suites. When
# present it must be a non-empty string (attachment name, ``seeded``, or
# ``n/a: <reason>``).
OPTIONAL_COVERAGE_FIELDS: tuple[str, ...] = ("battery",)


def lens_list_text() -> str:
    """Human-readable lens list, e.g. ``contract, execution and mutation``."""
    lenses = list(REQUIRED_REVIEW_LENSES)
    if len(lenses) == 1:
        return lenses[0]
    return ", ".join(lenses[:-1]) + " and " + lenses[-1]


def coverage_fields_text() -> str:
    """Field list for prompts/descriptions: required first, optional marked."""
    return ", ".join(
        ("lenses",) + REQUIRED_COVERAGE_FIELDS
        + tuple(f"optional {name}" for name in OPTIONAL_COVERAGE_FIELDS)
    )


# ---------------------------------------------------------------------------
# review_coverage v2 (Themis spec 2026-09-27 v7, sections 5.7, 5.8 and 6).
#
# v2 is ADDITIVE. A record enters v2 mode when it carries ``schema_version``,
# a ``reviewer`` object (v1 reviewers are plain strings) or any v2-only key.
# In v2 mode every present v2 field must match its closed alphabet. With
# ``schema_version: 2`` the key set is closed too (an unknown key is
# refused) and ``verdict`` must be one of VERDICTS. A Themis record
# (``reviewer.profile == "themis"`` or ``reviewer.kind == "stage"``) must also
# carry the THEMIS_REQUIRED_* set. v1 records (no v2 key) keep their v1 checks
# and nothing else: live v1 verdicts include ``approve``/``REQUEST_CHANGES``
# and ad-hoc keys (``note``, ``scope``), so the strict rules would break them.
#
# Pure stdlib, no kanban_db import: themis_state.py imports this module from
# the runtime tree (spec 5.7 "Schema" row) and refuses to run if it cannot.
# ---------------------------------------------------------------------------
import re as _re
from datetime import datetime as _datetime
from typing import Any as _Any, Optional as _Optional

SCHEMA_VERSION = 2

THEMIS_PROFILE = "themis"
THEMIS_AUTHOR = "themis"
# P1-P2 board prefix. It is never ``review_coverage:``, so no v1 consumer
# (human_review, land_request, review_honesty) reads a shadow record.
THEMIS_BOARD_PREFIX = "themis:"
THEMIS_STAGE_KIND = "stage"

VERDICTS: tuple[str, ...] = ("APPROVE", "CHANGES_REQUESTED", "NO-VERDICT")
NO_VERDICT = "NO-VERDICT"
# Section 6 enum, plus the aliases the card body lists (t_1296b54f). One closed set.
NO_VERDICT_REASONS: tuple[str, ...] = (
    "oversize", "head-moved", "ci-not-green", "unparseable", "unanchored",
    "merged-or-closed", "cap", "round-cap", "lane-off", "timeout",
    "transport-off", "multi-card", "parse", "budget",
)
# ``packet-incomplete:<n>`` takes a packet field NUMBER 1-7, never a name.
NO_VERDICT_PACKET_INCOMPLETE_RE = r"packet-incomplete:[1-7]"
THEMIS_TRIGGERS: tuple[str, ...] = ("green", "handback", "sweep")
THEMIS_BINDINGS: tuple[str, ...] = ("head", "card")
THEMIS_HEAD_ORIGINS: tuple[str, ...] = ("update-branch", "push", "unknown")
THEMIS_BOARD_STATES: tuple[str, ...] = ("posted", "comment-refused")
# Section 5.8/6 values plus the card-body short forms.
THEMIS_TRANSPORTS: tuple[str, ...] = (
    "cpr-cli-slim", "cpr-slim", "apx", "bpr", "apx-messages", "bpr-messages",
)
THEMIS_TIERS: tuple[int, ...] = (0, 1)
REVIEWER_KINDS: tuple[str, ...] = ("stage", "human", "profile")
FINDING_SEVERITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
FINDING_ACTIONS: tuple[str, ...] = ("auto-fix", "ask-user", "no-op")
ACCEPTANCE_STATUSES: tuple[str, ...] = ("met", "unmet", "not-checkable")
ACCEPTANCE_NONE = "n/a: card has no ACs"

HEAD_SHA40_PATTERN = r"[0-9a-f]{40}"
THEMIS_RECORD_PATTERN = r"state/themis/records/[0-9a-f]{32}\.json"
THEMIS_ITEM_PATTERN = r"F[1-9][0-9]{0,3} P[0-3]"
THEMIS_CARD_ID_PATTERN = r"t_[0-9a-f]{8}"
ISO8601_UTC_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)"
PROMPT_VERSION_PATTERN = r"[0-9a-f]{12}"
INPUT_SHA256_PATTERN = r"[0-9a-f]{64}"
MODEL_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}"
FAMILY_PATTERN = r"[a-z0-9][a-z0-9-]{0,31}"

# Keys a v1 record may carry: the v1 contract plus the de-facto keys.
V1_FIELDS: tuple[str, ...] = (
    ("lenses",) + REQUIRED_COVERAGE_FIELDS + OPTIONAL_COVERAGE_FIELDS + ("verdict", "by")
)
# v2 keys allowed on the board (section 5.8 closed alphabet).
V2_BOARD_FIELDS: tuple[str, ...] = (
    "schema_version", "no_verdict_reason", "base_sha", "ci_run", "pr",
    "anchored", "witness_only", "same_family_approve", "would_hold", "late",
    "multi_card", "trigger", "binding", "reviewer", "event_ts", "written_at",
    "record",
)
# v2 keys on the FILE record only (section 5.8 "Not on the board, ever").
V2_FILE_ONLY_FIELDS: tuple[str, ...] = (
    "acceptance", "finding_records", "sensitive_hits", "eligible_at",
    "last_send_back_at", "head_origin", "handback_run_id", "claim_stolen",
    "multi_card_cards", "board", "author_family",
)
# Keys that switch a record into v2 mode. ``pr``, ``verdict``, ``by`` and a
# string ``reviewer`` appear on live v1 records, so they do not.
V2_ONLY_FIELDS: tuple[str, ...] = tuple(
    k for k in V2_BOARD_FIELDS + V2_FILE_ONLY_FIELDS if k not in ("pr", "reviewer")
)
REVIEWER_FIELDS: tuple[str, ...] = (
    "kind", "profile", "model", "family", "transport", "tier",
    "prompt_version", "input_sha256", "latency_s",
)
THEMIS_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version", "verdict", "head_sha", "trigger", "binding", "anchored",
    "witness_only", "reviewer", "by",
)
THEMIS_REQUIRED_REVIEWER_FIELDS: tuple[str, ...] = (
    "profile", "model", "family", "transport", "tier", "prompt_version", "input_sha256",
)
SURFACES: tuple[str, ...] = ("board", "file")

_BOOL_FIELDS = ("anchored", "witness_only", "same_family_approve", "would_hold",
                "late", "multi_card", "claim_stolen")
_TS_FIELDS = ("event_ts", "written_at", "eligible_at")


def _full(pattern: str, value: _Any) -> bool:
    return isinstance(value, str) and _re.fullmatch(pattern, value) is not None


def _is_int(value: _Any) -> bool:
    return type(value) is int


def _ts_ok(value: _Any) -> bool:
    if not _full(ISO8601_UTC_PATTERN, value):
        return False
    try:
        _datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def no_verdict_reason_ok(value: _Any) -> bool:
    """True iff ``value`` is in the closed NO-VERDICT reason alphabet."""
    return (isinstance(value, str)
            and (value in NO_VERDICT_REASONS or _full(NO_VERDICT_PACKET_INCOMPLETE_RE, value)))


def is_v2_record(record: _Any) -> bool:
    """True when a coverage record opts into the v2 rules."""
    if not isinstance(record, dict):
        return False
    return (isinstance(record.get("reviewer"), dict)
            or any(key in record for key in V2_ONLY_FIELDS))


def is_themis_record(record: _Any) -> bool:
    """A stage/Themis record: every THEMIS_REQUIRED_* field is mandatory."""
    reviewer = record.get("reviewer") if isinstance(record, dict) else None
    return isinstance(reviewer, dict) and (
        reviewer.get("profile") == THEMIS_PROFILE or reviewer.get("kind") == THEMIS_STAGE_KIND
    )


def _validate_reviewer(reviewer: _Any, themis: bool) -> _Optional[str]:
    if not isinstance(reviewer, dict):
        return "reviewer must be an object in a v2 record"
    unknown = sorted(k for k in reviewer if k not in REVIEWER_FIELDS)
    if unknown:
        return f"reviewer has unknown key(s): {', '.join(map(str, unknown))}"
    if themis:
        for key in THEMIS_REQUIRED_REVIEWER_FIELDS:
            if key not in reviewer:
                return f"reviewer.{key} is required on a themis record"
    checks = (
        ("kind", lambda v: v in REVIEWER_KINDS, f"one of {', '.join(REVIEWER_KINDS)}"),
        ("profile", lambda v: _full(FAMILY_PATTERN, v), "a profile name"),
        ("model", lambda v: _full(MODEL_ID_PATTERN, v), "a model id"),
        ("family", lambda v: _full(FAMILY_PATTERN, v), "a lowercase family token"),
        ("transport", lambda v: v in THEMIS_TRANSPORTS, f"one of {', '.join(THEMIS_TRANSPORTS)}"),
        ("tier", lambda v: _is_int(v) and v in THEMIS_TIERS, "0 or 1"),
        ("prompt_version", lambda v: _full(PROMPT_VERSION_PATTERN, v), "12 lowercase hex"),
        ("input_sha256", lambda v: _full(INPUT_SHA256_PATTERN, v), "64 lowercase hex"),
        ("latency_s", lambda v: type(v) in (int, float) and v >= 0, "a non-negative number"),
    )
    for key, ok, want in checks:
        if key in reviewer and not ok(reviewer[key]):
            return f"reviewer.{key} must be {want}"
    return None


def _validate_acceptance(value: _Any) -> _Optional[str]:
    if value == ACCEPTANCE_NONE:
        return None
    if not isinstance(value, list) or not value:
        return f"acceptance must be a non-empty list or '{ACCEPTANCE_NONE}'"
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"criterion", "status", "pointer"}
                or row.get("status") not in ACCEPTANCE_STATUSES
                or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("criterion", "pointer"))):
            return "acceptance rows must be {criterion, status: met|unmet|not-checkable, pointer}"
    return None


_FINDING_KEYS = {"id", "severity", "lens", "file", "line", "claim", "evidence", "action"}


def _validate_finding_records(value: _Any) -> _Optional[str]:
    if not isinstance(value, list):
        return "finding_records must be a list"
    for row in value:
        if (not isinstance(row, dict) or set(row) != _FINDING_KEYS
                or not _full(r"F[1-9][0-9]{0,3}", row.get("id"))
                or row.get("severity") not in FINDING_SEVERITIES
                or row.get("action") not in FINDING_ACTIONS
                or not _is_int(row.get("line")) or row["line"] < 1
                or not all(isinstance(row.get(k), str) and row[k].strip()
                           for k in ("lens", "file", "claim", "evidence"))):
            return ("finding_records rows must be {id: F<n>, severity: P0-P3, lens, file, "
                    "line >= 1, claim, evidence, action: auto-fix|ask-user|no-op}")
    return None


def validate_v2_fields(record: _Any, surface: str = "board") -> _Optional[str]:
    """Check the v2 part of a review_coverage record; None when valid.

    ``surface`` is ``board`` (a card comment, the section 5.8 closed alphabet;
    file-only keys are refused) or ``file`` (the Themis file record). The v1
    checks (lenses, findings/items, review_minutes, batch_id) stay with the
    caller. A record with no v2 key returns None untouched.
    """
    if surface not in SURFACES:
        raise ValueError(f"surface must be one of {SURFACES}")
    if not isinstance(record, dict):
        return "review_coverage must be a JSON object"
    if not is_v2_record(record):
        return None
    themis = is_themis_record(record)
    versioned = "schema_version" in record
    if versioned or themis:
        version = record.get("schema_version")
        if not _is_int(version) or version != SCHEMA_VERSION:
            return f"schema_version must be the integer {SCHEMA_VERSION}"
        allowed = set(V1_FIELDS) | set(V2_BOARD_FIELDS)
        if surface == "file":
            allowed |= set(V2_FILE_ONLY_FIELDS)
        unknown = sorted(str(k) for k in record if k not in allowed)
        if unknown:
            where = "board" if surface == "board" else "v2"
            return f"unknown key(s) in {where} review_coverage record: {', '.join(unknown)}"
    elif surface == "board":
        leaked = sorted(k for k in record if k in V2_FILE_ONLY_FIELDS)
        if leaked:
            return f"file-record-only key(s) on the board: {', '.join(leaked)}"
    if themis:
        for key in THEMIS_REQUIRED_FIELDS:
            if key not in record:
                return f"{key} is required on a themis record"
        if record.get("by") != THEMIS_AUTHOR:
            return f"by must be '{THEMIS_AUTHOR}' on a themis record"
        if not _full(HEAD_SHA40_PATTERN, record.get("head_sha")):
            return "head_sha must be 40 lowercase hex on a themis record"
    verdict = record.get("verdict")
    if (versioned or themis) and "verdict" in record and verdict not in VERDICTS:
        return f"verdict must be one of {', '.join(VERDICTS)}"
    if verdict == NO_VERDICT or "no_verdict_reason" in record:
        if verdict == NO_VERDICT and "no_verdict_reason" not in record:
            return "no_verdict_reason is required when verdict is NO-VERDICT"
        if verdict != NO_VERDICT:
            return "no_verdict_reason is allowed only when verdict is NO-VERDICT"
    if "no_verdict_reason" in record and not no_verdict_reason_ok(record["no_verdict_reason"]):
        return ("no_verdict_reason must be one of " + ", ".join(NO_VERDICT_REASONS)
                + " or packet-incomplete:<1-7>")
    if themis and verdict == "APPROVE" and "ci_run" not in record:
        return "ci_run is required when a themis record approves"
    if "reviewer" in record:
        error = _validate_reviewer(record["reviewer"], themis)
        if error:
            return error
    for key in _BOOL_FIELDS:
        if key in record and not isinstance(record[key], bool):
            return f"{key} must be true or false"
    for key in _TS_FIELDS:
        if key in record and not _ts_ok(record[key]):
            return f"{key} must be ISO-8601 UTC"
    if "last_send_back_at" in record and record["last_send_back_at"] is not None \
            and not _ts_ok(record["last_send_back_at"]):
        return "last_send_back_at must be ISO-8601 UTC or null"
    enums = (("trigger", THEMIS_TRIGGERS), ("binding", THEMIS_BINDINGS),
             ("head_origin", THEMIS_HEAD_ORIGINS), ("board", THEMIS_BOARD_STATES))
    for key, values in enums:
        if key in record and record[key] not in values:
            return f"{key} must be one of {', '.join(values)}"
    if "base_sha" in record and not _full(HEAD_SHA40_PATTERN, record["base_sha"]):
        return "base_sha must be 40 lowercase hex"
    if "ci_run" in record and not _full(r"[1-9][0-9]{0,19}", record["ci_run"]):
        return "ci_run must be a string of digits"
    if (versioned or themis) and "pr" in record and not (_is_int(record["pr"]) and record["pr"] > 0):
        return "pr must be a positive integer"
    if "handback_run_id" in record and not (_is_int(record["handback_run_id"]) and record["handback_run_id"] > 0):
        return "handback_run_id must be a positive integer"
    if "record" in record and not _full(THEMIS_RECORD_PATTERN, record["record"]):
        return "record must match state/themis/records/<32 hex>.json"
    if "multi_card_cards" in record and not (
            isinstance(record["multi_card_cards"], list)
            and all(_full(THEMIS_CARD_ID_PATTERN, c) for c in record["multi_card_cards"])):
        return "multi_card_cards must be a list of card ids"
    if "sensitive_hits" in record and not (
            isinstance(record["sensitive_hits"], list)
            and all(isinstance(p, str) and p.strip() for p in record["sensitive_hits"])):
        return "sensitive_hits must be a list of paths"
    if "author_family" in record and not _full(FAMILY_PATTERN, record["author_family"]):
        return "author_family must be a lowercase family token"
    if "acceptance" in record:
        error = _validate_acceptance(record["acceptance"])
        if error:
            return error
    if "finding_records" in record:
        error = _validate_finding_records(record["finding_records"])
        if error:
            return error
    if themis and surface == "board" and "items" in record and not (
            isinstance(record["items"], list)
            and all(_full(THEMIS_ITEM_PATTERN, i) for i in record["items"])):
        return "themis board items must be finding ids like 'F1 P1' (no titles, no paths)"
    return None
