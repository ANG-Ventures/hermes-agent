"""Declared PRs beat prose on the completion route (t_0ecd6506).

t_536b03d9 declared own_prs=[#3918]; its summary said "the fix is Apollo's #3853" and the router
gated it on #3853 as well. When the handoff declares its PRs (own_prs / pr_url / pr_urls / pr /
--survivor-pr), the route and the "card closes on merged=true (...)" set are exactly those refs.
The summary text gates only when nothing is declared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

REPO = "ANG-Ventures/hermes-home"
OWN, MENTIONED = 3918, 3853
SUMMARY = f"Shipped {REPO}#{OWN}. The upstream fix is Apollo's {REPO}#{MENTIONED}, not this card's."


def _query(repo, number):
    # Neither head branch names a card: the t_60b39483 branch heuristic cannot save this case.
    return {"state": "OPEN", "head_ref": f"fix/pr-{number}"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: _query)
    kb.init_db()
    return home


def _route(metadata):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="own-prs route", assignee="daedalus")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary=SUMMARY, metadata=metadata) is True
        assert kb.get_task(conn, tid).status == "review"
        meta = kb.latest_run(conn, tid).metadata
        comments = [r["body"] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (tid,))]
    return meta, comments


@pytest.mark.parametrize("metadata", [
    {"own_prs": [f"{REPO}#{OWN}"]},
    {"pr_url": f"https://github.com/{REPO}/pull/{OWN}"},
    {"pr_urls": [f"https://github.com/{REPO}/pull/{OWN}"]},
])
def test_declared_prs_are_the_route_and_close_set(kanban_home, metadata):
    meta, comments = _route(metadata)
    assert meta["auto_routed_open_prs"] == [f"{REPO}#{OWN}"]
    assert meta["own_prs"] == [f"{REPO}#{OWN}"]
    assert meta["mentioned_unowned_prs"] == [f"{REPO}#{MENTIONED}"]
    assert comments == [f"survivor PR open; card closes on merged=true ({REPO}#{OWN})"]


def test_no_declared_prs_falls_back_to_the_summary_scan(kanban_home):
    meta, comments = _route({"tests_run": 1})
    assert meta["auto_routed_open_prs"] == [f"{REPO}#{OWN}", f"{REPO}#{MENTIONED}"]
    assert comments == [
        f"survivor PR open; card closes on merged=true ({REPO}#{OWN}, {REPO}#{MENTIONED})"]


def test_declared_pr_keeps_a_prose_pr_on_this_cards_branch_gating(kanban_home, monkeypatch):
    """t_287a2872: a merged declared pr_url plus "follow-up in o/r#N" must not let #N, still open on
    THIS card's branch, slip to done. Another card's branch, or no card id, stays a mention."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="follow-up route", assignee="daedalus")
        heads = {OWN: None, MENTIONED: f"daedalus/{tid}-follow-up", 3854: "daedalus/t_0000aaaa-x"}

        def query(repo, number):
            if number == OWN:
                return {"state": "MERGED", "merged": True, "head_ref": f"daedalus/{tid}-main"}
            return {"state": "OPEN", "head_ref": heads.get(number) or f"fix/pr-{number}"}

        monkeypatch.setattr(op, "_default_query", lambda: query)
        kb.claim_task(conn, tid)
        summary = (f"Shipped {REPO}#{OWN}; follow-up in {REPO}#{MENTIONED}. "
                   f"Unrelated: {REPO}#3854, {REPO}#3855.")
        assert kb.complete_task(conn, tid, summary=summary,
                                metadata={"pr_url": f"https://github.com/{REPO}/pull/{OWN}"}) is True
        assert kb.get_task(conn, tid).status == "review"
        meta = kb.latest_run(conn, tid).metadata
    assert meta["auto_routed_open_prs"] == [f"{REPO}#{MENTIONED}"]
    assert meta["mentioned_unowned_prs"] == [f"{REPO}#3854", f"{REPO}#3855"]
