"""``--superseded-by <card>``: another card's merged PR superseded this card's closed PR (t_6a853d1c).

t_eb5bf009's fleet-ops-scripts#350 was closed ``CLOSED: SUPERSEDED-BY #351``. #351 is t_be729571's PR,
merged as f02c16eb, and edits the same spawn() block a different way. No commit carries #350's content
and #351 does not name t_eb5bf009, so every close form refused and the card could only be archived.

The case is accepted only when ALL hold: (1) that card is done, (2) its PR merged to the default
branch, (3) the closed PR's own thread carries ``CLOSED: SUPERSEDED-BY <that PR>``, (4) the two PRs'
changed files intersect. Each is broken on its own below and the close still refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

REPO = "ANG-Ventures/fleet-ops-scripts"
CLOSED_PR = f"https://github.com/{REPO}/pull/350"
MERGED_PR = f"https://github.com/{REPO}/pull/351"
MERGE_SHA = "f02c16eb44bf667d407e6e325890cb4dabece1fd"
SHARED = "ci-runners/src/ci-runner-pool.sh"
FILES = {350: {SHARED, "ci-runners/test_pool_work_bind.py"},
         351: {SHARED, "ci-runners/test_pool_spawn_path.py", ".github/workflows/ci.yml"}}
RECORD = ("CLOSED: SUPERSEDED-BY #351 -- #351 (merged f02c16eb) moves the same per-spawn wipe off the "
          "spawn path [card t_eb5bf009]")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_" + "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


_SUP: dict = {}


def _stub(monkeypatch, *, merged=True, on_default=True, record=RECORD, files=None, head_ref=None):
    """The t_eb5bf009 shape on GitHub, every oracle stubbed. Each kwarg breaks one condition.
    #351's head branch names the superseding card unless ``head_ref`` overrides it (fos#351 is on
    ``daedalus/t_be729571-spawn-path``). #352 is a third card's closed PR with the same record and
    files as #350 (the chain case)."""
    files = dict(FILES if files is None else files)
    files.setdefault(352, files.get(350, set()))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _SUP.clear()

    def query(repo, n):
        if n == 351:
            out = {"state": "MERGED", "merge_commit_sha": MERGE_SHA} if merged else {"state": "CLOSED"}
            return dict(out, head_ref=head_ref or f"daedalus/{_SUP.get('id')}-spawn-path")
        return {"state": "CLOSED"} if n in (350, 352) else {"state": op.NOT_FOUND}
    monkeypatch.setattr(op, "_default_query", lambda: query)
    monkeypatch.setattr(op, "sha_on_default", lambda repo, sha: on_default)
    monkeypatch.setattr(op, "_default_close_record", lambda: None)
    monkeypatch.setattr(op, "_default_terminal_record", lambda: None)
    monkeypatch.setattr(op, "_default_superseded_targets",
                        lambda: (lambda repo, n: op._SUPERSEDED_LINE_RE.findall(record) if n in (350, 352) else []))
    monkeypatch.setattr(op, "_default_files", lambda: (lambda repo, n: set(files.get(n, ()))))


def _cards(conn, *, superseder_status="done"):
    """t_be729571 (merged #351) and t_eb5bf009 (running, own PR #350 closed)."""
    sup = kb.create_task(conn, title="spawn path", assignee="worker")
    kb.claim_task(conn, sup)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (sup,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"pr_url": "%s"}' % MERGED_PR, run))
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (superseder_status, sup))
    _SUP["id"] = sup
    tid = kb.create_task(conn, title="pacer", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                 ('{"pr_url": "%s", "changed_files": ["%s"]}' % (CLOSED_PR, SHARED), run))
    conn.commit()
    return sup, tid, run


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_t_eb5bf009_shape_closes_superseded_with_both_prs_in_the_event(board, monkeypatch):
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True
        assert _status(conn, tid) == "done"
        last = kb.list_runs(conn, tid)[-1]
        assert last.outcome == "superseded"
        assert last.metadata["survivor"]["kind"] == "ref"
        assert last.metadata["survivor"]["refs"][0]["sha"] == MERGE_SHA
        done = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1].payload
        assert done["superseded_by"] == sup
        assert done["superseded_by_card"] == {"card": sup, "prs": [
            {"closed": f"{REPO}#350", "merged": f"{REPO}#351", "sha": MERGE_SHA}]}


