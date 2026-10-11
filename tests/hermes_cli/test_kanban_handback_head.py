"""A handoff that cites an older commit of its open PR is refused (t_e52337cb).

Measured 2026-10-10: Apollo caught three handbacks by hand whose cited sha was
not the PR head (#1857 b78653d4 vs 43a8e44a, #3447 64284fba vs a5a008c5, #3465
3e355903 vs 49c80152). 14-day replay: 21 of 3,279 sha-citing handoffs cited an
ancestor of the head the freshness gate read at handoff time, never the head.
Hermetic: no gh; the PR reader is a stub.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_handback_head as hh

# (repo, pr, cited sha in the handback, branch commits oldest -> head)
TODAY = [
    ("ANG-Ventures/hermes-agent", 1857, "b78653d4",
     ["b78653d432f8b1600ee23a2fbddcc273c480eba2", "0c4c0bc7a1", "4b611ac22b6db36090e434ca1f0ee0b00cff8b89",
      "43a8e44a295a9b0c16e2bde6eb3d19357127eab4"]),
    ("ANG-Ventures/hermes-home", 3447, "64284fba",
     ["64284fba539a838980b3d07eed61b92df8a60e5f", "a5a008c50bf187d523a4f50c21ebd8ecddb11775"]),
    ("ANG-Ventures/hermes-home", 3465, "3e355903",
     ["3e355903fe7c979c7189bf8fa13b6e6658609e95", "49c80152d5b41ef35acfc7e39070536a2651ee95"]),
]


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "all")
    monkeypatch.setattr(hh, "_CACHE", {})
    kb.init_db()
    return home


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def _stub(prs: dict):
    """prs: {(repo, n): commits} -> reader; a missing key is unreadable (None)."""
    calls = []

    def reader(repo, number):
        calls.append((repo, number))
        commits = prs.get((repo, number))
        if commits is None:
            return None
        return {"state": "open", "head": commits[-1], "commits": list(commits)}
    reader.calls = calls
    return reader


def _handoff(conn, monkeypatch, reader, summary, metadata=None):
    monkeypatch.setattr(hh, "_default_reader", lambda: reader)
    tid = kb.create_task(conn, title="slice: fix", assignee="builder")
    claimed = kb.claim_task(conn, tid)
    return tid, claimed.current_run_id


@pytest.mark.parametrize("repo,number,cited,commits", TODAY, ids=lambda v: str(v)[:12])
def test_todays_stale_heads_are_refused(kanban_home, monkeypatch, repo, number, cited, commits):
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        summary = f"PR {repo}#{number} (head {cited}): CI green, ready to land"
        with pytest.raises(hh.StaleHandbackHeadError) as exc:
            kb.request_review(conn, tid, summary=summary, reviewer="argus",
                              expected_run_id=run, with_reason=True)
        msg = str(exc.value)
        assert f"handback cites {cited}, PR head is {commits[-1][:12]}" in msg
        assert "re-verify at head or state why" in msg
        assert f"gh pr view {number} -R {repo} --json headRefOid" in msg
        assert kb.get_task(conn, tid).status == "running"
        assert _events(conn, tid, "completion_blocked_stale_head")[0]["stale"][0]["cited"] == cited
        assert _events(conn, tid, "review_requested") == []


def test_current_head_passes(kanban_home, monkeypatch):
    repo, number, _cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        ok, reason = kb.request_review(
            conn, tid, summary=f"{repo}#{number} at head {commits[-1][:8]}: CI green",
            reviewer="argus", expected_run_id=run, with_reason=True)
        assert ok, reason
        assert kb.get_task(conn, tid).status == "review"
        assert _events(conn, tid, "completion_blocked_stale_head") == []
        assert reader.calls == [(repo, number)]


def test_naming_both_old_and_current_head_passes(kanban_home, monkeypatch):
    repo, number, cited, commits = TODAY[0]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        summary = (f"{repo}#{number}: fix in {cited}; head is {commits[-1][:10]} after "
                   f"3 empty CI re-trigger commits, verified there")
        assert kb.request_review(conn, tid, summary=summary, reviewer="argus",
                                 expected_run_id=run) is True


def test_head_named_only_in_metadata_passes(kanban_home, monkeypatch):
    repo, number, cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        assert kb.request_review(
            conn, tid, summary=f"fix in {cited}", reviewer="argus", expected_run_id=run,
            metadata={"pr_url": f"https://github.com/{repo}/pull/{number}", "head": commits[-1]},
        ) is True


def test_gh_unreadable_passes_with_event(kanban_home, monkeypatch):
    repo, number, cited, _commits = TODAY[0]
    reader = _stub({})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        ok, reason = kb.request_review(
            conn, tid, summary=f"{repo}#{number} head {cited}", reviewer="argus",
            expected_run_id=run, with_reason=True)
        assert ok, reason
        assert _events(conn, tid, "head_check_unavailable") == [{"prs": [f"{repo}#{number}"]}]


def test_sha_not_on_pr_branch_and_run_ids_are_ignored(kanban_home, monkeypatch):
    repo, number, _cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        summary = f"{repo}#{number}: rebased onto main 45b87c76; CI run 38062422030 green"
        assert kb.request_review(conn, tid, summary=summary, reviewer="argus",
                                 expected_run_id=run) is True


def test_complete_path_is_gated_too(kanban_home, monkeypatch):
    """The open-PR auto-route (how all three measured handbacks arrived) runs via complete_task."""
    repo, number, cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, run = _handoff(conn, monkeypatch, reader, "")
        with pytest.raises(hh.StaleHandbackHeadError):
            kb.complete_task(conn, tid, summary=f"{repo}#{number} head {cited}: green",
                             expected_run_id=run)
        assert kb.get_task(conn, tid).status == "running"


def test_operator_force_is_not_gated(kanban_home, monkeypatch):
    repo, number, cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    with kb.connect() as conn:
        tid, _run = _handoff(conn, monkeypatch, reader, "")
        assert kb.request_review(conn, tid, summary=f"{repo}#{number} head {cited}",
                                 reviewer="argus", force=True) is True
        assert reader.calls == []


def test_reader_is_cached_for_60s(monkeypatch):
    monkeypatch.setattr(hh, "_CACHE", {})
    repo, number, cited, commits = TODAY[2]
    reader = _stub({(repo, number): commits})
    for _ in range(2):
        with pytest.raises(hh.StaleHandbackHeadError):
            hh.check(task_id="t_x", summary=f"{repo}#{number} {cited}", reader=reader)
    assert reader.calls == [(repo, number)]
    monkeypatch.setattr(hh.time, "time", lambda: 10**10)
    with pytest.raises(hh.StaleHandbackHeadError):
        hh.check(task_id="t_x", summary=f"{repo}#{number} {cited}", reader=reader)
    assert len(reader.calls) == 2
