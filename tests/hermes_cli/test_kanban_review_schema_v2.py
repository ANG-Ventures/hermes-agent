"""review_coverage v2 (Themis spec 2026-09-27 v7, sections 5.7, 5.8, 6).

v2 is additive: a v1 record validates exactly as before. A Themis/stage record
must carry the closed-alphabet v2 set, and in versioned mode no unknown key
passes.
"""
import copy
import json
import random
import sqlite3
import string
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_schema as s

HEAD = "0123456789abcdef0123456789abcdef01234567"
BASE = "89abcdef0123456789abcdef0123456789abcdef"


def themis_board(**overrides):
    rec = {
        "schema_version": 2, "verdict": "CHANGES_REQUESTED", "head_sha": HEAD,
        "base_sha": BASE, "pr": 1313, "trigger": "handback", "binding": "head",
        "anchored": True, "witness_only": False, "same_family_approve": False,
        "would_hold": True, "late": False, "multi_card": False,
        "findings": 2, "items": ["F1 P1", "F2 P2"],
        "reviewer": {
            "kind": "stage", "profile": "themis", "model": "claude-opus-5-5",
            "family": "anthropic", "transport": "cpr-cli-slim", "tier": 0,
            "prompt_version": "a1b2c3d4e5f6", "input_sha256": "f" * 64, "latency_s": 41.5,
        },
        "by": "themis", "event_ts": "2026-09-27T23:04:00Z",
        "written_at": "2026-09-27T23:05:12.5+00:00",
        "record": "state/themis/records/" + "3f" * 16 + ".json",
    }
    rec.update(overrides)
    return rec


def themis_file(**overrides):
    rec = themis_board(
        eligible_at="2026-09-27T23:03:00Z", last_send_back_at=None, handback_run_id=232,
        head_origin="push", claim_stolen=False, multi_card_cards=["t_1296b54f"],
        board="posted", author_family="openai", sensitive_hits=[],
        acceptance=[{"criterion": "v1 still validates", "status": "met", "pointer": "tests/x.py:1"}],
        finding_records=[{"id": "F1", "severity": "P1", "lens": "contract", "file": "a.py",
                          "line": 3, "claim": "c", "evidence": "e", "action": "ask-user"}],
    )
    rec.update(overrides)
    return rec


# Shapes observed on the live boards (591 comments, read-only, 2026-09-28):
# free-form verdicts, string reviewers, ad-hoc keys. None may enter v2 mode.
V1_SHAPES = [
    {"lenses": {"contract": "done"}, "findings": 1, "items": ["x at a:1"], "review_minutes": 3,
     "batch_id": "b", "head_sha": "abc1234"},
    {"verdict": "approve", "reviewer": "apollo", "note": "free text", "pr": "#12"},
    {"verdict": "REQUEST_CHANGES", "by": "argus", "scope": ["a"], "battery": "seeded"},
    {"verdict": "scope refinement per Ace, not a defect", "files_reviewed": 3, "round": 2},
    {"verdict": "NO-VERDICT", "reason": "anything", "gates": "x", "tests": 4, "read": True},
]


@pytest.mark.parametrize("rec", V1_SHAPES)
def test_v1_shapes_are_untouched(rec):
    assert not s.is_v2_record(rec)
    assert s.validate_v2_fields(rec, "board") is None
    assert s.validate_v2_fields(rec, "file") is None


def test_valid_themis_records_pass():
    assert s.validate_v2_fields(themis_board(), "board") is None
    assert s.validate_v2_fields(themis_file(), "file") is None
    nv = themis_board(verdict="NO-VERDICT", no_verdict_reason="packet-incomplete:4")
    assert s.validate_v2_fields(nv) is None
    ap = themis_board(verdict="APPROVE", ci_run="18234567890", would_hold=False)
    assert s.validate_v2_fields(ap) is None


def test_constants():
    assert s.SCHEMA_VERSION == 2
    assert s.VERDICTS == ("APPROVE", "CHANGES_REQUESTED", "NO-VERDICT")
    assert s.THEMIS_TRIGGERS == ("green", "handback", "sweep")
    assert s.THEMIS_TIERS == (0, 1)
    assert {"cpr-cli-slim", "cpr-slim", "apx", "bpr"} <= set(s.THEMIS_TRANSPORTS)
    assert s.THEMIS_BOARD_PREFIX == "themis:" and "review_coverage" not in s.THEMIS_BOARD_PREFIX
    for reason in ("oversize", "unanchored", "round-cap", "transport-off", "multi-card", "parse", "budget"):
        assert s.no_verdict_reason_ok(reason)
    assert not set(s.V2_BOARD_FIELDS) & set(s.V2_FILE_ONLY_FIELDS)