def _refused(conn, tid, run, sup):
    with pytest.raises(op.ClosedUnmergedPrError, match="CLOSED WITHOUT MERGE"):
        kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup)
    assert _status(conn, tid) == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "completion_blocked_closed_unmerged_pr" in kinds and "completed" not in kinds


@pytest.mark.parametrize("status", ["review", "running", "blocked", "archived"])
def test_mutation_1_superseding_card_not_done_refuses(board, monkeypatch, status):
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn, superseder_status=status)
        _refused(conn, tid, run, sup)


@pytest.mark.parametrize("kw", [{"merged": False}, {"on_default": False}])
def test_mutation_2_superseding_pr_not_merged_to_default_refuses(board, monkeypatch, kw):
    _stub(monkeypatch, **kw)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


@pytest.mark.parametrize("record", [
    "",                                                     # no close record at all
    "CLOSED: SUPERSEDED-BY #999 -- some other PR",          # names a different PR
    "superseded by #351, see there",                        # prose, not the contract line
    "CLOSED: ABANDONED -- #351 does it",                    # a different token
])
def test_mutation_3_closed_pr_lacks_superseded_by_that_pr_refuses(board, monkeypatch, record):
    _stub(monkeypatch, record=record)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_mutation_4_disjoint_changed_files_refuses(board, monkeypatch):
    _stub(monkeypatch, files={350: {"a.sh"}, 351: {"b.sh"}})
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_unreadable_files_refuses(board, monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(op, "_default_files", lambda: (lambda repo, n: None))
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_unknown_card_and_self_pointer_refuse(board, monkeypatch):
    _stub(monkeypatch)
    with kb.connect() as conn:
        _sup, tid, run = _cards(conn)
        _refused(conn, tid, run, "t_00000000")
        _refused(conn, tid, run, tid)


def test_a_bare_pr_pointer_is_unchanged(board, monkeypatch):
    """The other paths are unchanged: `--superseded-by owner/repo#351` (no card) still meets the old gates,
    here the merged-PR token, which accepts a merged PR exactly as before."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        _sup, tid, run = _cards(conn)
        cover = []
        op.enforce_not_closed_unmerged(tid, metadata={"pr_url": CLOSED_PR}, superseded_by=f"{REPO}#351",
                                       query_fn=op._default_query(), card_covered_out=cover)
        assert cover == []


def test_superseding_card_parses_one_card_only():
    assert op.superseding_card("t_be729571") == "t_be729571"
    assert op.superseding_card("t_be729571 (fos#351)") == "t_be729571"
    assert op.superseding_card("t_be729571 or t_eb5bf009") is None
    assert op.superseding_card("ANG-Ventures/fleet-ops-scripts#351") is None
    assert op.superseding_card(None) is None


def _renamed_superseder(conn, sup, new="t_be729571"):
    conn.execute("UPDATE tasks SET id=? WHERE id=?", (new, sup))
    conn.execute("UPDATE task_runs SET task_id=? WHERE task_id=?", (new, sup))
    conn.commit()
    return new


def test_merged_pr_on_the_superseding_cards_branch_covers(board, monkeypatch):
    """The real case: fos#351's head is ``daedalus/t_be729571-spawn-path``."""
    _stub(monkeypatch, head_ref="daedalus/t_be729571-spawn-path")
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        sup = _renamed_superseder(conn, sup)
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True


@pytest.mark.parametrize("head_ref", ["feature/no-card-id", "apollo/x", "aegis/fix-thing"])
def test_merged_pr_on_a_no_card_or_operator_branch_is_nobodys_own_and_refuses(board, monkeypatch, head_ref):
    """Prism fe296d065135: rule 2 needs POSITIVE ownership, the head branch names the superseding card
    (branch_names_card, as the open-PR route). A no-card or operator branch belongs to no card."""
    _stub(monkeypatch, head_ref=head_ref)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        sup = _renamed_superseder(conn, sup)
        _refused(conn, tid, run, sup)


@pytest.mark.parametrize("head_ref", ["feature/no-card-id", "daedalus/t_be729571-spawn-path"])
def test_auto_routed_only_mention_is_not_the_superseders_own_pr(board, monkeypatch, head_ref):
    """Prism 4cacc8c8c4b5: a legacy run whose only record of #351 is ``auto_routed_open_prs`` (handoff prose
    merely mentioned it) never makes #351 that card's own work. The no-card branch is refused by the branch
    test; the card's own branch by authored_pr_refs reading pr_url / own_prs only."""
    _stub(monkeypatch, head_ref=head_ref)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?",
                     ('{"auto_routed_open_prs": ["%s"]}' % MERGED_PR, sup))
        sup = _renamed_superseder(conn, sup)
        _refused(conn, tid, run, sup)


