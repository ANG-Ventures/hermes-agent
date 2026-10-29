"""A PR closed on purpose (``CLOSED: ABANDONED|REJECTED|THROWAWAY`` on its own thread) is terminal (t_2ce83b37).

t_14e6a81c sat in review forever: hermes-home#7741 was closed with ``CLOSED: ABANDONED -- ...`` and re-carded,
so there is no merged survivor, and ``complete --abandon-routed-pr`` / ``archive`` were refused. A stacked PR
auto-closed by base deletion has no such comment and is still refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

PR_URL = "https://github.com/ANG-Ventures/r/pull/5"
HOME_ENV = "HERMES_" + "HOME"


def closed5(repo, n):
    return {"state": "CLOSED" if n == 5 else op.NOT_FOUND}


@pytest.mark.parametrize("body,ok", [
    ("CLOSED: ABANDONED -- design re-carded as t_90019410", True),
    ("note\nCLOSED: REJECTED -- finding r1", True),
    ("CLOSED: THROWAWAY -- CI vehicle", True),
    ("CLOSED: SUPERSEDED-BY #9 -- needs a merged survivor", False),
    ("CLOSED: STALE -- not a no-survivor reason", False),
    ("abandoning this, CLOSED: ABANDONED mid-line", False),
    (None, False),
])
def test_terminal_record_tokens(body, ok):
    assert op.has_terminal_close_record(body) is ok


def test_done_with_abandon_reason_and_terminal_record_passes():
    seen = []

    def record(repo, n):
        seen.append((repo, n))
        return True
    out = op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}, query_fn=closed5,
                                         abandon_reason="re-carded as t_90019410", terminal_record_fn=record)
    assert [(r.repo, r.number) for r in out] == [("ANG-Ventures/r", 5)]
    assert seen == [("ANG-Ventures/r", 5)]


@pytest.mark.parametrize("record", [lambda r, n: False, lambda r, n: None])
def test_done_without_terminal_record_still_refused(record):
    # The stacked-PR shape: auto-closed by base deletion, no CLOSED: comment (or GitHub unreadable).
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}, query_fn=closed5,
                                       abandon_reason="abandoned", terminal_record_fn=record)
    assert "CLOSED WITHOUT MERGE" in str(exc.value) and "--abandon-routed-pr" in str(exc.value)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_done_terminal_record_needs_the_abandon_reason(reason):
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}, query_fn=closed5,
                                       abandon_reason=reason, terminal_record_fn=lambda r, n: True)


def test_archive_accepts_terminal_record_without_a_flag():
    assert op.enforce_not_closed_unmerged("t_x", recorded=[PR_URL], query_fn=closed5, verb="archive",
                                          terminal_record_fn=lambda r, n: True)
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", recorded=[PR_URL], query_fn=closed5, verb="archive",
                                       terminal_record_fn=lambda r, n: False)


def test_every_closed_pr_needs_its_own_terminal_record():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=lambda r, n: {"state": "CLOSED"},
                                       abandon_reason="abandoned", terminal_record_fn=lambda r, n: n == 5)


def test_no_live_github_under_pytest():
    assert op._default_terminal_record() is None


# --- E2E through complete_task / archive_task on a temp board ---------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: closed5)
    return home


@pytest.fixture
def no_survivor(monkeypatch):
    import importlib
    ks = importlib.import_module(kb.__name__.rsplit(".", 1)[0] + ".kanban_survivor")
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)


def _in_review(conn):
    # t_14e6a81c shape: parked in review on its own PR, which was then closed ABANDONED.
    tid = kb.create_task(conn, title="slice", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    return tid


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _events(conn, tid):
    return [(r["kind"], json.loads(r["payload"]) if r["payload"] else None)
            for r in conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def test_e2e_abandoned_pr_with_flag_completes_and_is_audited(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_terminal_record", lambda: (lambda r, n: True))
    with kb.connect() as conn:
        tid = _in_review(conn)
        assert kb.complete_task(conn, tid, summary="abandoned by design; re-carded",
                                metadata={"pr_url": PR_URL}, survivor_none=True,
                                survivor_reason="PR abandoned, design re-carded",
                                abandon_routed_pr="re-carded as t_90019410")
        assert _status(conn, tid) == "done"
        audit = [p for k, p in _events(conn, tid) if k == "routed_pr_abandoned"]
        assert audit == [{"prs": ["ANG-Ventures/r#5"], "reason": "re-carded as t_90019410", "closed": True}]


def test_e2e_closed_pr_without_token_still_refused(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_terminal_record", lambda: (lambda r, n: False))
    with kb.connect() as conn:
        tid = _in_review(conn)
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="x", metadata={"pr_url": PR_URL}, survivor_none=True,
                             survivor_reason="r", abandon_routed_pr="abandoned")
        assert _status(conn, tid) == "review"
        kinds = [k for k, _ in _events(conn, tid)]
        assert "completion_blocked_closed_unmerged_pr" in kinds and "routed_pr_abandoned" not in kinds


def test_e2e_terminal_record_without_flag_refused(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_terminal_record", lambda: (lambda r, n: True))
    with kb.connect() as conn:
        tid = _in_review(conn)
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="x", metadata={"pr_url": PR_URL})
        assert _status(conn, tid) == "review"


def test_e2e_archive_with_terminal_record(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_terminal_record", lambda: (lambda r, n: True))
    with kb.connect() as conn:
        tid = _in_review(conn)
        monkeypatch.setattr(kb, "_card_recorded_pr_refs", lambda c, t, **k: [PR_URL])  # routed own PR
        assert kb.archive_task(conn, tid)
        assert _status(conn, tid) == "archived"
