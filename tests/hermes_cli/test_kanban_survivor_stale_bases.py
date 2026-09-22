"""A vanished recorded repository must still have a reachable remedy.

`preserve()` records `bases` at dispatch, from the git checkout that was in the
workspace then. When that checkout is later gone but the workspace DIRECTORY
survives (evidence, logs, qa-output), the recorded-repository guard fired
unconditionally -- on the branch where `explicit` is never consulted. The
operator escape hatch documented in the error (`--survivor-pr` / `--survivor-ref`)
was therefore structurally unreachable for that state.

These tests pin the remedy AND the guard: an operator-named, remote-VERIFIED
survivor satisfies the card; anything less still fails closed.
"""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

HEAD = "a1" * 20
PR = "example/project#68"
STALE = "af0d85e37470550d554abb89a5cd51039dbbe358"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    """Answer remote lookups affirmatively; the gate must not rely on a network failure.

    ``state["missing"]`` makes the PR lookup fail the way a nonexistent PR does,
    and ``state["tips"]`` is what a bare ``git ls-remote <url>`` advertises --
    empty by default, so a ``--survivor-ref`` resolves to nothing. Both are
    stubbed rather than left to the network so these tests never reach out.
    """
    state = {"state": "OPEN", "headRefOid": HEAD, "mergeCommit": None,
             "missing": False, "tips": ""}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            if state["missing"]:
                return subprocess.CompletedProcess(args, 1, b"", b"not found")
            payload = {k: state[k] for k in ("state", "headRefOid", "mergeCommit")}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")
        # `_ext` shells out as `git ls-remote ...`; `_git` always passes `-C`.
        if args[0] == "git" and len(args) > 1 and args[1] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, state["tips"].encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def stale_card(conn, *, title="review lane", loose=True):
    """A card whose recorded repo is gone but whose workspace dir survives."""
    tid = kb.create_task(conn, title=title)
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    if loose:
        (ws / "qa-output").mkdir(parents=True, exist_ok=True)
        (ws / "qa-output" / "verdict.md").write_text("APPROVED\n")
    kb.set_workspace_path(conn, tid, ws)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({".": STALE})),
        )
    return tid, ws


def test_stale_bases_with_a_verified_survivor_pr_completes(board, remote):
    """The case that was impossible: dir exists, repo gone, operator names a PR."""
    tid, ws = stale_card(board)

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "ref"
    ref = saved["refs"][0]
    # The recorded survivor is the remote-verified external ref, not the dead base.
    assert ref["sha"] == HEAD and ref["pr"] == PR and ref["external"] is True
    assert ref["sha"] != STALE
    assert kb.get_task(board, tid).status == "done"


def test_stale_bases_without_an_explicit_survivor_still_refuses(board, remote):
    """Guard preserved: no operator survivor means no completion, workspace held."""
    tid, ws = stale_card(board)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="approved")

    assert "recorded repository missing" in str(excinfo.value)
    # The error must now name the remedy it previously withheld.
    assert "--survivor-pr" in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"
    assert (ws / "qa-output" / "verdict.md").is_file(), "held workspace must survive"


def test_stale_bases_with_an_unverifiable_survivor_pr_still_refuses(board, remote):
    """An operator CLAIM is not authority: a closed PR buys nothing."""
    remote["state"] = "CLOSED"
    tid, ws = stale_card(board)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    assert "could not verify --survivor-pr" in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"
    assert (ws / "qa-output" / "verdict.md").is_file()


def test_stale_bases_does_not_fall_through_to_text_mining(board, remote):
    """Teeth: the relaxation is for OPERATOR flags only, never a mined hint.

    Without this, forcing the external path could let `discover()` mine a PR out
    of the handoff and convert a fail-closed HOLD into a delete -- the exact
    regression test_kanban_survivor_authority.py exists to prevent.
    """
    tid, ws = stale_card(board)
    kb.add_comment(board, tid, "reviewer", f"context: unrelated {PR} landed earlier")

    with pytest.raises(ValueError):
        kb.complete_task(board, tid, result=f"see {PR} at {HEAD}", summary="approved")

    assert kb.get_task(board, tid).status != "done"
    assert (ws / "qa-output" / "verdict.md").is_file()


# --- CLI surface: a refused terminal transition must be LOUD ----------------

def _cli(board, monkeypatch, argv):
    import argparse
    import contextlib

    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: contextlib.nullcontext(board))
    return cli.kanban_command(parser.parse_args(argv))


def test_cli_refusal_exits_non_zero_with_the_reason_and_hint(board, remote, monkeypatch, capsys):
    """A caller must be able to tell 'completed' from 'refused' by exit code."""
    tid, _ = stale_card(board)

    rc = _cli(board, monkeypatch, ["kanban", "complete", tid, "--summary", "ok"])
    err = capsys.readouterr().err

    assert rc != 0, "a refused terminal transition must not report success"
    assert "recorded repository missing" in err
    assert "--survivor-pr" in err and "--survivor-ref" in err
    assert kb.get_task(board, tid).status != "done"


def test_cli_successful_completion_exits_zero_and_says_so(board, remote, monkeypatch, capsys):
    """Teeth for the test above: the happy path must stay quiet and green."""
    tid, _ = stale_card(board)

    rc = _cli(board, monkeypatch,
              ["kanban", "complete", tid, "--summary", "ok", "--survivor-pr", PR])
    out = capsys.readouterr()

    assert rc == 0
    assert f"Completed {tid}" in out.out
    assert kb.get_task(board, tid).status == "done"