def test_merged_pr_whose_branch_names_another_card_is_not_the_superseders_own(board, monkeypatch):
    """Rule 2 "its own PR": a recorded PR whose head branch names a DIFFERENT card was never its work."""
    _stub(monkeypatch, head_ref="daedalus/t_deadbeef-other-work")
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_supersession_does_not_chain_through_a_survivor_ref(board, monkeypatch):
    """Prism 7004158a070b: once B closes --superseded-by A, A's #351 sits in B's survivor.refs[].pr. A third
    card C (closed #352, `CLOSED: SUPERSEDED-BY #351`, same files) must not close --superseded-by B on it."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, b, b_run = _cards(conn)
        assert kb.complete_task(conn, b, expected_run_id=b_run, superseded_by=sup) is True
        assert any("#351" in str(r.get("pr")) for r in kb.list_runs(conn, b)[-1].metadata["survivor"]["refs"])
        c = kb.create_task(conn, title="third", assignee="worker")
        kb.claim_task(conn, c)
        c_run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (c,)).fetchone()[0]
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     ('{"pr_url": "https://github.com/%s/pull/352"}' % REPO, c_run))
        conn.commit()
        _refused(conn, c, c_run, b)
        # control: C's real superseder still covers it
        assert kb.complete_task(conn, c, expected_run_id=c_run, superseded_by=sup) is True


def test_authored_pr_refs_drops_survivor_evidence_only():
    md = {"pr_url": "o/r#1", "own_prs": ["o/r#2"],
          "survivor": {"refs": [{"pr": "o/r#3"}], "claims": [{"pr": "o/r#4"}]}}
    assert op.authored_pr_refs(md) == ["o/r#1", "o/r#2"]
    assert op.authored_pr_refs({"auto_routed_open_prs": ["o/r#5"]}) == []
    assert set(op.recorded_pr_refs(md)) == {"o/r#1", "o/r#2", "o/r#3", "o/r#4"}
    assert op.authored_pr_refs(None) == []


def _fake_gh(pages_by_number):
    """subprocess.run stand-in for `gh api --paginate .../pulls/N/files --jq <expr>`: emits what gh would
    print for that jq expr, one array per page for the array form, one path per line for the `.[] |` form."""
    import subprocess as sp

    def run(argv, **kw):
        n = int(next(a for a in argv if "/pulls/" in a).split("/pulls/")[1].split("/")[0])
        jq = argv[argv.index("--jq") + 1]
        pages = pages_by_number.get(n, [])
        import json as _j
        out = "".join(_j.dumps(p) + "\n" for p in pages) if jq.startswith("[") else \
            "".join(p + "\n" for page in pages for p in page)
        return sp.CompletedProcess(argv, 0, stdout=out, stderr="")
    return run


def test_trailing_space_twin_path_is_a_different_file_and_refuses(board, monkeypatch):
    """Prism 340172f6f023: `src/runner.py ` and `src/runner.py` are two files; pr_files keeps both verbatim."""
    _stub(monkeypatch)
    gh = _fake_gh({350: [["src/runner.py "]], 351: [["src/runner.py"]], 7: [["a.sh", "old/a.sh"], ["b c.sh"]]})

    def files(repo, n):  # the real pr_files, with only its own gh call faked
        with monkeypatch.context() as m:
            m.setattr(op.subprocess, "run", gh)
            return op.pr_files(repo, n)
    assert files(REPO, 350) == {"src/runner.py "}
    assert files(REPO, 7) == {"a.sh", "old/a.sh", "b c.sh"}  # every page, verbatim
    monkeypatch.setattr(op, "_default_files", lambda: files)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_cross_repo_superseder_with_identical_paths_refuses(board, monkeypatch):
    """Rule 4 is same-repo only: README.md in another repo is not this PR's README.md."""
    other = "ANG-Ventures/hermes-home"
    _stub(monkeypatch, record=f"CLOSED: SUPERSEDED-BY {other}#351", files={350: {SHARED}, 351: {SHARED}})
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?",
                     ('{"pr_url": "https://github.com/%s/pull/351"}' % other, sup))
        conn.commit()
        _refused(conn, tid, run, sup)


