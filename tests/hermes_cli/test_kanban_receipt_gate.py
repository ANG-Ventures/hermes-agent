"""A worker handoff that closes ``done`` must carry a receipt (t_e21aa11c).

Incident 2026-10-01: deploy cards closed ``done`` and the session overview read
"no receipt on card". The gate refuses a prose-only worker completion with reason
code ``no_receipt``; a receipt is a PR, a survivor claim, an attachment or
structured handoff metadata.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_receipt as rc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, number: {"state": "MERGED"}))
    kb.init_db()
    return home


def _claimed(conn):
    tid = kb.create_task(conn, title="deploy pcv #609 to ACE-AI", assignee="daedalus")
    run = kb.claim_task(conn, tid)
    assert run is not None
    return tid, run.current_run_id


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def test_prose_only_worker_completion_is_refused_with_named_code(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _claimed(conn)
        with pytest.raises(rc.ReceiptRequiredError) as err:
            kb.complete_task(conn, tid, summary="Deployed and live on ACE-AI, and verified.",
                             metadata={"worker_session_id": "s1", "no_pr": True,
                                       "no_pr_reason": "deploy-only"},
                             expected_run_id=run_id)
        assert err.value.code == "no_receipt"
        assert "no_receipt" in str(err.value)
        assert _status(conn, tid) == "running"
        assert rc.EVENT in _kinds(conn, tid)


def test_receipt_attachment_is_accepted(kanban_home, tmp_path):
    with kb.connect() as conn:
        tid, run_id = _claimed(conn)
        blob = tmp_path / "deploy-receipt.txt"
        blob.write_text("pcv 64eb851e -> 7fceff2a; read-back ok\n")
        kb.add_attachment(conn, tid, filename=blob.name, stored_path=str(blob),
                          size=blob.stat().st_size, uploaded_by="agent")
        assert kb.complete_task(conn, tid, summary="Deployed and live.",
                                expected_run_id=run_id) is True
        assert _status(conn, tid) == "done"


@pytest.mark.parametrize("kwargs", [
    {"metadata": {"pcv_before": "64eb851e", "pcv_after": "7fceff2a"}},
    {"metadata": {"artifacts": ["/tmp/x.log"]}},
    {"summary": "merged ANG-Ventures/pipecat-house-voice#609 and deployed"},
])
def test_structured_handback_or_pr_is_accepted(kanban_home, kwargs):
    kwargs = {"summary": "Deployed and live.", **kwargs}
    with kb.connect() as conn:
        tid, run_id = _claimed(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run_id, **kwargs) is True
        assert _status(conn, tid) == "done"


def test_operator_close_and_superseded_are_not_gated(kanban_home):
    with kb.connect() as conn:
        tid, _run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary="closed by hand") is True
        tid2, run2 = _claimed(conn)
        assert kb.complete_task(conn, tid2, superseded_by="t_c18e4162",
                                expected_run_id=run2) is True


def test_knob_off_disables_the_gate(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_receipt_gate", lambda: False)
    with kb.connect() as conn:
        tid, run_id = _claimed(conn)
        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run_id) is True


def test_cli_complete_exits_with_named_rc(kanban_home, monkeypatch, capsys):
    with kb.connect() as conn:
        tid, run_id = _claimed(conn)
    monkeypatch.setattr(kcli, "_worker_run_id_for", lambda _t: run_id)
    args = argparse.Namespace(task_ids=[tid], summary="Deployed and live.", result=None,
                              metadata=None, superseded_by=None, draft_ok=None)
    assert kcli._cmd_complete(args) == rc.EXIT_NO_RECEIPT == 4
    assert "no_receipt" in capsys.readouterr().err
    with kb.connect() as conn:
        assert _status(conn, tid) == "running"


def test_lint_lists_receipt_less_done_cards(kanban_home):
    with kb.connect() as conn:
        bare = kb.create_task(conn, title="bare", assignee="daedalus")
        kb.complete_task(conn, bare, summary="done")          # operator close, no receipt
        good = kb.create_task(conn, title="good", assignee="daedalus")
        kb.complete_task(conn, good, summary="done", metadata={"sha_after": "abc"})
        sup = kb.create_task(conn, title="sup", assignee="daedalus")
        kb.complete_task(conn, sup, superseded_by="t_c18e4162")
        old = kb.create_task(conn, title="old", assignee="daedalus")
        kb.complete_task(conn, old, summary="done")
        conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?",
                     (int(time.time()) - 9 * 86400, old))
        conn.commit()
        found = [c["id"] for c in rc.lint(conn, days=7)]
    assert found == [bare]


def test_lint_cli_rc(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bare", assignee="daedalus")
        kb.complete_task(conn, tid, summary="done")
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert rc._main(["--db", path, "--json"]) == 1
    assert [c["id"] for c in json.loads(capsys.readouterr().out)] == [tid]
