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
''')

OUR_AUTHORS_LIB = textwrap.dedent('''
    def is_machine(body):
        return isinstance(body, str) and body.lstrip().startswith("[review-merge-pass]")
''')


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "lib" / "close_on.py").write_text(CLOSE_ON_LIB, encoding="utf-8")
    (home / "scripts" / "lib" / "our_authors.py").write_text(OUR_AUTHORS_LIB, encoding="utf-8")
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


def test_if_latest_release_check_shares_the_insert_transaction(fleet, monkeypatch):
    """The check runs inside the insert's write_txn: a newer line committed by the time the
    check reads is seen, and a refusal leaves no row and no ``commented`` event."""
    _, tid = fleet
    c10 = _post(tid, "apollo", HOLD)
    _post(tid, "apollo", "close-on: merged")
    seen = []
    real = guard.newer_release_comment

    def spy(conn, task_id, cid):
        seen.append(conn.in_transaction)
        return real(conn, task_id, cid)

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
