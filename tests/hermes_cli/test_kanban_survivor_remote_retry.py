"""Survivor gate: retry a transient non-answer; accept a landed non-tip commit.

t_47199870 (seen 2026-09-24 on t_4f944382 and t_a887cce3):

1. ``git ls-remote`` exited 128 once and the card was HELD
   (``survivor_unavailable``) instead of asking again.
2. A survivor commit already on ``main`` but no longer a ref tip was refused,
   forcing a hand-made ``survivor/<id>`` tag.
"""
import json
import subprocess
import time  # noqa: F401  (patched by the sleeps fixture)
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_external_survivor as ext

URL = "https://github.com/example/project.git"
MAIN_TIP = "c3" * 20
LANDED = "d4" * 20


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    return slept


@pytest.fixture
def remote(monkeypatch, sleeps):
    """A github remote whose ``main`` has moved past ``LANDED``.

    ``state["fail"]`` is how many leading remote calls exit 128.
    """
    state = {"fail": 0, "subject": "fix: the thing", "body": "", "tips": {MAIN_TIP: ["refs/heads/main"]},
             "status": "ahead", "calls": []}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] not in {"git", "gh"} or (args[0] == "git" and "ls-remote" not in args):
            return real(args, **kwargs)
        state["calls"].append(list(args))
        if state["fail"]:
            state["fail"] -= 1
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: unable to access")
        if "--symref" in args:
            out = f"ref: refs/heads/main\tHEAD\n{MAIN_TIP}\tHEAD\n"
        elif "ls-remote" in args:
            out = "".join(f"{oid}\t{ref}\n" for oid, refs in state["tips"].items() for ref in refs)
        elif args[2].startswith("repos/example/project/commits/"):
            sha = args[2].rsplit("/", 1)[1]
            known = [oid for oid in (LANDED, MAIN_TIP) if oid.startswith(sha)]
            if not known:
                return subprocess.CompletedProcess(args, 1, b"", b"gh: Not Found (HTTP 404)")
            message = state["subject"] + ("\n\n" + state["body"] if state["body"] else "")
            out = json.dumps({"sha": known[0], "commit": {"message": message}})
        elif "/compare/" in args[2]:
            out = json.dumps({"status": state["status"], "ahead_by": 3, "behind_by": 0})
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out.encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    return state


# --- AC1: a transient non-answer is retried, bounded -------------------------

def test_transient_exit_128_is_retried_then_answers(remote, sleeps):
    remote["fail"] = 2
    out = ext._query(["git", "ls-remote", "--heads", "--tags", "--", URL])
    assert MAIN_TIP in out
    assert len(remote["calls"]) == 3
    assert sleeps == [0.5, 1.0], "bounded exponential backoff"


def test_persistent_exit_128_gives_up_after_three_attempts(remote, sleeps):
    remote["fail"] = 99
    with pytest.raises(ext.RemoteUnavailable, match=r"exited 128.*after 3 attempts"):
        ext._query(["git", "ls-remote", "--heads", "--tags", "--", URL])
    assert len(remote["calls"]) == 3
    assert len(sleeps) == 2


def test_missing_binary_is_not_retried(monkeypatch, sleeps):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ext.RemoteUnavailable, match="did not answer"):
        ext._query(["git", "ls-remote", "--", URL])
    assert len(calls) == 1 and sleeps == []


def test_blip_during_completion_does_not_hold_the_card(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote["tips"] = {LANDED: [f"refs/heads/kanban/{tid}-fix"]}
    remote["fail"] = 1
    assert kb.complete_task(board, tid, survivor_ref=f"{URL}#{LANDED}")
    assert kb.get_task(board, tid).status == "done"
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["sha"] == LANDED


# --- AC2: a commit reachable from the default branch, tip not required -------

@pytest.mark.parametrize("claimed", [LANDED, LANDED[:9]])
def test_non_tip_commit_on_default_naming_the_card_is_accepted(board, remote, claimed):
    tid = kb.create_task(board, title="external implementation")
    remote["subject"] = f"feat(ops): the thing ({tid})"
    remote["status"] = "ahead"  # compare <sha>...main: main moved past it -> reachable, not a tip
    assert kb.complete_task(board, tid, survivor_ref=f"{URL}#{claimed}")
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["sha"] == LANDED
    assert ref["branch"] == ref["reachable_from"] == "refs/heads/main"
    assert ref["corroborated_by"] == "commit-subject"


def test_default_tip_naming_the_card_in_its_subject_is_accepted(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote["subject"] = f"fix: landed directly on main ({tid})"
    remote["status"] = "identical"
    assert kb.complete_task(board, tid, survivor_ref=f"{URL}#{MAIN_TIP}")
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["sha"] == MAIN_TIP and ref["corroborated_by"] == "commit-subject"


def test_commit_on_default_that_only_mentions_the_card_in_its_body_is_refused(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote["body"] = f"follow-up to {tid}; does not address it"
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_ref=f"{URL}#{LANDED}")
    message = str(excinfo.value)
    assert "does not name" in message and tid in message
    # AC3: the refusal names both accepted shapes.
    assert "branch/tag tip naming it" in message
    assert "reachable from the default branch" in message
    assert kb.get_task(board, tid).status != "done"


def test_commit_not_reachable_from_default_is_refused_with_the_reason(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote["subject"] = f"feat: work ({tid})"
    remote["status"] = "diverged"
    with pytest.raises(ValueError, match="not reachable from default branch refs/heads/main"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_ref=f"{URL}#{LANDED}")
    assert kb.get_task(board, tid).status != "done"


def test_mined_path_does_not_widen_to_ancestry(remote):
    remote["subject"] = "feat: work (t_0000abcd)"
    assert ext.verify_ref(f"{URL}#{LANDED}", mined_for="t_0000abcd") is None


# --- AC3: refusal text names both paths --------------------------------------

def test_bare_sha_names_the_accepted_shapes(board, remote):
    tid = kb.create_task(board, title="external implementation")
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]}, survivor_ref=LANDED)
    message = str(excinfo.value)
    assert "names no remote" in message
    assert "branch/tag tip" in message and "reachable from the default branch" in message
    assert remote["calls"] == [], "nothing to ask a remote about"


def test_unreachable_remote_refusal_names_both_paths(board, remote):
    tid = kb.create_task(board, title="external implementation")
    remote["fail"] = 99
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_ref=f"{URL}#{LANDED}")
    message = str(excinfo.value)
    assert "after 3 attempts" in message
    assert "branch/tag tip" in message and "reachable from the default branch" in message
    assert kb.get_task(board, tid).status != "done"
