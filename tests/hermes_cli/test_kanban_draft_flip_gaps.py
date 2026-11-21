"""Draft gate gaps (t_5c6c67c5).

(1) ``review_negative_result`` handbacks (the worker saying the close gate is NOT met) leave their own PR
    exactly as it is: the draft flag keeps self-declared-unfinished work out of every lander until the human
    lane decides (operator ruling 02:5x). The review notification names the flip for when the reviewer accepts.
(2) hermes-home#8231: ``lookup: failed (fail-open)`` routed a draft to review. The lookup is retried once;
    the card's own PR in a worker-draft repo that is still unreadable, or unreadable because there is no
    GitHub client at all, refuses (``DraftLookupError``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o", "u"})
    cfg = tmp_path / "worker-pr-draft.json"
    cfg.write_text(json.dumps({"enabled": True, "repos": ["O/R"]}))
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))
    monkeypatch.setattr(fr, "LOOKUP_RETRY_DELAY_S", 0)


def _gh(head_ref, *, draft=True, fail_first=0, state="open"):
    """Fake ``gh api``: the pulls/N read returns None ``fail_first`` times, then the PR."""
    calls = []
    left = {"fail": fail_first}

    def gh(*args):
        calls.append(args)
        path = args[-1]
        if "/pulls/" in path and "/update-branch" not in path:
            if left["fail"]:
                left["fail"] -= 1
                return None
            return {"draft": draft, "state": state, "head": {"sha": HEAD, "ref": head_ref},
                    "base": {"ref": "main"}, "mergeable_state": "clean"}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": [{"status": "completed", "conclusion": "skipped"}]}
        if path.endswith("/status"):
            return {"state": "success", "total_count": 1}
        return None
    gh.calls = calls
    return gh


def _pr_reads(gh):
    return [c for c in gh.calls if c[-1].endswith("/pulls/5")]


# --- (2) lookup retry, unit ------------------------------------------------


def test_lookup_retried_once_then_succeeds():
    gh = _gh("daedalus/t_5c6c67c5-x", draft=False, fail_first=1)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"o/r#5"})
    assert len(_pr_reads(gh)) == 2
    assert "lookup" not in rep["prs"]["o/r#5"]


def test_lookup_failing_twice_refuses_own_pr_in_worker_draft_repo():
    """Red on main: the first None was recorded `failed (fail-open)` and the handoff went to review."""
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    with pytest.raises(fr.DraftLookupError) as exc:
        fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=True, gh=gh,
                 armable={"o/r#5"}, evaluate=lambda *a: pytest.fail("evaluated"), event_land=True)
    assert exc.value.prs == ["o/r#5"]
    assert isinstance(exc.value, fr.DraftPrError)
    assert "failed twice" in str(exc.value) and "draft_ok" in str(exc.value)
    assert len(_pr_reads(gh)) == 2


def test_lookup_failure_on_unlisted_repo_still_fails_open():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    url = "https://github.com/u/other/pull/5"
    rep = fr.check(op.extract_pr_refs(url), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"u/other#5"})
    assert rep["prs"]["u/other#5"]["lookup"] == "failed (fail-open)"


def test_lookup_failure_on_mentioned_pr_fails_open():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable=set())
    assert rep["prs"]["o/r#5"]["lookup"] == "failed (fail-open)"


def test_lookup_failure_with_draft_ok_passes():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"o/r#5"}, draft_ok=True)
    assert rep["prs"]["o/r#5"]["lookup"] == "failed (fail-open)"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    return home


def _claimed(conn):
    tid = kb.create_task(conn, title="slice: soak", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind)).fetchall()]


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


NEG = f"Close-on read: NOT met. {PR_URL} stays open."


# --- (2) switch-off and no-client, unit (complete_task's gate) ------------------------------


def test_flip_disabled_owned_draft_is_refused_not_passed(monkeypatch):
    """Prism 6632d57be41c: ownership is classified independently of KANBAN_HANDOFF_DRAFT_FLIP; with
    flipping off an owned draft is refused, never flipped and never passed through."""
    monkeypatch.setenv("KANBAN_HANDOFF_DRAFT_FLIP", "0")
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False,
                 gh=_gh("daedalus/t_5c6c67c5-x"), armable={"o/r#5"},
                 ready=lambda r, n: pytest.fail("flipped with the switch off"))
    assert exc.value.prs == ["o/r#5"]


def test_flip_disabled_unreadable_own_pr_still_refuses(monkeypatch):
    monkeypatch.setenv("KANBAN_HANDOFF_DRAFT_FLIP", "0")
    with pytest.raises(fr.DraftLookupError):
        fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False,
                 gh=_gh("daedalus/t_5c6c67c5-x", fail_first=2), armable={"o/r#5"})


def test_no_github_client_refuses_own_pr_in_worker_draft_repo(monkeypatch):
    """Prism 6a3d114dfec9 (Lookup Bypass), red on main: _default_gh() None returned an empty report."""
    monkeypatch.setattr(fr, "_default_gh", lambda: None)
    with pytest.raises(fr.DraftLookupError) as exc:
        fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=True, armable={"o/r#5"},
                 evaluate=lambda *a: pytest.fail("evaluated"), event_land=True)
    assert exc.value.prs == ["o/r#5"]


@pytest.mark.parametrize("url", [PR_URL, "https://github.com/O/R/pull/5", "https://github.com/o/R/pull/5"])
def test_unread_refuses_matches_worker_draft_repo_case_insensitively(url):
    """t_6a6a7bc0: GitHub owner/repo are case-insensitive; the config lists "O/R", so every spelling of it
    refuses when unreadable (byte-exact would fail open), and an unlisted repo never does."""
    (ref,) = op.extract_pr_refs(url)
    assert fr._unread_refuses(ref, "o/r#5", None) is True
    assert fr._unread_refuses(ref, "O/R#5", {"o/r#5"}) is True
    assert fr._unread_refuses(ref, "o/r#5", set()) is False
    (other,) = op.extract_pr_refs("https://github.com/u/other/pull/5")
    assert fr._unread_refuses(other, "u/other#5", None) is False


def test_no_github_client_mentioned_or_unlisted_or_draft_ok_fails_open(monkeypatch):
    monkeypatch.setattr(fr, "_default_gh", lambda: None)
    refs = op.extract_pr_refs(PR_URL)
    assert fr.check(refs, task_id="t_5c6c67c5", allow_arm=False, armable=set())["prs"] == {}
    assert fr.check(refs, task_id="t_5c6c67c5", allow_arm=False, armable={"o/r#5"}, draft_ok=True)["prs"] == {}
    other = op.extract_pr_refs("https://github.com/u/other/pull/5")
    assert fr.check(other, task_id="t_5c6c67c5", allow_arm=False, armable={"u/other#5"})["prs"] == {}


# --- (1) negative-result handback: PR untouched, E2E through request_review -------------------


def test_e2e_negative_result_own_draft_goes_to_review_untouched(board, monkeypatch):
    """Operator ruling 02:5x: no GitHub read, no ace_gate call, no flip event; the review event carries the
    flip command for the reviewer."""
    monkeypatch.setattr(fr, "_default_gh", lambda: pytest.fail("read GitHub on a negative-result handback"))
    monkeypatch.setattr(fr, "_default_ready", lambda: pytest.fail("flipped a negative-result draft"))
    monkeypatch.setattr(fr, "ace_gate_ready", lambda *a: pytest.fail("flipped a negative-result draft"))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        ok, reason = kb.request_review(conn, tid, summary=NEG, metadata={"pr_url": PR_URL},
                                       expected_run_id=run, with_reason=True)
        assert ok is True, reason
        assert _status(conn, tid) == "review"
        assert _events(conn, tid, "review_negative_result")
        assert _events(conn, tid, "handoff_draft_flipped") == []
        assert _events(conn, tid, "completion_blocked_draft_pr") == []
        (req,) = _events(conn, tid, "review_requested")
        assert "ace_gate.py ready o/r 5" in req["draft_note"]
        assert "stays draft" in req["draft_note"]


def test_e2e_negative_result_without_pr_has_no_note(board):
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary="Close-on read: NOT met.", expected_run_id=run) is True
        (req,) = _events(conn, tid, "review_requested")
        assert "draft_note" not in req


def test_review_requested_ping_shows_the_draft_note():
    from types import SimpleNamespace
    from gateway import kanban_watchers_notifier as nt
    msg = nt._fmt_review_requested(SimpleNamespace(payload={"summary": "NOT met",
                                                            "draft_cmds": ["ace_gate.py ready o/r 5"]}),
                                   SimpleNamespace(head="h", title="T"))[0]
    assert "ace_gate.py ready o/r 5" in msg
    plain = nt._fmt_review_requested(SimpleNamespace(payload={"summary": "NOT met"}),
                                     SimpleNamespace(head="h", title="T"))[0]
    assert "ace_gate.py" not in plain


# --- Prism d0bec1125a49: only the card's OWN handoff PRs become flip commands ------------------


def test_draft_cmds_ignore_a_pr_the_summary_only_cites():
    from hermes_cli import kanban_negative_result as neg
    assert neg.draft_cmds(None) == []
    assert neg.draft_cmds({"summary_only": "Blocked by https://github.com/o/r/pull/5"}) == []
    assert neg.draft_cmds({"pr_url": "https://github.com/o/r/pull/6"}) == ["ace_gate.py ready o/r 6"]


def test_e2e_negative_result_citing_another_cards_pr_has_no_command(board):
    """Prism repro: a NOT-met summary citing o/r#5 (another task's dependency) yields no flip command."""
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary=f"Close-on read: NOT met. Blocked by {PR_URL}",
                                 expected_run_id=run) is True
        (req,) = _events(conn, tid, "review_requested")
        assert "draft_note" not in req and "draft_cmds" not in req


