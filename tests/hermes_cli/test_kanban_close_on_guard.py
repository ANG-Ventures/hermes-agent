"""The kanban comment write path refuses a malformed ``close-on`` line (t_ebc8eb87).

Regression for t_d712b9ff (2026-10-17 10:47): an ACK comment quoting "Bare `close-on: merged`"
in prose was read by the review-merge-pass as a new, unprovable gate. The check lives in
``kanban_db.add_comment`` so the CLI verb and the ``kanban_comment`` tool are refused by the
same function. The fleet grammar is loaded from ``<fleet root>/scripts/lib``; the fixture below
installs a stand-in with the real ``LINE_RE`` and the release shapes of ``parse_release``.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_close_on_guard as guard
from hermes_cli import kanban_db as kb

ACK_1047 = ("DEADMAN ACK (Apollo 11:25 PT): #4737 MERGED a9fd8b9fc7. The closer held on my earlier "
            "close-on line (trailing prose after `merged` is malformed per close_on.py). Bare "
            "`close-on: merged` posted above; the next review-merge-pass tick completes this card.")

CLOSE_ON_LIB = textwrap.dedent(r'''
    import json
    import re
    LINE_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?[*_`]*close-on[*_`]*[ \t]*:[*_`]*[ \t]*(?P<gate>[^\n]*)$", re.I | re.M)

    def parse_release(gate):
        raw = " ".join(str(gate or "").split()).strip(" *_`")
        if re.fullmatch(r"merged\.?", raw, re.I):
            return {"kind": "merged"}
        if re.match(r"manual\s*:\s*\S", raw, re.I):
            return {"kind": "gate"}
        head, _, text = raw.partition(" ")
        if re.fullmatch(r"[0-9a-f]{12}", head) and text.strip():
            return {"kind": "cron"}
        return None

    def _hold_verdict(rec):
        v = re.sub(r"[-\s]+", "_", str(rec.get("verdict") or "").strip().upper())
        return v in ("CHANGES_REQUESTED", "REQUEST_CHANGES", "BLOCK", "BLOCKED", "REJECT", "REJECTED")

    def latest(con, tid, trusted=lambda a: True, is_machine=lambda b: False, prose_gate=None, include_block=False):
        # The comment half of scripts/lib/close_on.latest(): close-on lines (order 1), review_coverage records
        # carrying close_on or a HOLD verdict (order 2); newest (created_at, order) wins.
        found = []
        for cid, author, body, t in con.execute("SELECT id, author, body, created_at FROM task_comments "
                                                "WHERE task_id=? ORDER BY created_at, id", (tid,)):
            if is_machine(body) or not trusted(author):
                continue
            for m in LINE_RE.finditer(body or ""):
                found.append((int(t or 0), 1, m.group("gate").strip(), None, f"comment {cid} ({author})"))
            for line in str(body or "").splitlines():
                s = line.strip()
                if not s.startswith("review_coverage:"):
                    continue
                try:
                    rec = json.loads(s.split(":", 1)[1].strip())
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("close_on"):
                    found.append((int(t or 0), 2, str(rec["close_on"]), None, f"review_coverage comment {cid} ({author})"))
                elif _hold_verdict(rec):
                    found.append((int(t or 0), 2, "(review_coverage %s without close_on)" % rec.get("verdict"), None,
                                  f"review_coverage comment {cid} ({author})"))
        if not found:
            return None
        t, _, raw, p, src = max(found, key=lambda f: (f[0], f[1]))
        return {"raw": raw, "parsed": p, "source": src, "at": t}
''')

OUR_AUTHORS_LIB = textwrap.dedent('''
    def is_ours(author):
        return isinstance(author, str) and author.strip().lower() in ("apollo", "default", "human:apollo")

    def is_machine(body):
        return isinstance(body, str) and body.lstrip().startswith("[review-merge-pass]")
''')

OPERATOR_IDENTITY_LIB = textwrap.dedent('''
    def is_operator(author):
        a = str(author or "").strip().lower()
        return a in ("apollo", "ace", "default", "human", "aegis") or a.startswith("human:")
''')


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "lib" / "close_on.py").write_text(CLOSE_ON_LIB, encoding="utf-8")
    (home / "scripts" / "lib" / "our_authors.py").write_text(OUR_AUTHORS_LIB, encoding="utf-8")
    (home / "scripts" / "lib" / "operator_identity.py").write_text(OPERATOR_IDENTITY_LIB, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN") and k != "HERMES_KANBAN_HOME"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    guard._LIB_CACHE.clear()
    kb._INITIALIZED_PATHS.clear()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus")
    return home, tid


def _comments(tid):
    with kb.connect_closing() as conn:
        return [c.body for c in kb.list_comments(conn, tid)]


def _tool(tid, body):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_comment({"task_id": tid, "body": body}))


def _cli(tid, body, capsys):
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    rc = kc.kanban_command(top.parse_args(["kanban", "comment", tid, "--body", body]))
    return rc, capsys.readouterr().err


@pytest.mark.parametrize("body", [
    "close-on: merged",
    "context first\n\nclose-on: merged\n",
    "close-on: manual: Ace reads the dashboard",
    "release line posted; the card closes on merge",
])
def test_valid_lines_and_plain_prose_are_allowed(fleet, body):
    _, tid = fleet
    assert _tool(tid, body).get("ok") is True
    assert _comments(tid) == [body.strip()]


@pytest.mark.parametrize("body", [ACK_1047, "close-on: merged — ANG-Ventures/x#1 MERGED via fleet-merge"])
def test_tool_and_cli_refuse_through_add_comment(fleet, capsys, body):
    _, tid = fleet
    out = _tool(tid, body)
    assert "ok" not in out and guard.REFUSAL_HINT in json.dumps(out)
    rc, err = _cli(tid, body, capsys)
    assert rc != 0 and guard.REFUSAL_HINT in err
    assert _comments(tid) == []


def test_both_entry_points_route_through_the_one_function(fleet, capsys, monkeypatch):
    """With check_comment disabled, both surfaces write the body: no second check exists."""
    _, tid = fleet
    monkeypatch.setattr(guard, "check_comment", lambda author, body: None)
    assert _tool(tid, ACK_1047).get("ok") is True
    assert _cli(tid, ACK_1047, capsys)[0] == 0
    assert _comments(tid) == [ACK_1047, ACK_1047]


def test_machine_authors_and_marked_bodies_are_exempt(fleet):
    _, tid = fleet
    marked = "[review-merge-pass] LANDED o/r#1 @ abc: held, close-on line has trailing prose"
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "kanban-dispatcher", ACK_1047)
        kb.add_comment(conn, tid, "default", marked)
    assert _comments(tid) == [ACK_1047, marked]


BOOMERANG_RECEIPT = ("[boomerang-wake] complete: PR #5084 merged; the card's close-on: merged condition is met")


def test_boomerang_wake_executor_receipt_is_exempt(fleet):
    """t_befc7c69: the executor (gateway/kanban_boomerang_wake.AUTHOR) posts receipts that quote the gate."""
    from gateway import kanban_boomerang_wake as bw

    assert bw.AUTHOR in guard.MACHINE_AUTHORS
    guard.check_comment(bw.AUTHOR, BOOMERANG_RECEIPT)
    with pytest.raises(guard.CloseOnLineRefused):
        guard.check_comment("apollo", BOOMERANG_RECEIPT)


def test_unreadable_library_allows(fleet):
    home, tid = fleet
    (home / "scripts" / "lib" / "close_on.py").unlink()
    guard._LIB_CACHE.clear()
    assert _tool(tid, ACK_1047).get("ok") is True


def test_off_switch_allows(fleet):
    home, tid = fleet
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "close-on-comment-policy-off").write_text("", encoding="utf-8")
    assert _tool(tid, ACK_1047).get("ok") is True


@pytest.mark.parametrize("sep", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"])
def test_non_newline_separators_are_one_line_like_the_fleet_grammar(fleet, sep):
    """Prism 38d008dd32f1: ``splitlines()`` split ``merged<sep>prose`` into a valid line + prose."""
    home, tid = fleet
    body = f"close-on: merged{sep}trailing prose"
    lib = guard._load("close_on")
    gate = lib.LINE_RE.search(body).group("gate")
    assert lib.parse_release(gate) is None, "the fleet grammar reads this as ONE invalid gate"
    with pytest.raises(guard.CloseOnLineRefused):
        with kb.connect_closing() as conn:
            kb.add_comment(conn, tid, "daedalus", body)
    assert _comments(tid) == []


def _triaged(tid):
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))


def test_triage_resolve_reason_is_refused_before_any_write(fleet):
    """Prism bfa6281ecbac: ``triage_resolve_task`` inserted its reason inline, past the guard."""
    _, tid = fleet
    _triaged(tid)
    with kb.connect_closing() as conn:
        events = len(kb.list_events(conn, tid))
        ok, err = kb.triage_resolve_task(conn, tid, to="todo", actor="daedalus",
                                         reason="Bare `close-on: merged` posted above")
        assert not ok and guard.REFUSAL_HINT in err
        assert kb.get_task(conn, tid).status == "triage"
        assert len(kb.list_events(conn, tid)) == events
    assert _comments(tid) == []
    with kb.connect_closing() as conn:
        assert kb.triage_resolve_task(conn, tid, to="todo", actor="daedalus", reason="human decided")[0]
    assert _comments(tid) == ["TRIAGE-RESOLVE -> todo: human decided"]


def test_request_changes_coverage_is_refused_before_any_write(fleet, monkeypatch):
    """Prism bfa6281ecbac: the review_coverage comment was an inline INSERT, past the guard."""
    from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

    _, tid = fleet
    session = "20261017_200000_operator"
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    with kb.connect_closing() as conn:
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
        before = (len(kb.list_events(conn, tid)), len(kb.list_runs(conn, tid)))
    coverage = json.dumps({
        "verdict": "CHANGES_REQUESTED", "head_sha": "1ee13c3d39", "lenses": {k: "done" for k in LENSES},
        "findings": 1, "items": ["Bare `close-on: merged` posted above"], "review_minutes": 4, "batch_id": "b1",
    })
    with kb.connect_closing() as conn:
        ok, err = kb.request_changes(conn, tid, reason="fix it", claimer="apollo", coverage=coverage,
                                     session_ref=kb.derive_session_ref(session))
        assert not ok and guard.REFUSAL_HINT in err, err
        assert (len(kb.list_events(conn, tid)), len(kb.list_runs(conn, tid))) == before
        assert kb.get_task(conn, tid).status == "review"
    assert _comments(tid) == []


def test_insert_comment_helper_is_guarded(fleet):
    """The shared inline helper (``_insert_comment``) carries the same check."""
    _, tid = fleet
    with pytest.raises(guard.CloseOnLineRefused):
        with kb.connect_closing() as conn, kb.write_txn(conn):
            kb._insert_comment(conn, tid, "daedalus", "Bare `close-on: merged` above", 1)
    assert _comments(tid) == []


def test_block_reason_is_refused_before_the_transition(fleet, capsys):
    _, tid = fleet
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    rc = kc.kanban_command(top.parse_args(["kanban", "block", tid, "--body", ACK_1047]))
    assert rc != 0 and guard.REFUSAL_HINT in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "blocked"
    assert _comments(tid) == []


# --- kanban comment --if-latest-release (Prism 3406ba8bbfd9 / f3498875f176, t_fc712261) ------------------------
HOLD = "close-on: manual: land-pr holds this card until the release posts"


def _cli_args(tid, body, capsys, *extra):
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    rc = kc.kanban_command(top.parse_args(["kanban", "comment", tid, "--body", body, *extra]))
    return rc, capsys.readouterr().err


def _post(tid, author, body):
    with kb.connect_closing() as conn:
        return kb.add_comment(conn, tid, author, body)


def test_if_latest_release_refuses_when_an_operator_line_landed_after_the_read(fleet, capsys):
    """The Prism repro: the merge pass read hold c10, an operator posted c11, the staged append is refused, no c12."""
    _, tid = fleet
    c10 = _post(tid, "apollo", HOLD)
    c11 = _post(tid, "apollo", "close-on: manual: Ace holds this until the acceptance run")
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c10))
    assert rc == 3 and f"comment {c11}" in err, err
    assert _comments(tid) == [HOLD, "close-on: manual: Ace holds this until the acceptance run"]


def test_if_latest_release_appends_while_the_read_comment_is_still_current(fleet, capsys):
    _, tid = fleet
    c10 = _post(tid, "apollo", HOLD)
    _post(tid, "apollo", "plain prose, no release line")
    _post(tid, "default", "[review-merge-pass] LANDED o/r#1: close-on: merged quoted by automation")
    assert _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c10))[0] == 0
    assert _comments(tid)[-1] == "close-on: merged"


def test_if_latest_release_fails_closed_on_an_id_from_another_card(fleet, capsys):
    _, tid = fleet
    with kb.connect_closing() as conn:
        other = kb.create_task(conn, title="other", assignee="daedalus")
    foreign = _post(other, "apollo", HOLD)
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(foreign))
    assert rc == 3 and "not a comment on" in err
    rc, _ = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", "999999")
    assert rc == 3 and _comments(tid) == []


def test_if_latest_release_refuses_an_anchor_that_is_not_release_bearing(fleet, capsys):
    """Prism 1a7315c3ea79: hold c1, prose c2, append anchored on c2 -> rc 3, nothing written.
    The anchor must itself be the newest release-bearing comment, not merely a newer one."""
    _, tid = fleet
    _post(tid, "apollo", HOLD)
    c2 = _post(tid, "apollo", "plain prose, no release line")
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c2))
    assert rc == 3 and "not a release-bearing comment" in err, err
    c3 = _post(tid, "default", "[review-merge-pass] LANDED o/r#1: close-on: merged quoted by automation")
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c3))
    assert rc == 3 and "not a release-bearing comment" in err, err
    assert _comments(tid) == [HOLD, "plain prose, no release line",
                              "[review-merge-pass] LANDED o/r#1: close-on: merged quoted by automation"]


def test_if_latest_release_check_shares_the_insert_transaction(fleet, monkeypatch):
    """The check runs inside the insert's write_txn: a newer line committed by the time the
    check reads is seen, and a refusal leaves no row and no ``commented`` event."""
    _, tid = fleet
    c10 = _post(tid, "apollo", HOLD)
    _post(tid, "apollo", "close-on: merged")
    seen = []
    real = guard.newer_release_comment

    def spy(conn, task_id, cid, *rest):
        seen.append(conn.in_transaction)
        return real(conn, task_id, cid, *rest)

    monkeypatch.setattr(guard, "newer_release_comment", spy)
    with kb.connect_closing() as conn:
        events = len(kb.list_events(conn, tid))
        with pytest.raises(guard.ReleaseSuperseded):
            kb.add_comment(conn, tid, "apollo", "close-on: merged", if_latest_release=c10)
        assert len(kb.list_events(conn, tid)) == events
    assert seen == [True]
    assert len(_comments(tid)) == 2


def test_if_latest_release_fails_closed_without_the_fleet_grammar(fleet):
    home, tid = fleet
    c10 = _post(tid, "apollo", HOLD)
    _post(tid, "apollo", "close-on: merged")
    (home / "scripts" / "lib" / "close_on.py").unlink()
    guard._LIB_CACHE.clear()
    with pytest.raises(guard.ReleaseSuperseded):
        with kb.connect_closing() as conn:
            kb.add_comment(conn, tid, "apollo", "close-on: merged", if_latest_release=c10)


def test_if_latest_release_fails_closed_without_the_automation_classifier(fleet):
    """Prism 1a7315c3ea79 (round 2): with our_authors.py unavailable, an automation release line posted as
    `default` must not become an accepted anchor that hides the operator's older hold."""
    home, tid = fleet
    _post(tid, "apollo", HOLD)
    c2 = _post(tid, "default", "[review-merge-pass]\nclose-on: merged")
    (home / "scripts" / "lib" / "our_authors.py").unlink()
    guard._LIB_CACHE.clear()
    with pytest.raises(guard.ReleaseSuperseded):
        with kb.connect_closing() as conn:
            kb.add_comment(conn, tid, "daedalus", "close-on: merged", if_latest_release=c2)
    assert _comments(tid) == [HOLD, "[review-merge-pass]\nclose-on: merged"]


