"""Operator-Surface S2 (t_cd1bcea4): ONE input convention + schema-on-miss for the kanban verbs.

Plan: ~/.hermes/plans/2026-10-12_operator-surface-overhaul-handoff.md section 3 S2 (P2, P3).
Closing condition: drive every lifecycle verb with empty and partial input and assert the refusal
names EVERY required field of that verb, not the first one argparse or the validator hit.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_verb_input as kvi
from hermes_cli.kanban_review_schema import REQUIRED_COVERAGE_FIELDS, REQUIRED_REVIEW_LENSES

SESSION = "20261015_010000_operator"
HEAD = "1ee13c3d39"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    import os
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    for key in ("HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="builder", session_id=SESSION)
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
    return tid


def _required(verb):
    return kvi.VERB_SCHEMAS[verb][0]


# (verb, argv after the verb) -- empty and partial inputs. {tid} is the card.
EMPTY_AND_PARTIAL = [
    ("create", []),
    ("create", ['""']),
    ("comment", []),
    ("comment", ["{tid}"]),
    ("comment", ["{tid}", "--body", '""']),
    ("complete", []),
    ("complete", ["{tid}", "--json", "'{\"a\":'"]),
    ("block", []),
    ("block", ["{tid}", "x", "--body", "y"]),
    ("request-review", []),
    ("request-review", ["{tid}", "--json", "'[1]'"]),
    ("request-changes", []),
    ("request-changes", ["{tid}"]),
    ("request-changes", ["{tid}", "--body", "fix", "--coverage", "'{\"findings\": 0}'"]),
    ("request-changes", ["{tid}", "--body", "fix", "--coverage", "'{not json'"]),
    ("request-changes", ["{tid}", "--body", "fix", "--lens", "contract=done"]),
]


@pytest.mark.parametrize("verb,argv", EMPTY_AND_PARTIAL,
                         ids=[f"{v}:{' '.join(a) or 'empty'}" for v, a in EMPTY_AND_PARTIAL])
def test_every_refusal_names_every_required_field(board, monkeypatch, verb, argv):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash(" ".join([verb, *[a.replace("{tid}", board) for a in argv]]))
    missing = [field for field in _required(verb) if field not in out]
    assert not missing, f"refusal omits {missing}:\n{out}"
    assert "example:" in out.lower(), out


def test_request_changes_schema_names_every_coverage_field():
    text = kvi.schema_text("request-changes")
    for field in ("lenses", *REQUIRED_COVERAGE_FIELDS, *REQUIRED_REVIEW_LENSES):
        assert field in text


def test_partial_coverage_lists_all_problems_in_one_refusal():
    """The four-attempt request-changes (plan row 2): one refusal must name all four."""
    err = kb.review_coverage_text_error(json.dumps({"lenses": {lens: "done" for lens in REQUIRED_REVIEW_LENSES}}))
    for fragment in ("findings must be", "items must list", "review_minutes must be",
                     "batch_id must", "head_sha must"):
        assert fragment in err, err


def test_redaction_never_breaks_valid_coverage_json():
    """Tonight's incident: valid JSON (python json.load OK) refused as invalid, because the
    redactor rewrote KEY="value" inside an item and ate the closing quote."""
    record = {"lenses": {lens: "done" for lens in REQUIRED_REVIEW_LENSES}, "findings": 2,
              "items": ['run.sh exports API_KEY="sk-abcdefghijklmnopqrstuv"',
                        'curl -H "X-Api-Key: abcdefabcdefabcdef" in the probe'],
              "review_minutes": 3, "batch_id": "n/a: solo", "head_sha": HEAD}
    text = json.dumps(record)
    assert kb.review_coverage_text_error(text) is None
    redacted = kb.redact_review_coverage_text(text)
    assert kb.review_coverage_text_error(redacted) is None, redacted
    assert "sk-abcdefghijklmnopqrstuv" not in redacted


def test_db_request_changes_accepts_secret_shaped_item(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    record = {"lenses": {lens: "done" for lens in REQUIRED_REVIEW_LENSES}, "findings": 1,
              "items": ['run.sh exports API_KEY="sk-abcdefghijklmnopqrstuv"'],
              "review_minutes": 3, "batch_id": "n/a: solo", "head_sha": HEAD}
    with kb.connect() as conn:
        ok, detail = kb.request_changes(
            conn, board, reason="rotate the key", claimer="apollo",
            coverage=json.dumps(record), session_ref=kb.derive_session_ref(SESSION))
        assert ok, detail
        bodies = [c.body for c in kb.list_comments(conn, board)]
    stored = [b for b in bodies if b.startswith("review_coverage:")]
    assert len(stored) == 1 and "sk-abcdefghijklmnopqrstuv" not in stored[0]
    json.loads(stored[0].split(":", 1)[1])


def test_flat_finding_form_builds_coverage_and_sends_back(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash(
        f"request-changes {board} --body 'two fixes' "
        f"--finding {shlex.quote('item one: $(whoami) in `run.sh`')} --finding 'item two' "
        f"--lens all=done --takeover 'probe: flat form'")
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        stored = [c.body for c in kb.list_comments(conn, board) if c.body.startswith("review_coverage:")]
    rec = json.loads(stored[-1].split(":", 1)[1])
    assert rec["findings"] == 2 and rec["items"] == ["item one: $(whoami) in `run.sh`", "item two"]
    assert rec["head_sha"] == "n/a: card routes no PR"
    assert type(rec["review_minutes"]) is int and rec["batch_id"]
    assert set(rec["lenses"]) == set(REQUIRED_REVIEW_LENSES)


def test_flat_form_json_override_wins(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash(
        f"request-changes {board} --body fix --finding 'one finding' --lens all=done "
        f"--json '{{\"head_sha\": \"{HEAD}\", \"batch_id\": \"b-7\"}}' --why")
    assert "built review_coverage:" in out, out
    built = json.loads(out.split("built review_coverage:", 1)[1].strip())
    assert built["head_sha"] == HEAD and built["batch_id"] == "b-7" and built["findings"] == 1


def test_why_never_writes(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    with kb.connect() as conn:
        before = (len(kb.list_comments(conn, board)), len(kb.list_events(conn, board)))
    for verb in ("comment", "complete", "block", "request-review", "request-changes"):
        out = cli.run_slash(f"{verb} {board} --body x --why")
        assert f"kanban {verb} input:" in out, out
    with kb.connect() as conn:
        after = (len(kb.list_comments(conn, board)), len(kb.list_events(conn, board)))
        assert kb.get_task(conn, board).status == "review"
    assert after == before


@pytest.mark.parametrize("verb", ["comment", "block", "request-review", "request-changes", "complete"])
def test_body_file_reaches_every_verb(board, tmp_path, monkeypatch, verb):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    f = tmp_path / "b.md"
    f.write_text("text with `ticks` and $(date)\n", encoding="utf-8")
    out = cli.run_slash(f"{verb} {board} --body-file {f} --why")
    assert "given twice" not in out and "--body-file:" not in out, out


def test_json_and_legacy_flag_together_refused(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash(f"complete {board} --body x --metadata '{{}}' --json '{{}}'")
    assert "same input" in out, out


# Prism #2011 follow-ups (t_83f9b892).
_PLAIN_SECRET = "hunter2hunter2xyz"


def test_coverage_redaction_keeps_key_based_rule():
    """Prism 0464d61148aa: a secret under a secret-named key has no recognizable prefix, so only
    the key rule (the serialized path's _JSON_FIELD_RE) can mask it."""
    record = {"lenses": {lens: "done" for lens in REQUIRED_REVIEW_LENSES}, "findings": 1,
              "items": ["one finding"], "review_minutes": 3, "batch_id": "n/a: solo", "head_sha": HEAD,
              "password": _PLAIN_SECRET, "extra": {"token": _PLAIN_SECRET}}
    redacted = kb.redact_review_coverage_text(json.dumps(record))
    assert _PLAIN_SECRET not in redacted, redacted
    assert kb.review_coverage_text_error(redacted) is None, redacted


def test_why_preview_prints_redacted_coverage(board, monkeypatch):
    """Prism eeae9238333f: --why must not echo a credential the write path would redact."""
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    out = cli.run_slash(
        f"request-changes {board} --body fix --finding {shlex.quote('leaked ' + secret)} "
        f"--lens all=done --json '{{\"head_sha\": \"{HEAD}\", \"password\": \"{_PLAIN_SECRET}\"}}' --why")
    assert "built review_coverage:" in out, out
    assert secret not in out and _PLAIN_SECRET not in out, out


def test_flat_form_head_reads_legacy_routed_pr(board, monkeypatch):
    """Prism d9a21feebf71: a legacy routed run (auto_routed_open_prs, no own_prs) still routes a PR."""
    from hermes_cli import kanban_handback_head as hh
    seen = []

    def reader(repo, number):
        seen.append((repo, number))
        return {"head": HEAD}

    monkeypatch.setattr(hh, "_default_reader", lambda: reader)
    with kb.connect() as conn:
        run = kb.latest_run(conn, board)
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                     (json.dumps({"auto_routed_open_prs": ["ANG-Ventures/hermes-agent#41"]}), run.id))
        conn.commit()
        assert kvi._routed_pr_head(conn, board) == (HEAD, "ANG-Ventures/hermes-agent#41")
    assert seen == [("ANG-Ventures/hermes-agent", 41)]


