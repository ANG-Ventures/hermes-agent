"""Prose is not provenance: a handoff that MENTIONS another card's open PR is not gated on it.

Incident t_f1b135a8 (2026-10-11): its one PR, hermes-agent#1904, merged. Its handoff prose named
#1902/#1903 (and later #1893) as PRs that belong to other cards. The completion router harvested
every qualified ref from the text, wrote "survivor PR open; card closes on merged=true (#1902,
#1893, #1903)" and bounced the card to review twice. The fixture is the real handoff text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kanban_handoff_t_f1b135a8.json").read_text())
REPO = "ANG-Ventures/hermes-agent"
CARD = "t_f1b135a8"


def _oracle(states: dict, heads: dict):
    calls = []

    def query(repo, number):
        calls.append((repo, number))
        state = states.get(number)
        if state is None:
            return None
        out = {"state": state}
        if heads.get(number):
            out["head_ref"] = heads[number]
        return out

    query.calls = calls
    return query


def _heads(card_for_1904: str = CARD) -> dict:
    heads = {int(n): ref for n, ref in FIXTURE["heads"].items()}
    heads[1904] = heads[1904].replace(CARD, card_for_1904)
    return heads


def _numbers(refs):
    return [r.number for r in refs]


def test_run1_handoff_gates_only_on_its_own_open_pr():
    run = FIXTURE["runs"]["23979"]
    q = _oracle({1904: "OPEN", 1902: "OPEN", 1903: "OPEN", 1897: "MERGED"}, _heads())
    unowned: list = []
    got = op.open_pr_refs(run["summary"], metadata=run["metadata"], query_fn=q,
                          task_id=CARD, unowned=unowned)
    assert _numbers(got) == [1904]
    assert _numbers(unowned) == [1902, 1903]


def test_run2_handoff_with_merged_own_pr_and_not_this_card_list_does_not_gate():
    run = FIXTURE["runs"]["24059"]
    q = _oracle({1904: "MERGED", 1902: "OPEN", 1893: "OPEN", 1903: "OPEN"}, _heads())
    unowned: list = []
    got = op.open_pr_refs(run["summary"], metadata=run["metadata"], query_fn=q,
                          task_id=CARD, unowned=unowned)
    assert got == []
    assert sorted(_numbers(unowned)) == [1893, 1902, 1903]


def test_declared_pr_gates_even_when_its_branch_names_another_card():
    q = _oracle({5: "OPEN"}, {5: "daedalus/t_00000000-other"})
    got = op.open_pr_refs("shipped", metadata={"pr_url": f"https://github.com/{REPO}/pull/5"},
                          query_fn=q, task_id=CARD)
    assert _numbers(got) == [5]
    got = op.open_pr_refs("shipped", survivor_pr=f"{REPO}#5", query_fn=q, task_id=CARD)
    assert _numbers(got) == [5]


@pytest.mark.parametrize("head", [f"daedalus/{CARD}-fix", "feature/no-card-id", None])
def test_prose_pr_still_gates_unless_its_branch_names_a_different_card(head):
    """Own-card branch, card-less branch, unknown head: fail closed (t_1bd02e0b)."""
    q = _oracle({7: "OPEN"}, {7: head})
    got = op.open_pr_refs(f"see {REPO}#7", query_fn=q, task_id=CARD)
    assert _numbers(got) == [7]


def test_unreadable_prose_pr_still_gates():
    q = _oracle({}, {})
    assert _numbers(op.open_pr_refs(f"see {REPO}#7", query_fn=q, task_id=CARD)) == [7]


def test_without_task_id_behaviour_is_unchanged():
    run = FIXTURE["runs"]["23979"]
    q = _oracle({1904: "OPEN", 1902: "OPEN", 1903: "OPEN", 1897: "MERGED"}, _heads())
    assert _numbers(op.open_pr_refs(run["summary"], metadata=run["metadata"], query_fn=q)) == [
        1904, 1902, 1903]


def test_branch_names_other_card():
    assert op.branch_names_other_card("daedalus-opus/t_90887924-hard-classes", CARD)
    assert not op.branch_names_other_card(f"daedalus/{CARD}-ssh-target-position", CARD)
    assert not op.branch_names_other_card("main", CARD)
    assert not op.branch_names_other_card(None, CARD)
    assert not op.branch_names_other_card("feature/t_90887924x", CARD)


# --- E2E through complete_task on a temp board -----------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn):
    tid = kb.create_task(conn, title="host_reach fix", assignee="daedalus")
    kb.claim_task(conn, tid)
    return tid


def test_e2e_run1_routes_on_own_pr_only_and_records_mentions(kanban_home, monkeypatch):
    run = FIXTURE["runs"]["23979"]
    with kb.connect() as conn:
        tid = _card(conn)
        q = _oracle({1904: "OPEN", 1902: "OPEN", 1903: "OPEN", 1897: "MERGED"}, _heads(tid))
        monkeypatch.setattr(op, "_default_query", lambda: q)
        assert kb.complete_task(conn, tid, summary=run["summary"], metadata=run["metadata"]) is True
        assert kb.get_task(conn, tid).status == "review"
        meta = kb.latest_run(conn, tid).metadata
        assert meta["auto_routed_open_prs"] == [f"{REPO}#1904"]
        assert meta["mentioned_unowned_prs"] == [f"{REPO}#1902", f"{REPO}#1903"]
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (tid,))]
        assert bodies == [f"survivor PR open; card closes on merged=true ({REPO}#1904)"]
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "completion_unowned_pr_mentions" in kinds


def test_e2e_run2_merged_own_pr_goes_done(kanban_home, monkeypatch):
    run = FIXTURE["runs"]["24059"]
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    with kb.connect() as conn:
        tid = _card(conn)
        q = _oracle({1904: "MERGED", 1902: "OPEN", 1893: "OPEN", 1903: "OPEN"}, _heads(tid))
        monkeypatch.setattr(op, "_default_query", lambda: q)
        assert kb.complete_task(conn, tid, summary=run["summary"], metadata=run["metadata"]) is True
        assert kb.get_task(conn, tid).status == "done"
        meta = kb.latest_run(conn, tid).metadata
        assert "auto_routed_open_prs" not in meta
        assert sorted(meta["mentioned_unowned_prs"]) == sorted(
            f"{REPO}#{n}" for n in (1902, 1893, 1903))
