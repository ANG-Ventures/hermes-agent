"""A survivor must be the card's OWN work, named without leaking credentials.

FleetReview on PR #795 raised four P1s against the external-survivor gate. Each
test here reproduces one of them; together they pin the rule that text mining
supplies hints, never authority, and that a rejected claim is never persisted
verbatim.
"""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

HEAD = "a1" * 20
MERGE = "b2" * 20
PR = "example/project#68"
URL = "https://github.com/example/project.git"
SECRET = "ghp_" + "S3cr3t" * 4


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    """Answer every remote lookup affirmatively; the gate must not rely on network failure."""
    state = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
    calls = []
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, json.dumps(state).encode(), b"")
        if "ls-remote" in args and "-C" not in args:
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args, 0, f"{HEAD}\trefs/heads/feature\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state, calls


# --- P1 #1: a rejected claim must not be persisted or logged verbatim -------

def test_unverifiable_survivor_ref_never_persists_the_raw_claim(board, remote, caplog):
    """The refusal path is exactly the credential-bearing path.

    ``_safe_url`` rejects any URL carrying userinfo, so a PAT typed into the
    flag is guaranteed to reach the refusal message — which is then written to
    ``held_reason``, a ``workspace_held`` event, the logfile and stderr.
    """
    claim = f"https://x-access-token:{SECRET}@github.com/example/project.git#{HEAD}"
    tid = kb.create_task(board, title="external implementation")
    with caplog.at_level("WARNING"):
        with pytest.raises(ValueError) as excinfo:
            kb.complete_task(board, tid, survivor_ref=claim,
                             metadata={"changed_files": ["code.py"]})

    assert SECRET not in str(excinfo.value)
    assert SECRET not in caplog.text
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held is not None and SECRET not in (held[0] or "")
    events = json.dumps([e.payload for e in kb.list_events(board, tid)])
    assert SECRET not in events
    # The operator still has to be told which flag failed.
    assert "--survivor-ref" in str(excinfo.value)


def test_unverifiable_survivor_pr_never_persists_the_raw_claim(board, remote, caplog):
    """Sibling call path: --survivor-pr takes the identical refusal branch."""
    claim = f"https://{SECRET}@github.com/example/project/pull/68"
    remote[0]["state"] = "CLOSED"
    tid = kb.create_task(board, title="external implementation")
    with caplog.at_level("WARNING"):
        with pytest.raises(ValueError) as excinfo:
            kb.complete_task(board, tid, survivor_pr=claim,
                             metadata={"changed_files": ["code.py"]})

    assert SECRET not in str(excinfo.value)
    assert SECRET not in caplog.text
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held is not None and SECRET not in (held[0] or "")
    assert SECRET not in json.dumps([e.payload for e in kb.list_events(board, tid)])


# --- P1 #3: explicit survivor flags must go through the multi-id guard ------

def test_multi_id_complete_refuses_survivor_flags(board, remote, monkeypatch, capsys):
    """One external PR cannot be the recovery pointer for three unrelated cards."""
    import argparse
    import contextlib

    from hermes_cli import kanban as cli

    ids = [kb.create_task(board, title=f"card {n}") for n in range(3)]
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: contextlib.nullcontext(board))

    rc = cli.kanban_command(parser.parse_args(
        ["kanban", "complete", *ids, "--survivor-pr", PR]))

    assert rc == 2, "multi-id completion with a survivor flag must be refused"
    assert "survivor" in capsys.readouterr().err
    for tid in ids:
        assert kb.get_task(board, tid).status != "done"
        assert not board.execute(
            "SELECT 1 FROM task_workspace_survivors WHERE task_id = ? AND survivor IS NOT NULL",
            (tid,)).fetchall()


# --- P1 #2 / #4: a mined hint must never authorize deleting real work -------

def test_mined_pr_does_not_authorize_deleting_an_uncaptured_workspace(board, remote):
    """A claimed card whose files are NOT in a git repo has nothing captured.

    ``_repos`` cannot see a plain scratch dir, so refs/patches/bundles are all
    empty and control reaches ``elif claimed:``. An incidental PR in a comment
    must not convert that fail-closed HOLD into an rmtree.
    """
    tid = kb.create_task(board, title="implementation card")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    (ws / "deliverable.py").write_text("real_work = True\n")
    kb.add_comment(board, tid, "reviewer", f"context: unrelated {PR} landed earlier")

    with pytest.raises(ValueError):
        kb.complete_task(board, tid, result="done",
                         metadata={"changed_files": ["deliverable.py"]})

    assert (ws / "deliverable.py").is_file(), "uncaptured work must survive"
    assert kb.get_task(board, tid).status != "done"


