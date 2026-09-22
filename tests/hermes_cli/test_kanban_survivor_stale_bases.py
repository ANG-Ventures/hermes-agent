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
    """Answer remote lookups affirmatively; the gate must not rely on a network failure."""
    state = {"state": "OPEN", "headRefOid": HEAD, "mergeCommit": None}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            return subprocess.CompletedProcess(args, 0, json.dumps(state).encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


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


def test_partial_loss_keeps_both_the_surviving_repo_and_the_operator_ref(board, remote, tmp_path, monkeypatch):
    """Only SOME recorded repos vanished: neither survivor may be dropped.

    The surviving repo resolves its own remote ref, which on its own would
    satisfy the completion and silently discard the operator's ref for the repo
    that is gone -- leaving that work pointed at nothing.
    """
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])

    def git(repo, *args):
        return subprocess.run([
            "git", "-C", str(repo), *args
        ], capture_output=True, check=True).stdout.decode().strip()

    tid = kb.create_task(board, title="partial loss")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
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
    kb.set_workspace_path(board, tid, ws)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({"kept": git(kept, "rev-parse", "HEAD"), "gone": STALE})),
        )

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