def partial_loss_card(conn, tmp_path, monkeypatch):
    """One recorded repo published and on disk ("kept"), one vanished ("gone")."""
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    def git(repo, *args):
        return subprocess.run([
            "git", "-C", str(repo), *args
        ], capture_output=True, check=True).stdout.decode().strip()

    tid = kb.create_task(conn, title="partial loss")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    kept = ws / "kept"
    kept.mkdir(parents=True)
    git(kept, "init", "-b", "main")
    git(kept, "config", "user.name", "Test")
    git(kept, "config", "user.email", "test@example.invalid")
    (kept / "a.py").write_text("value = 1\n")
    git(kept, "add", ".")
    git(kept, "commit", "-m", "base")
    git(kept, "init", "--bare", str(tmp_path / "kept.git"))
    git(kept, "remote", "add", "origin", str(tmp_path / "kept.git"))
    git(kept, "push", "origin", "HEAD:main")
    kb.set_workspace_path(conn, tid, ws)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": git(kept, "rev-parse", "HEAD"), "gone": STALE})),
        )
    return tid, ws


def test_partial_loss_keeps_both_the_surviving_repo_and_the_operator_ref(board, remote, tmp_path, monkeypatch):
    """Only SOME recorded repos vanished: neither survivor may be dropped.

    The surviving repo resolves its own remote ref, which on its own would
    satisfy the completion and silently discard the operator's ref for the repo
    that is gone -- leaving that work pointed at nothing.
    """
    tid, _ = partial_loss_card(board, tmp_path, monkeypatch)

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    saved = kb.latest_run(board, tid).metadata["survivor"]
    by_repo = {ref["repository"]: ref for ref in saved["refs"]}
    assert by_repo["gone"]["pr"] == PR, "the lost repo must carry the operator's verified ref"
    assert by_repo["kept"]["remote"] == "origin", "the surviving repo keeps its own ref"
    # Exactly one ref per recorded repository. Falling through to the external
    # path instead of resolving here appends a SECOND copy of the operator ref
    # under repository ".", which does not correspond to anything on disk.
    assert len(saved["refs"]) == 2, saved["refs"]
    assert set(by_repo) == {"gone", "kept"}, saved["refs"]


# --- PARTIAL LOSS x UNVERIFIABLE SURVIVOR ----------------------------------
#
# The intersection the two blocks above each miss. The test immediately above
# covers partial loss with a VERIFIED survivor;
# `test_stale_bases_with_an_unverifiable_survivor_pr_still_refuses` covers an
# unverifiable survivor on the ALL-GONE path. Nothing pinned partial loss WITH
# an unverifiable survivor, and that is the branch with no verification of its
# own: it builds `recovered` straight out of `explicit` and feeds it into the
# `len(refs) == len(repos) + 1` arithmetic that decides completion. The ONLY
# thing standing between an operator's CLOSED/fake PR and a durably stored
# survivor is the single `_verified_explicit()` raise upstream at
# `preserve()`. These tests pin that dependency, so a refactor that moves,
# inlines, narrows, or short-circuits that raise goes RED here instead of
# silently completing partial-loss cards on a survivor that points nowhere.


def _assert_partial_loss_refused(board, tid, ws, excinfo):
    """Refusal is not enough: nothing unverifiable may be stored either."""
    assert "survivor_unavailable" in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"
    assert (ws / "kept" / "a.py").is_file(), "held workspace must survive"
    row = board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?", (tid,)
    ).fetchone()
    saved = json.loads(row[0]) if row and row[0] else None
    # Shape, not just outcome: no unverifiable ref recorded, and in particular
    # no bogus `repository: "."` entry from a fall-through to the external path.
    assert saved is None, saved
    run = kb.latest_run(board, tid)
    assert run is None or (run.metadata or {}).get("survivor") is None, run.metadata


def test_partial_loss_with_a_closed_survivor_pr_still_refuses(board, remote, tmp_path, monkeypatch):
    """A CLOSED-unmerged PR is not authority, partial loss or not."""
    remote["state"] = "CLOSED"
    tid, ws = partial_loss_card(board, tmp_path, monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    assert "could not verify --survivor-pr" in str(excinfo.value)
    _assert_partial_loss_refused(board, tid, ws, excinfo)


def test_partial_loss_with_a_nonexistent_survivor_pr_still_refuses(board, remote, tmp_path, monkeypatch):
    """A PR that does not resolve at all buys nothing either."""
    remote["missing"] = True
    tid, ws = partial_loss_card(board, tmp_path, monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    assert "could not verify --survivor-pr" in str(excinfo.value)
    _assert_partial_loss_refused(board, tid, ws, excinfo)


def test_partial_loss_with_an_unresolvable_survivor_ref_still_refuses(board, remote, tmp_path, monkeypatch):
    """Cover the OTHER flag: a SHA that is not a tip on the named remote."""
    tid, ws = partial_loss_card(board, tmp_path, monkeypatch)
    claim = f"https://github.com/example/project.git#{'b2' * 20}"

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="approved", survivor_ref=claim)

    assert "could not verify --survivor-ref" in str(excinfo.value)
    _assert_partial_loss_refused(board, tid, ws, excinfo)


def test_partial_loss_with_a_verified_survivor_ref_completes(board, remote, tmp_path, monkeypatch):
    """Teeth for the three above: the same `--survivor-ref` path DOES complete
    once the SHA is an advertised tip, so those refusals are about verification
    and not about the flag being inert on this branch."""
    sha = "b2" * 20
    remote["tips"] = f"{sha}\trefs/heads/work\n"
    tid, _ = partial_loss_card(board, tmp_path, monkeypatch)
    claim = f"https://github.com/example/project.git#{sha}"

    assert kb.complete_task(board, tid, summary="approved", survivor_ref=claim)

    saved = kb.latest_run(board, tid).metadata["survivor"]
    by_repo = {ref["repository"]: ref for ref in saved["refs"]}
    assert by_repo["gone"]["sha"] == sha
    assert set(by_repo) == {"gone", "kept"}, saved["refs"]


# --- the SATISFIED claim must not be re-litigated during reclamation -------
#
# `bases` is never rewritten on a successful completion, so the stale-bases
# check above re-ran on the cleanup pass (`remove_workspace_dir` ->
# `preserve(cleanup=True)`). A reaper passes no `--survivor-pr`, so `explicit`
# was None there and the check re-raised on a claim the operator had ALREADY
# honoured -- and `_hold()` rewrote the `held_reason` that `_record()` had just
# NULLed. The card ended `done`, permanently HELD, citing the very remedy that
# had been used, and its workspace never reclaimed.


def test_a_recorded_survivor_clears_the_hold_it_satisfied(board, remote):
    """The bug: a SUCCESSFUL --survivor-pr completion left held_reason set."""
    tid, ws = stale_card(board, loose=False)

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    row = board.execute(
        "SELECT held_reason, survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert row["held_reason"] is None, (
        "the cleanup re-check re-held a card whose survivor was already recorded"
    )
    assert json.loads(row["survivor"])["refs"][0]["pr"] == PR
    assert kb.get_task(board, tid).status == "done"


def test_the_satisfied_workspace_is_actually_reclaimed(board, remote):
    """Teeth for the assertion above: a held workspace is never reclaimed."""
    tid, ws = stale_card(board, loose=False)

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    assert not ws.exists(), "a card with a recorded survivor must not leak its dir"


def test_reclamation_consults_the_recorded_survivor_not_the_dead_base(board, remote):
    """The reaper's own call must succeed on its own, with no operator flag."""
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)
    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)
    ws.mkdir(parents=True, exist_ok=True)  # a reaper re-examining a retained dir

    # No survivor_pr: exactly what `remove_workspace_dir` passes.
    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["refs"][0]["pr"] == PR, "cleanup must reuse the recorded survivor"
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None


