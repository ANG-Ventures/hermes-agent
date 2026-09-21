"""Completion must verify remote evidence even without an implementation clone."""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor

HEAD = "a1" * 20
MERGE = "b2" * 20
PR = "example/project#68"
URL = "https://github.com/example/project.git"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    state = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
    calls = []
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            calls.append(args)
            assert args[:6] == ["gh", "pr", "view", "68", "--repo", "example/project"]
            return subprocess.CompletedProcess(args, 0, json.dumps(state).encode(), b"")
        if "ls-remote" in args:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, f"{HEAD}\trefs/heads/feature\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state, calls


@pytest.mark.parametrize("state,expected", [("MERGED", MERGE), ("OPEN", HEAD)])
def test_result_pr_without_workspace_clone(board, remote, state, expected):
    remote[0]["state"] = state
    tid = kb.create_task(board, title="external implementation")
    assert kb.complete_task(board, tid, result=f"Shipped {PR} at {HEAD}",
                            metadata={"changed_files": ["code.py"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "ref"
    assert saved["refs"][0]["sha"] == expected
    assert remote[1], "must consult remote, not accept the text"


@pytest.mark.parametrize("state", ["CLOSED", "UNKNOWN"])
def test_unmerged_or_unknown_pr_refuses(board, remote, state):
    remote[0]["state"] = state
    tid = kb.create_task(board, title="external implementation")
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR}", metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_wrong_claimed_sha_refuses(board, remote):
    tid = kb.create_task(board, title="external implementation")
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR} at {'c3' * 20}",
                         metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_explicit_ref_missing_on_remote_refuses(board, remote):
    tid = kb.create_task(board, title="external implementation")
    with pytest.raises(ValueError, match="verify"):
        kb.complete_task(board, tid, survivor_ref=f"{URL}#{'c3' * 20}")
    assert kb.get_task(board, tid).status != "done"


def test_explicit_ref_records_resolved_full_sha(board, remote):
    tid = kb.create_task(board, title="external implementation")
    assert kb.complete_task(board, tid, survivor_ref=f"{URL}#{HEAD[:7]}")
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["sha"] == HEAD
    assert remote[1]


def test_review_approval_discovers_handoff_summary_and_preserves_on_cleanup(board, remote):
    """The implementer's review handoff (a run summary) is the mined source, not the approval comment."""
    tid = kb.create_task(board, title="external implementation")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    assert kb.request_review(board, tid, summary=f"Shipped {PR} at {HEAD}",
                             metadata={"changed_files": ["code.py"]})
    kb.add_comment(board, tid, "reviewer", "Approved")
    assert kb.complete_task(board, tid, summary="Approved")
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "ref"
    assert not ws.exists()
    assert not [e for e in kb.list_events(board, tid) if e.kind == "workspace_held"]


@pytest.mark.parametrize("source", ["result", "comment"])
def test_non_code_card_does_not_mine_an_incidental_pr(board, remote, source):
    """A card that claims no code has no deliverable to infer.

    Discussion text routinely cites other cards' PRs. Mining it would record
    another card's work as this one's survivor, and nothing downstream
    re-checks a recovery pointer.
    """
    mention = f"FYI unrelated context: see {PR} for the survivor work."
    tid = kb.create_task(board, title="research card, no code at all")
    if source == "comment":
        kb.add_comment(board, tid, "reviewer", mention)
    assert kb.complete_task(board, tid, result=mention if source == "result" else "No code changed.")
    assert (kb.latest_run(board, tid).metadata or {}).get("survivor") is None
    assert not board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ? AND survivor IS NOT NULL",
        (tid,)).fetchall()
    assert remote[1] == [], "must not consult the remote for unclaimed work"


def test_claimed_card_still_reaches_the_remote(board, remote):
    """Teeth for the tripwire above: a stub that can never record would pass it."""
    tid = kb.create_task(board, title="external implementation")
    assert kb.complete_task(board, tid, result=f"Shipped {PR} at {HEAD}",
                            metadata={"changed_files": ["code.py"]})
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "ref"
    assert len(remote[1]) == 1


def test_explicit_pr_keeps_dirty_workspace_capture(board, remote, tmp_path):
    tid = kb.create_task(board, title="dirty implementation")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    def git(*args):
        return subprocess.run(["git", "-C", str(ws), *args], check=True,
                              stdin=subprocess.DEVNULL, capture_output=True)
    git("init", "-b", "main")
    (ws / "code.py").write_text("unpublished = True\n")
    assert kb.complete_task(board, tid, survivor_pr=PR)
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "bundle"
    assert Path(saved["bundles"][0]["path"]).is_file()
    assert not ws.exists()