def _coverage(rec):
    return "review_coverage: " + json.dumps(rec)


def test_if_latest_release_refuses_when_an_operator_coverage_gate_arrived_after_the_check(fleet, capsys):
    """Prism 2918b8f9f46e: the merge pass's statement check passes on hold c1, then an operator posts a
    review_coverage record with a different close_on (a gate close_on.release() never reads), then the conditional
    insert anchored on c1 must refuse (rc 3) and write nothing."""
    home, tid = fleet
    c1 = _post(tid, "apollo", HOLD)
    lib = guard._load("close_on")
    with kb.connect_closing() as conn:   # the merge pass's statement check: the hold is the card's latest gate
        assert lib.latest(conn, tid)["source"].startswith(f"comment {c1} (")
    gate = _coverage({"verdict": "APPROVE", "close_on": "pr-merged o/r#9"})
    c2 = _post(tid, "apollo", gate)
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c1))
    assert rc == 3 and f"comment {c2}" in err and "review_coverage" in err, err
    assert _comments(tid) == [HOLD, gate]
    # a HOLD verdict without close_on is a newer manual statement: same refusal; `ace` is an operator
    # (operator_identity.is_operator, the merge pass's write-boundary rule since Prism f9c75767e0e1)
    hold = _coverage({"verdict": "CHANGES_REQUESTED"})
    _post(tid, "ace", hold)
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c1))
    assert rc == 3 and "review_coverage" in err, err