def test_schema_module_imports_without_kanban_db():
    code = ("import sys, hermes_cli.kanban_review_schema as m; "
            "assert 'hermes_cli.kanban_db' not in sys.modules; print(m.SCHEMA_VERSION)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "2"


# RED per field: (dotted key, bad value, fragment of the error). GREEN is themis_board().
BAD = [
    ("schema_version", 1, "schema_version"), ("schema_version", "2", "schema_version"),
    ("schema_version", True, "schema_version"),
    ("verdict", "approve", "verdict"), ("verdict", "REQUEST_CHANGES", "verdict"),
    ("verdict", "LGTM ship it", "verdict"),
    ("head_sha", "abc1234", "head_sha"), ("head_sha", HEAD.upper(), "head_sha"),
    ("base_sha", "abc", "base_sha"), ("pr", "1313", "pr"), ("pr", 0, "pr"),
    ("trigger", "manual", "trigger"), ("trigger", "Green", "trigger"),
    ("binding", "pr", "binding"),
    ("anchored", "true", "anchored"), ("anchored", 1, "anchored"),
    ("witness_only", None, "witness_only"), ("would_hold", "no", "would_hold"),
    ("late", 0, "late"), ("multi_card", "false", "multi_card"),
    ("same_family_approve", "x", "same_family_approve"),
    ("event_ts", "yesterday", "event_ts"), ("event_ts", "2026-09-27T23:04:00-07:00", "event_ts"),
    ("written_at", "2026-13-40T99:00:00Z", "written_at"),
    ("record", "state/themis/records/ANG-Ventures-repo.json", "record"),
    ("by", "apollo", "by"), ("items", ["F1 P1 missing guard in a.py", "F2 P2"], "items"),
    ("reviewer", "themis", "reviewer"),
    ("reviewer.profile", "Themis!", "reviewer.profile"),
    ("reviewer.model", "", "reviewer.model"), ("reviewer.family", "Anthropic Inc", "reviewer.family"),
    ("reviewer.transport", "bpx", "reviewer.transport"), ("reviewer.transport", "cpr-cli", "reviewer.transport"),
    ("reviewer.tier", 2, "reviewer.tier"), ("reviewer.tier", "0", "reviewer.tier"),
    ("reviewer.tier", True, "reviewer.tier"),
    ("reviewer.prompt_version", "a1b2c3", "reviewer.prompt_version"),
    ("reviewer.input_sha256", "f" * 63, "reviewer.input_sha256"),
    ("reviewer.latency_s", -1, "reviewer.latency_s"), ("reviewer.kind", "bot", "reviewer.kind"),
]


def _set(rec, dotted, value):
    head, _, tail = dotted.partition(".")
    if tail:
        rec[head][tail] = value
    else:
        rec[head] = value
    return rec


@pytest.mark.parametrize("key,value,frag", BAD, ids=[f"{k}={v!r}" for k, v, _ in BAD])
def test_red_per_field(key, value, frag):
    error = s.validate_v2_fields(_set(themis_board(), key, value), "board")
    assert error and frag.split(".")[-1] in error, error


FILE_BAD = [
    ("eligible_at", "soon"), ("last_send_back_at", "never"), ("handback_run_id", "232"),
    ("handback_run_id", 0), ("head_origin", "rebase"), ("claim_stolen", "no"),
    ("multi_card_cards", ["card one"]), ("board", "hidden"), ("author_family", "Open AI"),
    ("sensitive_hits", "a.py"), ("acceptance", []), ("acceptance", "n/a"),
    ("acceptance", [{"criterion": "c", "status": "done", "pointer": "p"}]),
    ("finding_records", [{"id": "F1", "severity": "P5", "lens": "l", "file": "f", "line": 1,
                          "claim": "c", "evidence": "e", "action": "no-op"}]),
    ("finding_records", [{"id": "F1", "severity": "P1", "lens": "l", "file": "f", "line": 1,
                          "claim": "c", "evidence": "e", "action": "no-op", "extra": 1}]),
]


@pytest.mark.parametrize("key,value", FILE_BAD, ids=[f"{k}={v!r}" for k, v in FILE_BAD])
def test_red_per_file_field(key, value):
    rec = themis_file()
    rec[key] = value
    error = s.validate_v2_fields(rec, "file")
    assert error and key in error, error


@pytest.mark.parametrize("key", s.THEMIS_REQUIRED_FIELDS)
def test_themis_required_fields(key):
    rec = themis_board()
    del rec[key]
    error = s.validate_v2_fields(rec)
    assert error and key in error, error


@pytest.mark.parametrize("key", s.THEMIS_REQUIRED_REVIEWER_FIELDS)
def test_themis_required_reviewer_fields(key):
    rec = themis_board()
    del rec["reviewer"][key]
    error = s.validate_v2_fields(rec)
    assert error and f"reviewer.{key}" in error, error


def test_stage_kind_alone_makes_a_themis_record():
    rec = themis_board()
    rec["reviewer"]["profile"] = "someone"
    del rec["trigger"]
    assert "trigger" in s.validate_v2_fields(rec)


def test_v2_fields_optional_for_non_themis_v2_author():
    rec = {"schema_version": 2, "verdict": "APPROVE", "lenses": {}, "findings": 1,
           "items": ["x at a:1"], "reviewer": {"kind": "human", "profile": "apollo"}}
    assert s.validate_v2_fields(rec) is None


def test_no_verdict_reason_iff_no_verdict():
    assert "no_verdict_reason" in s.validate_v2_fields(themis_board(verdict="NO-VERDICT"))
    assert "only when" in s.validate_v2_fields(themis_board(no_verdict_reason="oversize"))
    for bad in ("packet-incomplete:diff", "packet-incomplete:8", "the model refused", "Oversize"):
        error = s.validate_v2_fields(themis_board(verdict="NO-VERDICT", no_verdict_reason=bad))
        assert error and "no_verdict_reason" in error
    for good in s.NO_VERDICT_REASONS + tuple(f"packet-incomplete:{n}" for n in range(1, 8)):
        assert s.validate_v2_fields(themis_board(verdict="NO-VERDICT", no_verdict_reason=good)) is None


def test_themis_approve_needs_ci_run():
    assert "ci_run" in s.validate_v2_fields(themis_board(verdict="APPROVE"))
    assert "ci_run" in s.validate_v2_fields(themis_board(verdict="APPROVE", ci_run="run 12"))


@pytest.mark.parametrize("enum,key", [
    (s.THEMIS_TRIGGERS, "trigger"), (s.THEMIS_BINDINGS, "binding"), (s.VERDICTS, "verdict"),
    (s.THEMIS_TRANSPORTS, "reviewer.transport"), (s.THEMIS_TIERS, "reviewer.tier"),
])
def test_every_enum_value_is_green(enum, key):
    for value in enum:
        extra = {"no_verdict_reason": "cap"} if value == "NO-VERDICT" else {}
        extra.update({"ci_run": "1"} if value == "APPROVE" else {})
        assert s.validate_v2_fields(_set(themis_board(**extra), key, value)) is None, value


def test_file_only_keys_refused_on_board_accepted_in_file():
    for key in s.V2_FILE_ONLY_FIELDS:
        rec = themis_board(**{key: themis_file()[key]})
        assert key in s.validate_v2_fields(rec, "board")
        assert s.validate_v2_fields(rec, "file") is None
    # Unversioned v1 author leaking a file-only key onto the board.
    assert "board" in s.validate_v2_fields({"verdict": "approve", "eligible_at": "2026-09-27T00:00:00Z"})


def test_bad_surface_is_a_programming_error():
    with pytest.raises(ValueError):
        s.validate_v2_fields(themis_board(), "comment")


def _random_key(rng):
    alphabet = string.ascii_letters + string.digits + "_-. :/$é​"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))