def test_reclamation_still_holds_when_no_survivor_vouches_for_the_lost_repo(board, remote):
    """REGRESSION: the relaxation is keyed on the RECORDED survivor, not on
    `cleanup` itself. A card with no survivor for the vanished repo still holds.
    """
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)
    # A recorded survivor that vouches for a DIFFERENT repository than the one
    # `bases` says vanished. Suppressing the check on `cleanup` alone would let
    # this through; keying on coverage must not.
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor",
            (tid, json.dumps({"kind": "ref", "refs": [{"repository": "elsewhere", "sha": HEAD}]})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert "recorded repository missing" in str(excinfo.value)
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is not None


def test_loose_evidence_is_still_not_reapable_on_a_satisfied_claim(board, remote):
    """REGRESSION (PR #795's 4xP1): a ref vouches for repositories, never for
    loose files beside them. Honouring the satisfied claim must not hand the
    reaper evidence that no survivor covers -- the workspace still HOLDS.
    """
    tid, ws = stale_card(board)  # loose=True: qa-output/verdict.md
    evidence = ws / "qa-output" / "verdict.md"

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    assert evidence.is_file(), "loose reviewer evidence must survive cleanup"
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"]
    assert held is not None and "outside any repository" in held, (
        "the hold must now name the loose files, not the already-honoured claim"
    )


def test_reclamation_does_not_mine_a_new_survivor_out_of_the_handoff(board, remote):
    """REGRESSION: reclamation reuses a RECORDED survivor; it never discovers
    one. Re-mining there would turn a fail-closed HOLD into a delete.
    """
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)
    kb.add_comment(board, tid, "reviewer", f"unrelated {PR} landed earlier")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET result = ? WHERE id = ?",
                      (f"see {PR} at {HEAD}", tid))

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert "recorded repository missing" in str(excinfo.value)


# --- the relaxation must not trade unvouched evidence for a reclaim --------
#
# FleetReview P1 (#842 @ 3ca4b600): `_loose_files()` has exactly ONE call site,
# inside the `elif claimed:` arm -- i.e. only when the in-tree capture produced
# nothing. On a PARTIAL loss `repos` is non-empty, the surviving repo resolves
# its own ref, and `refs` was pre-seeded with `carried`, so the ref branch is
# satisfied and control returns before that arm is ever reached. The relaxation
# therefore cleared a hold that had been protecting loose reviewer evidence, and
# `remove_workspace_dir` went on to `rmtree` it. Pre-diff the same call raised.


def _seed_published_repo(path, bare, *, name="a.py"):
    """A clean repo whose HEAD is published to a real bare remote."""
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, check=True).stdout.decode().strip()

    path.mkdir(parents=True)
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (path / name).write_text("value = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("init", "--bare", str(bare))
    git("remote", "add", "origin", str(bare))
    git("push", "origin", "HEAD:main")
    return git("rev-parse", "HEAD")


def test_partial_loss_at_cleanup_still_holds_for_loose_unvouched_evidence(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: the relaxation must consult `_loose_files()` on EVERY exit
    that can reach `rmtree`, not only on the total-loss `elif claimed:` arm.

    `gone` is vouched for by the recorded operator ref, `kept` survives and
    resolves its own ref -- but `qa-output/verdict.md` sits beside them and no
    survivor covers it. The workspace must stay HELD.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="partial loss with loose evidence")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "kept.git")
    evidence = ws / "qa-output" / "verdict.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("APPROVED\n")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, survivor = excluded.survivor",
            (tid, json.dumps({"kept": kept_head, "gone": STALE}),
             json.dumps({"kind": "ref", "refs": [
                 {"repository": "gone", "sha": HEAD, "pr": PR, "external": True}]})),
        )

    # Exactly what the reaper passes: cleanup, no operator flag.
    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert "outside any repository" in str(excinfo.value), str(excinfo.value)
    assert evidence.is_file(), "loose reviewer evidence must survive the relaxation"
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is not None