def test_mined_sha_from_an_unrelated_remote_tip_is_not_a_survivor(board, remote):
    """Any published SHA mentioned in the card text used to 'verify'.

    ``discover`` cross-products every SHA-looking token with every candidate
    remote URL, and ``verify_ref`` accepts a prefix match against any ref tip.
    Mentioning the commit you branched from is not evidence of a deliverable.
    (A workspace with no clone is the path that reaches text mining; a repo
    with uncommitted work is captured as a bundle before mining is consulted.)
    """
    tid = kb.create_task(board, title="implementation card")

    with pytest.raises(ValueError):
        kb.complete_task(
            board, tid,
            result=f"branched from {URL} at {HEAD}; nothing pushed",
            metadata={"changed_files": ["untracked-deliverable.py"]})

    assert kb.get_task(board, tid).status != "done"
    assert not board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ? AND survivor IS NOT NULL",
        (tid,)).fetchall()


def test_mined_ref_on_a_branch_that_names_the_task_is_accepted(board, remote, monkeypatch):
    """Teeth for the test above: the task's own pushed branch is still evidence."""
    tid = kb.create_task(board, title="implementation card")
    real = subprocess.run

    def run(args, **kwargs):
        if "ls-remote" in args and "-C" not in args:
            remote[1].append(list(args))
            return subprocess.CompletedProcess(
                args, 0, f"{HEAD}\trefs/heads/kanban/{tid}-impl\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert kb.complete_task(
        board, tid,
        result=f"pushed {URL} at {HEAD}",
        metadata={"changed_files": ["code.py"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "ref" and saved["refs"][0]["sha"] == HEAD
    assert saved["refs"][0]["branch"] == f"refs/heads/kanban/{tid}-impl"


def test_mined_pr_corroborated_by_the_cards_own_metadata_is_accepted(board, remote):
    """Teeth: the gate must still accept evidence the card itself claims.

    Without this, a stub that refuses everything would pass the four tests
    above while destroying the feature the branch exists to ship.
    """
    tid = kb.create_task(board, title="external implementation")
    assert kb.complete_task(
        board, tid,
        result=f"Shipped {PR} at {HEAD}",
        metadata={"changed_files": ["code.py"], "pr": PR})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "ref"
    assert saved["refs"][0]["sha"] == MERGE


# --- t_169d6e46: a swallowed git failure must be diagnosable ----------------

def test_git_failure_logs_returncode_and_stderr_without_persisting_them(tmp_path, caplog):
    """The hold names the Git operation and exit code without persisting stderr.

    The old message was identical for a real capture defect and for a purely
    environmental fault (t_169d6e46: a concurrent pytest session deleting this
    repo's tmp_path, so git exits 128 `cannot change to '<path>'`). Telling them
    apart cost a full attribution pass per occurrence. The returncode and stderr
    must reach the LOG; the raised message must stay credential-free,
    because it is persisted to held_reason, the event log and stderr.
    """
    import hermes_cli.kanban_survivor as survivor

    missing = tmp_path / "vanished"          # never created: git exits 128
    with caplog.at_level("WARNING"):
        with pytest.raises(survivor.SurvivorUnavailable) as excinfo:
            survivor._git(missing, "status", "--porcelain")

    assert str(excinfo.value) == "survivor_unavailable: git status failed (rc=128)"
    assert "No such file or directory" not in str(excinfo.value)
    # ...and the diagnostic that distinguishes environment from defect is logged.
    assert "rc=128" in caplog.text
    assert "status" in caplog.text
    assert "No such file or directory" in caplog.text


def test_git_failure_log_redacts_credentials_from_stderr(tmp_path, caplog, monkeypatch):
    """Git stderr can carry a credential-bearing remote URL; the log must not.

    Teeth for the test above: an unredacted `log.warning(stderr)` would satisfy
    every assertion there while leaking a PAT into the logfile.
    """
    import subprocess as sp

    import hermes_cli.kanban_survivor as survivor

    leaky = f"fatal: could not read from https://x-access-token:{SECRET}@github.com/a/b.git\n"
    monkeypatch.setattr(
        survivor.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0], 128, b"", leaky.encode()),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(survivor.SurvivorUnavailable) as excinfo:
            survivor._git(tmp_path, "fetch")

    assert SECRET not in caplog.text
    assert SECRET not in str(excinfo.value)
    assert str(excinfo.value) == "survivor_unavailable: git fetch failed (rc=128)"
    assert SECRET not in str(caplog.records[-1].getMessage())
    assert "rc=128" in caplog.text        # still diagnosable