@pytest.mark.parametrize("surface", s.SURFACES)
def test_fuzz_no_unknown_key_passes(surface):
    rng = random.Random(1296)
    known_top = set(s.V1_FIELDS) | set(s.V2_BOARD_FIELDS) | set(s.V2_FILE_ONLY_FIELDS)
    base = themis_file() if surface == "file" else themis_board()
    tried = 0
    while tried < 2000:
        key = _random_key(rng)
        if key in known_top or key in s.REVIEWER_FIELDS:
            continue
        tried += 1
        value = rng.choice([True, 0, "x", None, [], {}, "APPROVE"])
        top = copy.deepcopy(base)
        top[key] = value
        assert s.validate_v2_fields(top, surface) is not None, key
        nested = copy.deepcopy(base)
        nested["reviewer"][key] = value
        assert s.validate_v2_fields(nested, surface) is not None, key
        plain = {"schema_version": 2, key: value}
        assert s.validate_v2_fields(plain, surface) is not None, key


# Gate: kanban_db._validate_review_coverage calls the same v2 function.
def _gate(payload):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, body TEXT)")
    conn.execute("INSERT INTO task_comments (task_id, run_id, body) VALUES ('t_x', 7, ?)",
                 ("review_coverage: " + json.dumps(payload),))
    return kb._validate_review_coverage(conn, "t_x", 7)


def _gate_ready(rec):
    rec = dict(rec)
    rec.update(lenses={k: "done" for k in s.REQUIRED_REVIEW_LENSES}, review_minutes=4, batch_id="themis:a1b2c3d4e5f6")
    return rec


def test_gate_v1_record_unchanged():
    v1 = {"lenses": {k: "done" for k in s.REQUIRED_REVIEW_LENSES}, "findings": 1,
          "items": ["Missing guard at handler:42"], "review_minutes": 12, "batch_id": "b-1",
          "head_sha": "abc1234", "verdict": "changes_requested", "reviewer": "apollo", "note": "free"}
    assert _gate(v1) is None


def test_gate_accepts_valid_and_refuses_invalid_themis_record():
    assert _gate(_gate_ready(themis_board())) is None
    assert "trigger" in _gate(_gate_ready(themis_board(trigger="cron")))
    assert "unknown key" in _gate(_gate_ready(themis_board(summary="looks fine to me")))
    assert "eligible_at" in _gate(_gate_ready(themis_board(eligible_at="2026-09-27T00:00:00Z")))