def test_if_latest_release_ignores_a_coverage_record_the_closer_does_not_trust(fleet, capsys):
    """An implementer's review_coverage close_on is not a gate (close_on_trusted): the append still posts."""
    _, tid = fleet
    c1 = _post(tid, "apollo", HOLD)
    _post(tid, "daedalus", _coverage({"verdict": "APPROVE", "close_on": "pr-merged o/r#9"}))
    _post(tid, "default", "[review-merge-pass] " + _coverage({"close_on": "pr-merged o/r#9"}))
    assert _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c1))[0] == 0
    assert _comments(tid)[-1] == "close-on: merged"


def test_if_latest_release_passes_when_the_coverage_gate_is_the_line_being_posted(fleet):
    """The merge pass posts as the hold's (operator) author; a restate counts only when close_on.latest() would read
    the appended comment itself as the card's gate, so an author the closer does not trust cannot restate."""
    _, tid = fleet
    c1 = _post(tid, "apollo", HOLD)
    _post(tid, "apollo", _coverage({"verdict": "APPROVE", "close_on": "merged"}))
    with kb.connect_closing() as conn:
        with pytest.raises(guard.ReleaseSuperseded):
            kb.add_comment(conn, tid, "daedalus", "close-on: merged", if_latest_release=c1)
        kb.add_comment(conn, tid, "apollo", "close-on: merged", if_latest_release=c1)
    assert _comments(tid)[-1] == "close-on: merged"


