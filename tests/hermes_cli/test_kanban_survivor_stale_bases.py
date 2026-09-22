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

    ``headRefName`` starts UNRELATED to any card on purpose. Existence is not
    relevance: an explicit ``--survivor-pr`` must corroborate the card that
    names it, so a test wanting the happy path calls :func:`names_card` and one
    wanting the refusal leaves this alone.
    """
    state = {"state": "OPEN", "headRefOid": HEAD, "mergeCommit": None,
             "missing": False, "tips": "",
             "headRefName": "someone/unrelated-work", "title": "", "body": ""}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            if state["missing"]:
                return subprocess.CompletedProcess(args, 1, b"", b"not found")
            # Forward every PR field the verifier may consult -- not just the
            # three this file's own assertions read. `--survivor-pr` now
            # corroborates the card against `headRefName`/`title`/`body`, so
            # narrowing the payload here would make `names_card` inert and
            # silently turn the happy-path tests into refusal tests.
            payload = {k: v for k, v in state.items() if k not in ("missing", "tips")}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")
        # `_ext` shells out as `git ls-remote ...`; `_git` always passes `-C`.
        if args[0] == "git" and len(args) > 1 and args[1] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, state["tips"].encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def names_card(remote, tid):
    """Make the claimed PR corroborate THIS card, the way a real one would."""
    remote["headRefName"] = f"operator/{tid}-landed-elsewhere"
    return remote


def stale_card(conn, *, title="review lane"):
    """A card whose recorded repo is gone but whose workspace dir survives."""
    tid = kb.create_task(conn, title=title)
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
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
    names_card(remote, tid)

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
    names_card(remote, tid)

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
    names_card(remote, tid)

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
    tid, _ = partial_loss_card(board, tmp_path, monkeypatch)
    # The advertised ref must NAME the card: an explicit `--survivor-ref` is
    # bound the same way `--survivor-pr` is, so `refs/heads/work` alone would
    # be refused here as an unrelated branch rather than as an unverified SHA.
    remote["tips"] = f"{sha}\trefs/heads/operator/{tid}-landed-elsewhere\n"
    claim = f"https://github.com/example/project.git#{sha}"

    assert kb.complete_task(board, tid, summary="approved", survivor_ref=claim)

    saved = kb.latest_run(board, tid).metadata["survivor"]
    by_repo = {ref["repository"]: ref for ref in saved["refs"]}
    assert by_repo["gone"]["sha"] == sha
    assert set(by_repo) == {"gone", "kept"}, saved["refs"]