def test_sha_on_default_reads_github_com_like_the_pr_reads(monkeypatch):
    """Prism fe268dfe02ca: with GH_HOST set, the ancestry proof must not come from another host."""
    import json as _j
    import subprocess as sp
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        out = {"default_branch": "main"} if argv[-1].endswith("/r") else {"status": "behind"}
        return sp.CompletedProcess(argv, 0, stdout=_j.dumps(out), stderr="")
    monkeypatch.setenv("GH_HOST", "ghe.example")
    monkeypatch.setattr(op.subprocess, "run", run)
    assert op.sha_on_default("o/r", MERGE_SHA) is True
    assert len(seen) == 2 and all(a[:4] == ["gh", "api", "--hostname", "github.com"] for a in seen)


def test_supersession_cover_completes_but_buys_no_workspace_deletion(board, monkeypatch, tmp_path):
    """Prism c5429c8af4f5: the cover proves #351 REPLACED #350, not that it holds this card's bytes.
    P9 shape: recorded child repo ``a`` vanishes under a replacement root that ignores it. The
    supersession close still completes, but its cleanup must keep the unpublished bytes."""
    import shutil
    import subprocess as sp

    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_survivor as ks

    def git(repo, *args):
        sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    def init(repo):
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "QA")
        git(repo, "config", "user.email", "qa@example.invalid")

    def commit(repo, name, text):
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", name)

    monkeypatch.setattr(ks, "_temporary_roots", lambda: [tmp_path / "temporary"])
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        a = ws / "a"
        init(a)
        commit(a, "lost_impl.py", f"UNPUBLISHED_{tid}\n")
        kbw.set_workspace_path(conn, tid, ws)
        ks.record_baseline(conn, tid, ws)
        assert set(ks._state(conn, tid)[0]) == {"a"}
        shutil.rmtree(a / ".git")
        init(ws)
        commit(ws, ".gitignore", "a/\n")
        commit(ws, "replacement.txt", "replacement B\n")

        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True
        assert _status(conn, tid) == "done"
        assert (a / "lost_impl.py").exists(), "supersession cleanup reaped this card's unpublished bytes"
        assert (a / "lost_impl.py").read_text(encoding="utf-8") == f"UNPUBLISHED_{tid}\n"
        assert "unbound" in ks._state(conn, tid)[1]
        assert kb.list_runs(conn, tid)[-1].metadata["survivor"]["refs"][0]["unbound"] is True