def test_partial_loss_at_cleanup_still_reclaims_when_nothing_is_loose(
        board, remote, tmp_path, monkeypatch):
    """Teeth for the test above: the guard must key on LOOSE FILES, not on
    partial loss itself. With no unvouched evidence the reclaim still succeeds
    and still carries the recorded ref for the vanished repo.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="partial loss, clean workspace")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "clean-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, survivor = excluded.survivor",
            (tid, json.dumps({"kept": kept_head, "gone": STALE}),
             json.dumps({"kind": "ref", "refs": [
                 {"repository": "gone", "sha": HEAD, "pr": PR, "external": True}]})),
        )

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    by_repo = {ref["repository"]: ref for ref in out["refs"]}
    assert by_repo["gone"]["pr"] == PR
    assert by_repo["kept"]["remote"] == "origin"
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None


def test_a_bundle_vouched_missing_repo_is_carried_into_the_rewritten_survivor(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: `_vouched_repositories()` counts bundles, but the
    carry-forward copied only `refs`. A repo whose ONLY survivor is a stored
    bundle satisfied `missing <= vouched`, then vanished from the survivor that
    `_record()` rewrote -- leaving the recovery index claiming `kind: "ref"`
    for a card whose unpushed history lives in an orphaned bundle attachment.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="bundle-vouched loss")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    a_head = _seed_published_repo(ws / "a", tmp_path / "a.git")
    kb.set_workspace_path(board, tid, ws)
    bundle = {"repository": "b", "path": str(tmp_path / "implementation-0.bundle"),
              "sha256": "f" * 64, "bytes": 42}
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, survivor = excluded.survivor",
            (tid, json.dumps({"a": a_head, "b": STALE}),
             json.dumps({"kind": "bundle", "notice": "NOT PUSHED", "bundles": [bundle],
                         "refs": [{"repository": "a", "sha": a_head, "remote": "origin"}]})),
        )

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert [b["repository"] for b in out.get("bundles") or ()] == ["b"], out
    assert out["bundles"][0]["path"] == bundle["path"], "the stored bundle pointer must survive"
    assert out["kind"] == "bundle", (
        "a survivor still holding unpushed history must not be relabelled `ref`"
    )
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert [b["repository"] for b in saved["bundles"]] == ["b"], saved


# --- reclamation must never SHRINK the recovery index ----------------------
#
# FleetReview P1 (#842 @ 3afa9086), three instances of one class: the cleanup
# re-capture can only see repositories still on disk, and `_record()` then
# overwrites the row with exactly what it captured. Anything the recorded
# survivor vouched for that is NOT on disk is therefore dropped unless it is
# explicitly carried forward. The first fix keyed that carry-forward on
# `set(bases) - keys`, which misses every repository `bases` never knew.


def _seed_survivor(conn, tid, ws, bases, survivor):
    kb.set_workspace_path(conn, tid, ws)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, survivor = excluded.survivor",
            (tid, json.dumps(bases), json.dumps(survivor)),
        )


def test_reclamation_carries_a_survivor_for_a_repo_cloned_after_dispatch(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: the carry-forward must be keyed on what is ABSENT FROM DISK,
    not on `bases`.

    `bases` is recorded once, before dispatch. A repository the worker cloned
    afterwards is in the recorded survivor and in NO `bases` entry, so
    `missing = set(bases) - keys` is empty, the relaxation never runs, and the
    cleanup re-capture rewrites the row without it -- silently orphaning the
    bundle that holds that repository's only unpushed history.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="repo cloned after dispatch")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "late-kept.git")
    bundle = {"repository": "cloned", "path": str(tmp_path / "implementation-0.bundle"),
              "sha256": "f" * 64, "bytes": 42}
    # `cloned` is in the survivor, NOT in bases: it did not exist at dispatch.
    _seed_survivor(board, tid, ws, {"kept": kept_head},
                   {"kind": "bundle", "notice": "NOT PUSHED", "bundles": [bundle],
                    "refs": [{"repository": "kept", "sha": kept_head, "remote": "origin"}]})

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert [b["repository"] for b in out.get("bundles") or ()] == ["cloned"], out
    assert out["bundles"][0]["path"] == bundle["path"]
    assert out["kind"] == "bundle", "unpushed history must not be relabelled `ref`"
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert [b["repository"] for b in saved["bundles"]] == ["cloned"], saved


def test_one_operator_survivor_cannot_vouch_for_two_vanished_repositories(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: `--survivor-pr` names ONE remote. Stamping it onto every
    vanished repository records provenance that is false for all but one of
    them -- the same recovery-index corruption as the dropped bundle above,
    written deliberately. The operator flag covered one repository, so a
    multi-repository loss is an INCOMPLETE claim and must fail closed.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="two vanished repositories")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "two-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone1": STALE, "gone2": STALE})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws, survivor_pr=PR)

    assert "gone1" in str(excinfo.value) and "gone2" in str(excinfo.value), str(excinfo.value)
    assert ws.is_dir(), "the workspace must be retained, not reaped, on an incomplete claim"
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is not None


def test_a_single_vanished_repository_is_still_covered_by_the_operator_flag(
        board, remote, tmp_path, monkeypatch):
    """Teeth for the test above: the refusal keys on the COUNT of vanished
    repositories, not on partial loss. One lost repo still has its remedy.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="one vanished repository")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "one-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone": STALE})),
        )

    out = survivor.preserve(board, tid, workspace=ws, survivor_pr=PR)

    by_repo = {ref["repository"]: ref for ref in out["refs"]}
    assert by_repo["gone"]["pr"] == PR
    assert set(by_repo) == {"gone", "kept"}, out["refs"]