@pytest.mark.parametrize("gone", ["close_on.latest", "operator_identity.is_operator"])
def test_if_latest_release_fails_closed_without_the_gate_reader(fleet, capsys, gone):
    home, tid = fleet
    c1 = _post(tid, "apollo", HOLD)
    if gone == "close_on.latest":
        (home / "scripts" / "lib" / "close_on.py").write_text(CLOSE_ON_LIB.replace("def latest(", "def _gone("),
                                                              encoding="utf-8")
    else:
        (home / "scripts" / "lib" / "operator_identity.py").unlink()
    guard._LIB_CACHE.clear()
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c1))
    assert rc == 3 and gone in err, err
    assert _comments(tid) == [HOLD]


# --- one trust predicate, gate_setter (Prism 70d6e09af5e2 / 85d133b843df, t_fc712261 final round) ---------------
def test_if_latest_release_counts_a_newer_conversion_gate(fleet, capsys):
    """Prism 70d6e09af5e2: the merge pass read operator hold c1, then close-on-convert posted c2, a gate the closer
    trusts. The append anchored on c1 must refuse (rc 3); the append anchored on c2 is a valid anchor."""
    _, tid = fleet
    c1 = _post(tid, "apollo", "close-on: manual: hold")
    c2 = _post(tid, "close-on-convert", "close-on: pr-merged o/r#9")
    rc, err = _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c1))
    assert rc == 3 and f"comment {c2}" in err, err
    assert _comments(tid) == ["close-on: manual: hold", "close-on: pr-merged o/r#9"]
    assert _cli_args(tid, "close-on: merged", capsys, "--if-latest-release", str(c2))[0] == 0