# --- --priority accepts p0..p3; the argparse refusal ends on the cause (2026-10-24) -------------

@pytest.mark.parametrize("text,expected", [("p0", 300), ("P1", 200), ("p2", 100), ("p3", 0), ("7", 7), ("-2", -2)])
def test_priority_value_accepts_tiers_and_ints(text, expected):
    from hermes_cli.kanban_parser import priority_value
    assert priority_value(text) == expected


def test_p0_sorts_above_every_live_priority_shape():
    """A p0 card must be dispatched first; the fleet's measured scale tops out at 300 (2026-10-24)."""
    from hermes_cli.kanban_parser import _P_TIERS, priority_value
    assert priority_value("p0") >= 300 and priority_value("p0") > priority_value("80")
    assert list(_P_TIERS.values()) == sorted(_P_TIERS.values(), reverse=True)


def test_priority_value_refuses_garbage_with_both_shapes_named():
    import argparse
    from hermes_cli.kanban_parser import priority_value
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        priority_value("high")
    assert "p0..p3" in str(exc.value) and "int" in str(exc.value)


def test_create_with_tier_priority_is_accepted(board, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash('create "tier prio card" --priority p2 --body x')
    assert "usage error" not in out, out
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT priority FROM tasks WHERE title = ?", ("tier prio card",)).fetchone()
    assert row is not None and row[0] == 100


def test_argparse_refusal_puts_the_cause_on_the_last_line(board, monkeypatch):
    """The schema is context; the refusal is the answer. A reader (or a `tail`) that sees only
    the last line must see WHY the call was refused, not the example invocation."""
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    out = cli.run_slash('create "x" --priority high')
    last = out.rstrip().splitlines()[-1]
    assert "error:" in last and "--priority" in last, out
    assert "example:" in out.lower(), out
