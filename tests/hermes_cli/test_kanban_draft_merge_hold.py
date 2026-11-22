"""t_12401145: a merge-hold ``draft_ok`` sets a ledger hold, marks the PR ready and routes to review, not done."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_draft_merge_hold as mh
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40
REASON = "card rails: PR only, merge hold; Apollo lands"
HOLDER = "_review-hold"


@pytest.fixture(autouse=True)
def _fixture_owner_is_fleet(monkeypatch):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})


def _gh(*args):
    path = args[-1] if not args[0].startswith("-") else args[2]
    if path.endswith("/pulls/5"):
        return {"draft": True, "head": {"sha": HEAD, "ref": "someone/branch"}, "base": {"ref": "main"},
                "mergeable_state": "clean"}
    if "/compare/" in path:
        return {"behind_by": 0, "ahead_by": 1}
    return None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh)
    monkeypatch.setattr(fr, "spawn_evaluate", lambda *a: "/tmp/log")
    monkeypatch.setattr(kb, "configured_review_hold_assignee", lambda: HOLDER)
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "human:apollo")
    return home


@pytest.fixture
def calls(monkeypatch):
    """Ordered record of hold writes and ready flips; ``fail`` makes either fail."""
    log: list = []
    fail: dict = {}

    def hold(repo, number, why, by):
        log.append(("hold", repo, number, why, by))
        if fail.get("hold"):
            raise RuntimeError("label add: `hold` not on o/r#5 after the POST")

    def ready(repo, number):
        log.append(("ready", repo, number))
        return fail.get("ready", 0)

    monkeypatch.setattr(mh, "_default_hold", lambda: hold)
    monkeypatch.setattr(fr, "_default_ready", lambda: ready)
    return log, fail


def _claimed(conn):
    tid = kb.create_task(conn, title="slice", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _row(conn, tid):
    return conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()


def _events(conn, tid, kind):
    return [json.loads(r["payload"]) if r["payload"] else None for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _complete(conn, tid, run, **kw):
    return kb.complete_task(conn, tid, summary=f"done {PR_URL}", metadata={"pr_url": PR_URL},
                            expected_run_id=run, draft_ok=REASON, **kw)


def test_merge_hold_sets_hold_then_ready_and_routes_to_review_hold(board, calls):
    log, _ = calls
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        row = _row(conn, tid)
        assert (row["status"], row["assignee"]) == ("review", HOLDER)
        assert log == [("hold", "o/r", 5, REASON, f"worker run {run} ({tid})"), ("ready", "o/r", 5)]
        ev = _events(conn, tid, "completion_draft_override")[0]
        assert ev["kind"] == "merge_hold" and ev["merge_hold"]["o/r#5"]["ok"] is True
        assert _events(conn, tid, "completion_routed_to_review")[0]["open_prs"] == ["o/r#5"]
        meta = kb.latest_run(conn, tid).metadata
        assert meta["draft_merge_hold"]["o/r#5"]["hold"] == "set"
        assert "merge hold set (pr_hold ledger) and marked ready" in kb.latest_run(conn, tid).summary


def test_keep_draft_is_todays_override(board, calls):
    log, _ = calls
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run, draft_kind="keep_draft") is True
        assert _row(conn, tid)["status"] == "done"
        assert log == []
        assert _events(conn, tid, "completion_draft_override") == [
            {"prs": ["o/r#5"], "reason": REASON, "kind": "keep_draft"}]


def test_kill_switch_restores_done(board, calls, monkeypatch):
    monkeypatch.setenv("KANBAN_DRAFT_MERGE_HOLD", "0")
    log, _ = calls
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        assert _row(conn, tid)["status"] == "done" and log == []


def test_prose_mention_only_keeps_todays_override(board, calls):
    log, _ = calls
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary=f"see {PR_URL}", expected_run_id=run,
                                draft_ok=REASON) is True
        assert _row(conn, tid)["status"] == "done" and log == []


def test_hold_failure_never_flips_and_goes_to_the_human(board, calls):
    log, fail = calls
    fail["hold"] = True
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        row = _row(conn, tid)
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")
        assert [c[0] for c in log] == ["hold"]          # no ready flip without a hold
        res = _events(conn, tid, "completion_draft_override")[0]["merge_hold"]["o/r#5"]
        assert res["ok"] is False and res["hold"].startswith("failed:")


def test_ready_refused_stays_draft_and_goes_to_the_human(board, calls):
    log, fail = calls
    fail["ready"] = 21
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        row = _row(conn, tid)
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")
        assert "ready flip failed (ace_gate rc=21)" in kb.latest_run(conn, tid).summary


def test_invalid_draft_kind_refused_before_any_mutation(board, calls):
    log, _ = calls
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(ValueError):
            _complete(conn, tid, run, draft_kind="stacked")
        assert _row(conn, tid)["status"] == "running" and log == []
        assert _events(conn, tid, "completion_draft_override") == []


def test_no_hold_writer_fails_toward_draft(monkeypatch):
    monkeypatch.setattr(mh, "_default_hold", lambda: None)
    flips = []
    out = mh.apply(["o/r#5"], why="w", by="b", ready=lambda r, n: flips.append(n) or 0)
    assert out["o/r#5"]["ok"] is False and flips == []


def test_replay_route():
    own = {"o/r#5"}
    assert mh.replay_route({"prs": ["o/r#5"], "reason": "merge hold"}, own) == "review"
    assert mh.replay_route({"prs": ["o/r#5"], "reason": "x", "kind": "keep_draft"}, own) == "done"
    assert mh.replay_route({"prs": ["o/r#6"], "reason": "x"}, own) == "done"
    assert mh.replay_route({"prs": ["o/r#5"], "reason": "x", "held": {"o/r#5": "re-drafted"}}, own) == "done"


def test_cli_draft_kind_flag(board, calls):
    import argparse, contextlib, io, shlex
    from hermes_cli import kanban as kc
    log, _ = calls
    with kb.connect() as conn:
        tid, _ = _claimed(conn)
    wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
    parser = kc.build_parser(wrap.add_subparsers(dest="_top"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = kc.kanban_command(parser.parse_args(shlex.split(
            f"complete {tid} --summary 'done {PR_URL}' --metadata '{{\"pr_url\": \"{PR_URL}\"}}' "
            f"--draft-ok 'stacked on o/r#4' --draft-kind keep_draft")))
    assert rc == 0, out.getvalue()
    with kb.connect() as conn:
        assert _row(conn, tid)["status"] == "done"
    assert log == []