def test_gate_setter_is_the_one_trust_predicate(fleet):
    """Both scans read one predicate: the merge pass's land_pr_release rule, automation excluded by BODY marker."""
    authors, ops = guard._load("our_authors"), guard._load("operator_identity")
    gs = guard.gate_setter_from(authors, ops)
    for who in ("apollo", "default", "human:ace", "argus", "close-on-convert", "ace", "aegis"):
        assert gs(who, "close-on: merged"), who
        assert not gs(who, "[review-merge-pass] close-on: merged"), who
    for who in ("daedalus", "kanban", "fleetreview-route", "unverified:apollo"):
        assert not gs(who, "close-on: merged"), who


def test_if_latest_release_restate_must_not_carry_a_different_coverage_gate(fleet, capsys):
    """Prism 85d133b843df: the appended body restates the operator's coverage gate in its close-on line but carries
    its own review_coverage record with a different close_on, which close_on.latest() ranks above the line. The
    effective gate would become `merged`: refuse, nothing written."""
    _, tid = fleet
    c1 = _post(tid, "apollo", "close-on: merged")
    gate = _coverage({"close_on": "manual: acceptance pending"})
    _post(tid, "apollo", gate)
    body = "close-on: manual: acceptance pending\n" + _coverage({"close_on": "merged"})
    rc, err = _cli_args(tid, body, capsys, "--if-latest-release", str(c1))
    assert rc == 3 and "review_coverage" in err, err
    assert _comments(tid) == ["close-on: merged", gate]