def test_armable_and_draft_cmds_share_one_ownership_filter():
    meta = {"pr_urls": ["https://github.com/o/r/pull/7"], "notes": "see https://github.com/o/r/pull/8"}
    assert [f"{r.repo}#{r.number}" for r in fr.handoff_refs(meta)] == ["o/r#7"]
    assert [f"{r.repo}#{r.number}" for r in fr.handoff_refs(None, survivor_pr="o/r#9")] == ["o/r#9"]


# --- Prism 11a86869f533: a ping never prints a sliced command ---------------------------------


LONG_REPO = "o/" + "r" * 28
LONG_URLS = [f"https://github.com/{LONG_REPO}/pull/{n}" for n in (12341, 12342, 12343, 12345)]


def test_long_command_list_is_counted_in_the_ping_and_posted_whole_on_the_card(board):
    import re
    from types import SimpleNamespace
    from gateway import kanban_watchers_notifier as nt
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary=NEG, metadata={"pr_urls": LONG_URLS},
                                 expected_run_id=run) is True
        (req,) = _events(conn, tid, "review_requested")
        assert req["draft_cmds"] == [f"ace_gate.py ready {LONG_REPO} {n}" for n in (12341, 12342, 12343, 12345)]
        msg = nt._fmt_review_requested(SimpleNamespace(payload=req), SimpleNamespace(head="h", title="T"))[0]
        named = {int(n) for n in re.findall(r"ace_gate\.py ready \S+ (\d+)", msg)}
        assert named <= {12341, 12342, 12343, 12345}
        assert "4 flip commands, see the card" in msg
        bodies = [b for (b,) in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,))]
        (body,) = [b for b in bodies if "flip each one" in b]
        for c in req["draft_cmds"]:
            assert c in body.splitlines()


def test_short_command_list_prints_whole_and_posts_no_comment(board):
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary=NEG, metadata={"pr_url": PR_URL}, expected_run_id=run) is True
        assert not [1 for (b,) in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,))
                    if "flip each one" in b]