def test_supersession_close_never_removes_the_workspace_itself(board, monkeypatch):
    """Prism 20f49b2e3092: no changed_files claim, a ``{}`` baseline and a loose file with no
    ``.git``. preserve() takes the no-claim path and would hand the workspace to the reaper.
    complete_task skips the removal for a card-covered close and records a retention hold."""
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_survivor as ks

    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"pr_url": "%s"}' % CLOSED_PR, run))
        conn.commit()
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, ws)
        ks.record_baseline(conn, tid, ws)
        assert ks._state(conn, tid)[0] == {}
        (ws / "draft.py").write_text(f"DRAFT_{tid}", encoding="utf-8")
        assert not (ws / ".git").exists()

        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True
        assert _status(conn, tid) == "done"
        assert (ws / "draft.py").exists(), "supersession completion deleted the workspace"
        assert (ws / "draft.py").read_text(encoding="utf-8") == f"DRAFT_{tid}"
        held = ks._state(conn, tid)[1]
        assert held and "superseded_by_card" in held
        assert "workspace_held" in [e.kind for e in kb.list_events(conn, tid)]


def test_supersession_hold_commits_with_done_and_survives_a_crash_after_commit(board, monkeypatch):
    """Prism ff7e3c99981d: the hold is the only protection on the no-claim path, so it must be
    durable together with ``done``. A crash at the first step after the done commit must leave
    ``held_reason`` set, and a later archive must keep the loose file."""
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_survivor as ks

    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"pr_url": "%s"}' % CLOSED_PR, run))
        conn.commit()
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, ws)
        ks.record_baseline(conn, tid, ws)
        (ws / "draft.py").write_text(f"DRAFT_{tid}", encoding="utf-8")

        real = kb._clear_failure_counter

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash right after the done commit")
        monkeypatch.setattr(kb, "_clear_failure_counter", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup)
        monkeypatch.setattr(kb, "_clear_failure_counter", real)

    with kb.connect() as conn:
        assert _status(conn, tid) == "done"
        held = ks._state(conn, tid)[1]
        assert held and "superseded_by_card" in held, "done committed without its retention hold"
        kb.archive_task(conn, tid)
    assert (ws / "draft.py").exists(), "archive deleted the unpublished file of a held workspace"
    assert (ws / "draft.py").read_text(encoding="utf-8") == f"DRAFT_{tid}"


def _shared_refused(conn, tid, run, sup):
    with pytest.raises(op.SharedWorkspaceSupersessionError,
                       match=f"superseded-by {sup} is not supported on a shared workspace"):
        kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup)
    assert _status(conn, tid) == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "completion_blocked_closed_unmerged_pr" in kinds and "completed" not in kinds


def test_workspace_of_reader_supersession_refuses(board, monkeypatch):
    """Prism fad0ffab0ac9: the completing card R reads owner O's workspace (workspace_of=O). R's hold
    protects only R's id, so archiving O later would delete the shared directory. Refused."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        owner = kb.create_task(conn, title="owner", assignee="worker")
        conn.execute("UPDATE tasks SET workspace_of=? WHERE id=?", (owner, tid))
        conn.commit()
        _shared_refused(conn, tid, run, sup)


@pytest.mark.parametrize("reader_status", ["todo", "ready", "running", "blocked", "review"])
def test_owner_with_a_live_reader_supersession_refuses(board, monkeypatch, reader_status):
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        reader = kb.create_task(conn, title="reader", assignee="worker")
        conn.execute("UPDATE tasks SET workspace_of=?, status=? WHERE id=?", (tid, reader_status, reader))
        conn.commit()
        _shared_refused(conn, tid, run, sup)


@pytest.mark.parametrize("reader_status", ["done", "archived"])
def test_owner_whose_readers_are_closed_still_covers(board, monkeypatch, reader_status):
    """Only a LIVE reader refuses: once the readers are closed, the owner's own hold covers it."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        reader = kb.create_task(conn, title="reader", assignee="worker")
        conn.execute("UPDATE tasks SET workspace_of=?, status=? WHERE id=?", (tid, reader_status, reader))
        conn.commit()
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True
        assert _status(conn, tid) == "done"