def test_reclamation_never_erases_a_recorded_survivor_it_cannot_recapture(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: the no-survivor `else` wrote NULL over a RECORDED survivor.

    `_record()` does `SET survivor = excluded.survivor`, so reaching the `else`
    with a survivor already on the row erased the recovery index AND returned
    None, handing `remove_workspace_dir` a green light to `rmtree` loose
    evidence the recorded ref never vouched for.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="recorded survivor, nothing to recapture")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    evidence = ws / "qa-output" / "verdict.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("APPROVED\n")
    recorded = {"kind": "ref", "refs": [
        {"repository": ".", "sha": HEAD, "pr": PR, "external": True}]}
    _seed_survivor(board, tid, ws, {}, recorded)

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert "outside any repository" in str(excinfo.value), str(excinfo.value)
    assert evidence.is_file(), "loose evidence no survivor covers must not be reapable"
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert saved == recorded, "the recorded survivor must survive the re-capture"


def test_a_recorded_survivor_with_nothing_loose_is_kept_and_reclaimable(
        board, remote, tmp_path, monkeypatch):
    """Teeth for the test above: the hold keys on LOOSE FILES, not on having a
    recorded survivor. With nothing unvouched the recorded survivor is returned
    unchanged (never NULLed) and the workspace reclaims.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="recorded survivor, clean workspace")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    recorded = {"kind": "ref", "refs": [
        {"repository": ".", "sha": HEAD, "pr": PR, "external": True}]}
    _seed_survivor(board, tid, ws, {}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out == recorded, out
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert saved == recorded
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None


def _seed_unpublished_empty_diff_repo(path, bare):
    """A repo whose HEAD is UNPUBLISHED but whose tree equals the published base.

    `_remote_survivor()` finds no published ancestor (HEAD is not on the
    remote), and `_snapshot()` against that base is empty -- so the capture
    loop appends no ref, no patch and no bundle for it. That is the only shape
    that reaches the `elif claimed:` arm with `repos` non-empty, i.e. the shape
    in which `carried` is actually populated.
    """
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, check=True).stdout.decode().strip()

    path.mkdir(parents=True)
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (path / "a.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("init", "--bare", str(bare))
    git("remote", "add", "origin", str(bare))
    git("push", "origin", "HEAD:main")
    base = git("rev-parse", "HEAD")
    # An unpublished commit with the SAME tree: not an ancestor of any remote
    # tip, and `git diff base` is empty.
    git("commit", "--allow-empty", "-m", "unpublished, empty tree delta")
    assert git("rev-parse", "HEAD") != base
    return base


def test_the_carried_ref_is_never_recorded_twice(board, remote, tmp_path, monkeypatch):
    """REGRESSION: the carried entries seed `refs` AND were re-added verbatim by
    the external merge on the `elif claimed:` arm, so one repository was
    recorded twice -- and a consumer reading the recovery index could not tell
    which entry was current.

    The previous version of this test left NOTHING on disk, which makes
    `carried` provably empty (the carry-forward is gated on `if repos:`) and
    the merge a no-op -- it could not fail. This drives the real path: `repos`
    is non-empty but the in-tree capture yields nothing for it.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="carried ref plus an empty in-tree capture")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_base = _seed_unpublished_empty_diff_repo(ws / "kept", tmp_path / "dup-kept.git")
    recorded = {"kind": "ref", "refs": [
        {"repository": "gone", "sha": HEAD, "pr": PR, "external": True},
        {"repository": "kept", "sha": kept_base, "remote": "origin"}]}
    _seed_survivor(board, tid, ws, {"kept": kept_base, "gone": STALE}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    repos = [ref["repository"] for ref in out["refs"]]
    assert sorted(repos) == ["gone", "kept"], out["refs"]
    assert len(repos) == len(set(repos)), f"duplicate refs: {out['refs']}"
    assert next(r for r in out["refs"] if r["repository"] == "gone")["pr"] == PR


# --- reclamation must not shrink a PATCH-shaped recovery index -------------
#
# FleetReview P1 (#842 @ 7afe1b9e): `_vouched_repositories()` counts refs and
# bundles, and deliberately NOT patches -- a patch keys no repository in the
# recorded row at all (only the `implementation.json` sidecar does). So the
# repository-keyed carry-forward can never reach a patch pointer, and the
# cleanup re-capture rewrites the row as `kind: "ref"`, i.e. "everything is
# pushed", while the only copy of that uncommitted work is an orphaned
# attachment.


def _patch_survivor(tmp_path, refs):
    return {"kind": "patch", "notice": "NOT PUSHED", "bundles": [], "refs": list(refs),
            "path": str(tmp_path / "implementation.patch"), "sha256": "e" * 64, "bytes": 512,
            "sidecar": str(tmp_path / "implementation.json")}


def test_reclamation_carries_a_patch_pointer_the_recapture_cannot_reproduce(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: `kept` was dirty at completion and captured as a patch; by
    cleanup its tree is clean and published, so the re-capture produces only a
    ref. The patch pointer must survive, not be relabelled away.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="patch survivor, clean at cleanup")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "patch-kept.git")
    recorded = _patch_survivor(tmp_path, [{"repository": "kept", "sha": kept_head,
                                           "remote": "origin"}])
    _seed_survivor(board, tid, ws, {"kept": kept_head}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["path"] == recorded["path"], "the stored patch pointer must survive"
    assert out["sha256"] == recorded["sha256"] and out["bytes"] == recorded["bytes"]
    assert out["sidecar"] == recorded["sidecar"]
    assert out["kind"] == "patch", "a survivor still holding unpushed work must not become `ref`"
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert saved["path"] == recorded["path"], saved


def test_reclamation_carries_a_patch_pointer_for_a_repo_cloned_after_dispatch(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: the genuinely distinct case -- the repository-keyed
    carry-forward and the patch carry-forward firing TOGETHER.

    The previous version of this test was byte-for-byte the preceding
    scenario: one on-disk `kept` and a patch keyed on `kept`, so `absent` was
    empty, the relaxation never ran, and `_unshrunk()` alone did the work. It
    asserted strictly less and added zero coverage while its docstring
    advertised a trigger it never built.

    Here the recorded survivor holds BOTH a ref for `extra` -- cloned after
    dispatch, so it is in no `bases` entry -- AND a patch pointer. `absent` is
    non-empty, so the relaxation carries `extra` forward, and `_unshrunk()`
    must ALSO keep the patch.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="patch survivor, repo cloned after dispatch")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "late-patch-kept.git")
    recorded = _patch_survivor(tmp_path, [
        {"repository": "kept", "sha": kept_head, "remote": "origin"},
        {"repository": "extra", "sha": HEAD, "pr": PR, "external": True}])
    # `bases` knew only `kept`; `extra` was cloned after dispatch and is gone.
    _seed_survivor(board, tid, ws, {"kept": kept_head}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["path"] == recorded["path"] and out["sidecar"] == recorded["sidecar"], out
    assert out["kind"] == "patch", out
    by_repo = {ref["repository"]: ref for ref in out["refs"]}
    assert set(by_repo) == {"kept", "extra"}, out["refs"]
    assert by_repo["extra"]["pr"] == PR, (
        "the repository-keyed carry-forward must fire alongside the patch carry-forward"
    )


def test_a_clean_recapture_without_a_recorded_patch_stays_a_ref(
        board, remote, tmp_path, monkeypatch):
    """Teeth for the two tests above: the carry-forward keys on the RECORDED
    survivor holding a patch pointer, never on `cleanup` itself. With nothing
    unpushed recorded, reclamation still records a plain `ref`.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="no recorded patch")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "plain-kept.git")
    _seed_survivor(board, tid, ws, {"kept": kept_head},
                   {"kind": "ref", "refs": [{"repository": "kept", "sha": kept_head,
                                             "remote": "origin"}]})

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["kind"] == "ref", out
    assert "path" not in out and "sidecar" not in out, out


# --- a multi-repository loss must have a REACHABLE remedy ------------------
#
# FleetReview P1 (#842 @ 7afe1b9e): the `len(missing) > 1` refusal was correct
# about the corruption (one flag names one remote) but left the state with no
# exit at all -- the completion always raised and the workspace was HELD
# forever, while the message still advertised `--survivor-pr`. The flags are
# now repeatable and repository-qualified, so the operator can record TRUE
# per-repository provenance instead of choosing between a lie and a dead end.


def test_qualified_operator_survivors_cover_two_vanished_repositories(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION: the state the `len(missing) > 1` guard made unreachable."""
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="two vanished repositories, qualified claims")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "qual-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone1": STALE, "gone2": STALE})),
        )

    out = survivor.preserve(board, tid, workspace=ws,
                            survivor_pr=[f"gone1={PR}", "gone2=example/other#12"])

    by_repo = {ref["repository"]: ref for ref in out["refs"]}
    assert set(by_repo) == {"gone1", "gone2", "kept"}, out["refs"]
    assert by_repo["gone1"]["pr"] == PR
    assert by_repo["gone2"]["pr"] == "example/other#12", (
        "each vanished repository must carry its OWN remote, not a copy of the first"
    )
    assert by_repo["kept"]["remote"] == "origin"
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None