def _cli(board, monkeypatch, argv):
    """Drive the real parser + handler so the flag->preserve() binding is gated."""
    import argparse
    import contextlib

    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    # The CLI closes its connection; the fixture's must survive for assertions.
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: contextlib.nullcontext(board))
    return cli.kanban_command(parser.parse_args(["kanban", *argv]))


def test_cli_complete_forwards_survivor_pr(board, remote, monkeypatch):
    tid = kb.create_task(board, title="external implementation")
    assert _cli(board, monkeypatch,
                ["complete", tid, "--result", "shipped", "--survivor-pr", PR]) == 0
    assert kb.get_task(board, tid).status == "done"
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["sha"] == MERGE
    assert remote[1], "must consult the remote, not accept the flag text"


def test_cli_complete_refuses_unverifiable_survivor_ref(board, remote, monkeypatch, capsys):
    tid = kb.create_task(board, title="external implementation")
    assert _cli(board, monkeypatch,
                ["complete", tid, "--survivor-ref", f"{URL}#{'c3' * 20}"]) != 0
    assert "could not verify --survivor-ref" in capsys.readouterr().err
    assert kb.get_task(board, tid).status != "done"


# --- FleetReview P1s on #795 (card t_8970f48e): text is a hint, never authority -------------

def test_mined_pr_without_sha_or_task_branch_refuses(board, remote):
    """A bare `owner/repo#N` mention proves nothing about THIS card's work.

    Without a corroborating SHA in the handoff (or a PR branch named after the
    task) the completion must fail closed and point at --survivor-pr.
    """
    tid = kb.create_task(board, title="external implementation")
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR}", metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_mined_pr_whose_branch_names_the_task_is_accepted(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote[0]["headRefName"] = f"kanban/{tid}-fix"
    assert kb.complete_task(board, tid, result=f"Shipped {PR}", metadata={"changed_files": ["code.py"]})
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["sha"] == MERGE


def test_comments_are_discussion_not_handoff(board, remote):
    """Another card's PR cited (with its SHA) in a comment must not become this card's survivor."""
    tid = kb.create_task(board, title="external implementation")
    kb.add_comment(board, tid, "reviewer", f"context: t_other shipped {PR} at {HEAD}")
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result="done", metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"
    assert not board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ? AND survivor IS NOT NULL",
        (tid,)).fetchall()


def test_uncaptured_workspace_files_are_never_traded_for_a_mined_pr(board, remote):
    """P1: a workspace holding un-versioned work must HOLD, not be deleted on a text hint."""
    tid = kb.create_task(board, title="external implementation")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "notes.md").write_text("work that lives nowhere else\n")
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, result=f"Shipped {PR} at {HEAD}",
                         metadata={"changed_files": ["code.py"]})
    assert ws.exists() and (ws / "notes.md").exists()
    assert kb.get_task(board, tid).status != "done"


def test_cleanup_reuses_the_recorded_survivor_and_never_mines(board, remote):
    tid = kb.create_task(board, title="external implementation")
    assert kb.complete_task(board, tid, result=f"Shipped {PR} at {HEAD}",
                            metadata={"changed_files": ["code.py"]})
    lookups = len(remote[1])
    stray = kb.kanban_home() / "stray-ws"
    stray.mkdir(parents=True)
    (stray / "leftover.py").write_text("x = 1\n")
    # A later GC pass over a directory with uncaptured files: the recorded
    # survivor does not vouch for THESE bytes, and text must not be re-mined.
    with pytest.raises(ValueError, match="survivor_unavailable"):
        survivor.preserve(board, tid, cleanup=True, workspace=stray)
    assert len(remote[1]) == lookups, "cleanup must not run remote lookups"
    assert stray.exists()


def test_cli_refuses_survivor_flags_with_multiple_ids(board, remote, monkeypatch, capsys):
    t1 = kb.create_task(board, title="one")
    t2 = kb.create_task(board, title="two")
    assert _cli(board, monkeypatch, ["complete", t1, t2, "--survivor-pr", PR]) == 2
    assert "per-task" in capsys.readouterr().err
    assert kb.get_task(board, t1).status != "done" and kb.get_task(board, t2).status != "done"
    assert remote[1] == []


def test_unverifiable_ref_error_never_echoes_credentials(board, remote):
    tid = kb.create_task(board, title="external implementation")
    leaky = f"https://oauth2:ghp_SECRET123@github.com/example/project.git#{'c3' * 20}"
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_ref=leaky)
    assert "ghp_SECRET123" not in str(excinfo.value)
    rows = board.execute("SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?", (tid,)).fetchall()
    assert all("ghp_SECRET123" not in (r[0] or "") for r in rows)
    assert not any("ghp_SECRET123" in json.dumps(e.payload if hasattr(e, "payload") else str(e))
                   for e in kb.list_events(board, tid))
