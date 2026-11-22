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


# --- the real writer seam (pr_hold_set) against a stand-in pr_hold.py under the board root -------------------------
# Mirrors hermes-home scripts/lib/pr_hold.py's contract: _root() (the readers' ledger root), held() (newest row wins,
# "operator hold (<by> at <at>): <why>"), write("set", ...). READER and WRITE roots are pinned per test.
_FAKE_PR_HOLD = """
import json, os
LEDGER = os.path.join("state", "pr-hold.jsonl")
READER, WRITE = @READER@, @WRITE@
def _root(root=None):
    return root or READER
def held(repo, pr, root=None):
    path = os.path.join(_root(root), LEDGER)
    why = None
    if not os.path.exists(path):
        return None
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if (r["repo"].casefold(), r["pr"]) == (repo.casefold(), int(pr)):
            why = ("operator hold (%s at %s): %s" % (r["by"], r["at"], r["why"] or "-")
                   if r["action"] == "set" else None)
    return why
def write(action, repo, pr, why, by, root=None, **_):
    path = os.path.join(root or WRITE, LEDGER)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": "2026-11-21T00:00:00Z", "repo": repo, "pr": int(pr), "action": action,
                            "by": by, "why": why}) + "\\n")
    if os.environ.get("FAKE_PR_HOLD_LABEL_FAIL"):   # the real write(): row appended first, then the label
        raise RuntimeError("label add rc=1 HTTP 403")
    return {}
"""


@pytest.fixture
def real_writer(board, monkeypatch):
    """complete_task -> mh.pr_hold_set (the production writer) with a stand-in pr_hold.py; ready is recorded."""
    root = kb.kanban_home()
    flips: list = []

    def install(reader, write):
        lib = root / "scripts" / "lib"
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "pr_hold.py").write_text(_FAKE_PR_HOLD.replace("@READER@", repr(str(reader))).replace("@WRITE@", repr(str(write))))

    monkeypatch.setattr(mh, "_default_hold", lambda: mh.pr_hold_set)
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda repo, n: flips.append((repo, n)) or 0))
    return root, install, flips


def _ledger(root):
    path = Path(root) / "state" / "pr-hold.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_real_writer_on_the_board_root_holds_reads_back_and_flips(real_writer):
    root, install, flips = real_writer
    install(root, root)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        assert tuple(_row(conn, tid)) == ("review", HOLDER)
    assert flips == [("o/r", 5)]
    assert [(r["repo"], r["pr"], r["action"], r["why"]) for r in _ledger(root)] == [("o/r", 5, "set", REASON)]


def test_pool_host_shape_reader_root_elsewhere_no_write_no_flip(real_writer, tmp_path):
    """The kernel's pr_hold resolves a ledger that is not the board's (a pool host's own root): refuse, no flip."""
    root, install, flips = real_writer
    pool = tmp_path / ("long-ci-tmp-" * 16) / "pool-host-root"   # CI tmp paths are long: the cause must survive
    install(pool, pool)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        assert tuple(_row(conn, tid)) == ("review", "human:apollo")
        res = _events(conn, tid, "completion_draft_override")[0]["merge_hold"]["o/r#5"]
    assert flips == [] and _ledger(pool) == [] and _ledger(root) == []
    assert res["ok"] is False and res["hold"].startswith(f"failed: {mh.CAUSE_READER_ROOT}")


def test_write_landing_in_a_non_reader_root_fails_read_back_no_flip(real_writer, tmp_path):
    """The write lands in a ledger the readers do not read: held() reads nothing back, so the PR stays draft."""
    root, install, flips = real_writer
    elsewhere = tmp_path / "local-ledger"
    install(root, elsewhere)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        assert tuple(_row(conn, tid)) == ("review", "human:apollo")
        res = _events(conn, tid, "completion_draft_override")[0]["merge_hold"]["o/r#5"]
    assert _ledger(elsewhere) and _ledger(root) == []
    assert flips == [] and res["ok"] is False and res["hold"].startswith(f"failed: {mh.CAUSE_READ_BACK}")


def test_ready_raising_is_recorded_not_raised(monkeypatch):
    def boom(repo, n):
        raise OSError("ace_gate.py missing")
    out = mh.apply(["o/r#5"], why="w", by="b", hold=lambda *a: None, ready=boom)
    assert out["o/r#5"]["hold"] == "set" and out["o/r#5"]["ok"] is False
    assert out["o/r#5"]["ready"].startswith("raised:")


def test_label_failure_after_the_row_records_held_label_missing_no_flip(real_writer, monkeypatch):
    """write() appends the ledger row, then the label add fails: the event says held (label missing), matching the
    ledger, and the PR is not flipped (Apollo note, 11-21 22:1x)."""
    root, install, flips = real_writer
    install(root, root)
    monkeypatch.setenv("FAKE_PR_HOLD_LABEL_FAIL", "1")
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert _complete(conn, tid, run) is True
        assert tuple(_row(conn, tid)) == ("review", "human:apollo")
        res = _events(conn, tid, "completion_draft_override")[0]["merge_hold"]["o/r#5"]
    assert [r["action"] for r in _ledger(root)] == ["set"] and flips == []
    assert res["ok"] is False and res["hold"].startswith(mh.CAUSE_LABEL_MISSING) and "403" in res["hold"]
    assert "label add failed" in mh.note({"o/r#5": res})[0]


def test_write_failure_with_no_row_stays_failed(board, monkeypatch):
    """A write that raised and left no hold readable is a plain failure, never reported as held."""
    class Mod:
        @staticmethod
        def _root(root=None):
            return str(kb.kanban_home())
        @staticmethod
        def write(*a, **k):
            raise RuntimeError("ledger append failed")
        @staticmethod
        def held(*a, **k):
            return None
    monkeypatch.setattr(mh, "_load_pr_hold", lambda root: Mod)
    out = mh.apply(["o/r#5"], why="w", by="b", hold=mh.pr_hold_set, ready=lambda *a: 0)
    assert out["o/r#5"]["ok"] is False and out["o/r#5"]["hold"] == "failed: ledger append failed"