def test_a_partially_qualified_claim_still_fails_closed(board, remote, tmp_path, monkeypatch):
    """Teeth: the remedy is per-repository provenance, not a bypass. A claim
    that names only one of two vanished repositories is INCOMPLETE and must
    still hold -- the uncovered one would otherwise be dropped silently.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="partially qualified claim")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "partial-qual-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone1": STALE, "gone2": STALE})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws, survivor_pr=[f"gone1={PR}"])

    assert "gone2" in str(excinfo.value) and "gone1" not in str(excinfo.value), str(excinfo.value)
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is not None


def test_the_multi_repository_refusal_names_a_remedy_that_actually_works(
        board, remote, tmp_path, monkeypatch):
    """The unqualified refusal is preserved, but its hint must be usable: the
    exact form it prints has to be the form that succeeds.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="unqualified claim, two vanished repositories")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "hint-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone1": STALE, "gone2": STALE})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws, survivor_pr=PR)

    message = str(excinfo.value)
    assert "gone1" in message and "gone2" in message
    assert "<repository>=<claim>" in message, message
    assert "--survivor-pr gone1=" in message, (
        "the hint must name the qualified form, not a flag shape that cannot satisfy this state"
    )

    # And that form actually completes the card.
    out = survivor.preserve(board, tid, workspace=ws,
                            survivor_pr=[f"gone1={PR}", f"gone2={PR}"])
    assert {ref["repository"] for ref in out["refs"]} == {"gone1", "gone2", "kept"}


def test_a_repository_qualifier_never_swallows_a_bare_claim(board, remote):
    """Teeth for the qualifier parser: a bare PR slug and a bare URL#sha both
    contain characters the prefix grammar must refuse, or the historical
    single-claim shape would be silently reinterpreted.
    """
    from hermes_cli import kanban_survivor as survivor

    assert survivor._split_qualifier(PR) == (None, PR)
    assert survivor._split_qualifier("https://github.com/o/r.git#" + HEAD) == (
        None, "https://github.com/o/r.git#" + HEAD)
    # The only cases that can actually be MIS-split are claims that contain an
    # `=` of their own. The prefix grammar must refuse every one of them.
    for bare in (f"https://host/p.git?trk=1#{HEAD}", f"ssh://git@host/p.git?a=b#{HEAD}",
                 f"git@github.com:o/r.git?x=y#{HEAD}"):
        assert survivor._split_qualifier(bare) == (None, bare), bare
    assert survivor._split_qualifier(f"sub/dir={PR}") == ("sub/dir", PR)
    assert survivor._split_qualifier(f".={PR}") == (".", PR)