def test_if_latest_release_restate_must_restate_on_every_close_on_line(fleet, capsys):
    """Only the body's LAST close-on line was compared; close_on.latest() takes the FIRST of equal-time statements,
    so a `merged` line ahead of the restated one became the effective gate. Judge the whole body: refuse."""
    _, tid = fleet
    c1 = _post(tid, "apollo", "close-on: merged")
    gate = _coverage({"close_on": "manual: acceptance pending"})
    _post(tid, "apollo", gate)
    body = "close-on: merged\nclose-on: manual: acceptance pending"
    rc, err = _cli_args(tid, body, capsys, "--if-latest-release", str(c1))
    assert rc == 3, err
    assert _comments(tid) == ["close-on: merged", gate]


# --- approve-stamp-writer at the write path (t_f2eb71b3) -------------------------------------------------------
# 2026-10-27 12:10 comment 153767 on t_e215d33f: a script built the stamp with json.dumps and shelled to the CLI,
# so the pre_tool_call hook never saw a literal. The write itself refuses it now.
HEAD40 = "a" * 40
STAMP = "review_coverage: " + json.dumps({"verdict": "APPROVE", "head_sha": HEAD40, "reviewer": "apollo",
                                          "by": "apollo/operator", "findings": 0})


@pytest.fixture
def operator_cli(fleet, monkeypatch):
    """The CLI as the operator: --author apollo with the operator token, as operator_approve.py posts."""
    home, tid = fleet
    kb.operator_token_path().parent.mkdir(parents=True, exist_ok=True)
    kb.operator_token_path().write_text("tok\n", encoding="utf-8")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    assert kb._operator_token_state() == "ok"
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    return home, tid


def test_runtime_built_stamp_through_the_cli_is_refused(operator_cli, tmp_path, capsys):
    """The 153767 shape: a script file builds the body at runtime and calls the CLI with --body-file."""
    _, tid = operator_cli
    body = "apollo manual: APPROVE @ aaaaaaa\n" + "review_coverage: " + json.dumps(
        {"verdict": "approve".upper(), "head_sha": HEAD40})
    f = tmp_path / "stamp.md"
    f.write_text(body, encoding="utf-8")
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    rc = kc.kanban_command(top.parse_args(["kanban", "comment", tid, "--author", "apollo", "--body-file", str(f)]))
    err = capsys.readouterr().err
    assert rc != 0 and "operator_approve.py" in err, err
    assert _comments(tid) == []


@pytest.mark.parametrize("author", ["apollo", "default", "human:ace", "unverified:apollo", "Apollo"])
def test_operator_stamp_is_refused_at_add_comment(fleet, author, monkeypatch):
    _, tid = fleet
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    with pytest.raises(guard.ApproveStampRefused):
        _post(tid, author, STAMP)
    assert _comments(tid) == []


def test_kanban_comment_tool_reaches_the_same_refusal(fleet, monkeypatch):
    _, tid = fleet
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    out = _tool(tid, STAMP)
    assert "ok" not in out and "operator_approve.py" in json.dumps(out)
    assert _comments(tid) == []


def test_the_one_writer_passes(fleet, monkeypatch):
    """operator_approve._post_env sets OPERATOR_APPROVE_WRITER=1 on the CLI child it spawns."""
    _, tid = fleet
    monkeypatch.setenv(guard.APPROVE_WRITER_ENV, "1")
    body = f"apollo manual: APPROVE @ aaaaaaa\n{STAMP}\nclose-on: merged"
    _post(tid, "apollo", body)
    assert _comments(tid) == [body]