def test_mixing_a_qualified_and_an_unqualified_claim_is_refused(board, remote):
    """An unqualified claim means "the one lost repository"; combining it with
    a qualified one leaves which repository it covers ambiguous.
    """
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws, survivor_pr=[PR, f"gone={PR}"])

    assert "ambiguous" in str(excinfo.value), str(excinfo.value)


# --- FleetReview round 4 (#842 @ 0062a34f) ---------------------------------
#
# Six P1 + two P2, all one theme: the "may KEEP or EXTEND, never SHRINK" rule
# was enforced on some exits and some shapes, not all of them.


def _bundle_survivor(tmp_path, repository, refs=()):
    return {"kind": "bundle", "notice": "NOT PUSHED", "refs": list(refs),
            "bundles": [{"repository": repository,
                         "path": str(tmp_path / f"implementation-{repository}.bundle"),
                         "sha256": "b" * 64, "bytes": 256}]}


def test_reclamation_keeps_a_bundle_for_a_repo_that_is_still_on_disk(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION (P1 :356): `_unshrunk()` carried only the four scalar patch
    pointers, and the repository-keyed carry-forward fires only for repos
    ABSENT from disk -- so a recorded bundle for a repository that is STILL
    there had nothing protecting it.

    `kept` had no published ancestor at completion, so its only copy of the
    unpushed work is a bundle. By cleanup a base IS published, the re-capture
    emits a plain ref, `bundles` comes back empty, and the row was rewritten
    `kind: "ref"` -- "everything is pushed" -- over the bundle attachment.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="bundle survivor, repo still on disk")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "bundle-kept.git")
    recorded = _bundle_survivor(tmp_path, "kept")
    _seed_survivor(board, tid, ws, {"kept": kept_head}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["kind"] == "bundle", f"a survivor still holding unpushed work must not become ref: {out}"
    assert [b["repository"] for b in out["bundles"]] == ["kept"], out
    assert out["bundles"][0]["path"] == recorded["bundles"][0]["path"], out
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert saved["bundles"] == out["bundles"], saved


def test_a_carried_bundle_is_never_recorded_twice(board, remote, tmp_path, monkeypatch):
    """Teeth for the bundle carry-forward: when the re-capture DOES reproduce a
    bundle for that repository, the carried copy must not be appended beside
    it -- a recovery index with two entries for one repo cannot be resolved.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    fresh = {"kind": "bundle", "bundles": [{"repository": "kept", "path": "/fresh.bundle"}]}
    previous = _bundle_survivor(tmp_path, "kept")

    out = survivor._unshrunk(previous, fresh)

    assert [b["repository"] for b in out["bundles"]] == ["kept"], out
    assert out["bundles"][0]["path"] == "/fresh.bundle", "the FRESH bundle must win"


def test_a_recorded_patch_vouches_for_the_repositories_its_sidecar_names(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION (P1 :471): a `kind: "patch"` survivor vouched for NOTHING, so
    reclamation of an already-completed card skipped the carry-forward, left
    `missing` populated and hit `if not explicit: raise` -- `_hold()` marking a
    done card permanently HELD. And it is exactly the uncommitted-work case
    where the patch is the only copy.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="patch survivor, recorded repo now gone")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "vouch-kept.git")
    sidecar = tmp_path / "vouch-implementation.json"
    sidecar.write_text(json.dumps({"repositories": [{"repository": "gone", "base_sha": STALE},
                                                    {"repository": "kept", "base_sha": kept_head}]}))
    recorded = dict(_patch_survivor(tmp_path, [{"repository": "kept", "sha": kept_head,
                                                "remote": "origin"}]),
                    sidecar=str(sidecar))
    _seed_survivor(board, tid, ws, {"kept": kept_head, "gone": STALE}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["path"] == recorded["path"], out
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None, "a patch already covering `gone` must not re-HOLD"


def test_an_unreadable_sidecar_vouches_for_nothing(board, remote, tmp_path, monkeypatch):
    """Teeth: the patch relaxation is keyed on the sidecar MANIFEST, never on
    the mere presence of a patch. A sidecar that cannot be read names no
    repository, so the guard still fails closed.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="patch survivor, sidecar missing")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "nosidecar-kept.git")
    recorded = _patch_survivor(tmp_path, [{"repository": "kept", "sha": kept_head,
                                           "remote": "origin"}])
    _seed_survivor(board, tid, ws, {"kept": kept_head, "gone": STALE}, recorded)

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert "recorded repository missing" in str(excinfo.value), str(excinfo.value)


def test_two_unqualified_operator_survivors_are_refused(board, remote):
    """REGRESSION (P1 :271): `action="append"` made `--survivor-pr A
    --survivor-pr B` a natural invocation, and the historical "keep the first"
    silently discarded B -- recording A as the provenance for a repository
    whose survivor may have been B, after verifying both.
    """
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws,
                          survivor_pr=[PR, "example/other#12"])

    assert "ambiguous" in str(excinfo.value), str(excinfo.value)
    assert "<repository>=<claim>" in str(excinfo.value), str(excinfo.value)


def test_a_single_unqualified_operator_survivor_is_still_accepted(board, remote):
    """Teeth: the refusal above is about AMBIGUITY, not about repeatability.
    The historical single-claim shape must keep working.
    """
    from hermes_cli import kanban_survivor as survivor

    tid, ws = stale_card(board, loose=False)

    out = survivor.preserve(board, tid, workspace=ws, survivor_pr=[PR])

    assert [ref["pr"] for ref in out["refs"]] == [PR], out


def test_a_qualifier_naming_a_repository_still_on_disk_is_refused(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION (P1 :519): `uncovered = missing - set(explicit)` checked only
    that every VANISHED repo is covered, never the converse. Qualifying a repo
    that is still on disk -- a plausible reading of "qualify every claim" --
    stamped `carried` with it, the capture loop then derived its own ref, and
    the row recorded TWO contradictory provenances for one repository. This
    exit does not pass through `_merge_refs()`.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="qualifier naming a live repository")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kept_head = _seed_published_repo(ws / "kept", tmp_path / "intrude-kept.git")
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": kept_head, "gone": STALE})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws,
                          survivor_pr=[f"gone={PR}", "kept=example/other#12"])

    message = str(excinfo.value)
    assert "still present" in message and "kept" in message, message
    assert board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"] is None, "no conflicting row may be recorded"


def test_a_non_code_recompletion_never_erases_a_recorded_survivor(board, remote):
    """REGRESSION (P1 :614): `survivor = previous if cleanup else None` guarded
    only reclamation. `_record()` does `SET survivor = excluded.survivor`, so
    on the COMPLETION pass this wrote JSON `null` over a survivor a previous
    completion had published -- and returned a survivor-less verdict that lets
    the caller rmtree the directory.

    Reachable: complete with `--survivor-pr` while the workspace is missing
    (records an external ref, `bases` empty, `claimed` False), then complete
    again with no flags once an empty dir exists.
    """
    from hermes_cli import kanban_survivor as survivor

    tid = kb.create_task(board, title="external implementation, re-completed")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    if ws.is_dir():
        ws.rmdir()  # the workspace-MISSING branch: the dir was reaped
    recorded = survivor.preserve(board, tid, workspace=ws, survivor_pr=PR)
    assert recorded["refs"][0]["pr"] == PR

    ws.mkdir(parents=True, exist_ok=True)  # re-created empty, no repo, nothing claimed
    out = survivor.preserve(board, tid, workspace=ws)

    assert out == recorded, "a re-completion may not shrink the recovery index"
    saved = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["survivor"])
    assert saved["refs"][0]["pr"] == PR, saved


def test_a_card_with_no_recorded_survivor_still_completes_with_none(board, remote):
    """Teeth: keeping `previous` must not turn genuine non-code work into a
    survivor. With nothing ever recorded, `preserve()` still returns None.
    """
    from hermes_cli import kanban_survivor as survivor

    tid = kb.create_task(board, title="docs only")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)

    assert survivor.preserve(board, tid, workspace=ws) is None


def test_a_bundle_shaped_external_survivor_does_not_raise_keyerror(
        board, remote, tmp_path, monkeypatch):
    """REGRESSION (P1 :602): on cleanup with every checkout gone, `_external()`
    returns the recorded survivor as-is; a `kind: "bundle"` row often has NO
    `refs` key, and `external["refs"]` raised `KeyError` -- which is not in
    `preserve()`'s except tuple, so it escaped past `_hold()` and skipped the
    fail-closed contract entirely.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    tid = kb.create_task(board, title="bundle survivor, every checkout gone")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    recorded = {"kind": "bundle", "notice": "NOT PUSHED",
                "bundles": [{"repository": "gone", "path": str(tmp_path / "gone.bundle"),
                             "sha256": "c" * 64, "bytes": 128}]}
    assert "refs" not in recorded
    _seed_survivor(board, tid, ws, {"gone": STALE}, recorded)

    out = survivor.preserve(board, tid, cleanup=True, workspace=ws)

    assert out["kind"] == "bundle" and out["bundles"] == recorded["bundles"], out
    assert board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()["held_reason"] is None


def test_an_unrepresentable_repository_key_gets_an_honest_remedy(board, remote):
    """REGRESSION (P2 :228): the refusals interpolate the real key into the
    form the operator is told to type. A key the grammar cannot express
    printed a remedy that does not parse -- no accepted input existed for that
    state at all, the same unreachable-remedy bug this module exists to close.

    The grammar now admits any plain directory name (spaces, `@`, `+`, `~`,
    non-ASCII); only a name carrying `:`, `?`, `#` or `=` is unrepresentable,
    and for those the hint must say so rather than print a lie.
    """
    from hermes_cli import kanban_survivor as survivor

    # Names that used to be rejected by the whitelist now round-trip.
    for key in ("qa output", "repo@v2", "a+b", "~scratch", "ünïcode", "-lead"):
        assert survivor._split_qualifier(f"{key}={PR}") == (key, PR), key
        assert survivor._qualified_hint([key]) == f"--survivor-pr {key}=owner/repo#N"

    # And a genuinely unrepresentable one is admitted to, not papered over --
    # including when it sits BESIDE a representable key, since every key in
    # the set needs its own claim for the state to be satisfiable at all.
    hint = survivor._qualified_hint(["odd#name"])
    assert "no qualified form exists" in hint, hint
    assert "--survivor-pr odd#name=" not in hint, hint
    assert survivor._qualified_hint({"gone", "odd#name"}) == hint, (
        "a tidy sibling key must not mask an unsatisfiable one"
    )


def test_the_unrepresentable_remedy_reaches_the_refusal_message(board, remote, tmp_path):
    """The honest remedy must be what the operator actually SEES, not just what
    the helper returns.
    """
    from hermes_cli import kanban_survivor as survivor

    tid = kb.create_task(board, title="unrepresentable repository key")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"odd#name": STALE, "gone": STALE})),
        )

    with pytest.raises(ValueError) as excinfo:
        survivor.preserve(board, tid, workspace=ws, survivor_pr=PR)

    assert "no qualified form exists" in str(excinfo.value), str(excinfo.value)