@pytest.mark.parametrize("body", [
    "review_coverage: " + json.dumps({"verdict": "CHANGES_REQUESTED", "head_sha": HEAD40}),
    "review_coverage: " + json.dumps({"verdict": "REQUEST_CHANGES", "head_sha": HEAD40}),
    "APPROVE in prose, no record",
    "review_coverage: {not json APPROVE}",
])
def test_non_approve_records_pass_for_an_operator(fleet, body, monkeypatch):
    _, tid = fleet
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    _post(tid, "apollo", body)
    assert _comments(tid) == [body]


@pytest.mark.parametrize("author", ["daedalus", "argus", "land-autopilot", "main-train", "dispatcher"])
def test_non_operator_approve_records_pass(fleet, author, monkeypatch):
    _, tid = fleet
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    _post(tid, author, STAMP)
    assert _comments(tid) == [STAMP]


def test_writer_marker_must_be_exactly_one(fleet, monkeypatch):
    _, tid = fleet
    monkeypatch.setenv(guard.APPROVE_WRITER_ENV, "true")
    with pytest.raises(guard.ApproveStampRefused):
        _post(tid, "apollo", STAMP)


def test_approve_stamp_off_switch(fleet, monkeypatch):
    home, tid = fleet
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "approve-stamp-guard-off").write_text("", encoding="utf-8")
    _post(tid, "apollo", STAMP)
    assert _comments(tid) == [STAMP]


def test_reviewer_complete_approval_record_is_not_refused(fleet, monkeypatch):
    """complete_task's own APPROVE record (a reviewer run closing with head_sha) is the kernel's write, even when
    the card's assignee is an operator profile."""
    _, tid = fleet
    monkeypatch.delenv(guard.APPROVE_WRITER_ENV, raising=False)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee='default' WHERE id=?", (tid,))
    with kb.connect_closing() as conn:
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="argus",
                                 expected_run_id=impl.current_run_id)
        assert kb.claim_review_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="LGTM", metadata={"head_sha": HEAD40})
    assert any('"verdict": "approve"' in b for b in _comments(tid))


def test_cli_with_operator_token_and_writer_marker_posts(operator_cli, capsys, monkeypatch):
    """operator_approve.py's actual post: hermes kanban comment --author apollo --body TEXT, token + marker."""
    _, tid = operator_cli
    monkeypatch.setenv(guard.APPROVE_WRITER_ENV, "1")
    body = f"apollo manual: APPROVE @ aaaaaaa\n{STAMP}\nclose-on: merged"
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    rc = kc.kanban_command(top.parse_args(["kanban", "comment", tid, "--author", "apollo", "--body", body]))
    assert rc == 0, capsys.readouterr().err
    assert _comments(tid) == [body]


def _granted_post(home, tid, body):
    """A delegate child's comment signed as `apollo` under a live grant (t_19c567a8)."""
    from agent.delegation_context import delegated_child_context
    from hermes_cli import kanban_sign_grants as ksg

    (home / "config.yaml").write_text("kanban:\n  sign_grants: 'on'\n")
    with kb.connect_closing() as conn:
        g = ksg.mint(conn, cards=[tid], verbs=["verdict", "approve", "release"], by="apollo",
                     child_session_ref=kb.derive_session_ref("child"), parent_model="m", child_model="m",
                     parent_profiles=["default", "apollo"])
    with delegated_child_context("child"), kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "apollo", body, session_ref=kb.derive_session_ref("child"), grant_id=g["grant_id"])
    return g


@pytest.mark.parametrize("granted", ["close-on: merged", _coverage({"verdict": "APPROVE", "close_on": "pr-merged o/r#9"})])
def test_a_granted_row_is_never_a_newer_gate_by_label(fleet, granted):
    """Apollo r8 / Prism 568d804ef684: a granted row is stored grant-marked, so neither the newer-release scan
    nor the coverage-gate check (_newer_coverage_gate) reads it as an operator's gate, and it is never an anchor."""
    home, tid = fleet
    c1 = _post(tid, "apollo", HOLD)
    _granted_post(home, tid, granted)
    with kb.connect_closing() as conn:
        granted_id = conn.execute("SELECT MAX(id) FROM task_comments").fetchone()[0]
        assert guard.newer_release_comment(conn, tid, c1, "close-on: merged", "apollo") is None
        assert "not a release-bearing" in guard.newer_release_comment(conn, tid, granted_id, "close-on: merged",
                                                                      "apollo")
