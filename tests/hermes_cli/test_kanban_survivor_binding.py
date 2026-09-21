"""An explicit survivor claim must be THIS card's work, not merely a live PR.

Found by Argus reviewing PR #839: ``_verified_explicit`` called
``verify_pr``/``verify_ref`` with ``mined_for`` unset, so an operator- or
worker-named ``--survivor-pr`` was verified only as "exists on GitHub and is
OPEN or MERGED". Measured against real public PRs, an entirely unrelated PR was
ACCEPTED as the survivor -- and ``preserve`` treats a verified explicit
survivor as authority for the branch whose whole job is protecting UNPUSHED
implementation work.

DECISION: option (a) with an explicit override. The claim is now bound to the
card the same way the text-mined path is bound, and the legitimate operator
case (the work really did land on a differently-named branch) keeps a path via
``--survivor-unbound``, which is recorded on the survivor and in the event log.
The override is a CLI flag only: a worker on the ``kanban_complete`` tool
cannot self-certify an unrelated survivor.
"""
import argparse
import contextlib
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
def unrelated(monkeypatch):
    """A real, live, MERGED PR on a branch that has nothing to do with any card.

    This is the measured shape of the defect: every remote lookup succeeds, so
    the gate cannot lean on network failure to fail closed.
    """
    view = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE},
            "headRefName": "someone-elses/unrelated-work",
            "title": "unrelated work", "body": "nothing to do with any card"}
    ref = "refs/heads/someone-elses/unrelated-work"
    calls = []
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, json.dumps(view).encode(), b"")
        if "ls-remote" in args and "-C" not in args:
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, f"{HEAD}\t{ref}\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return view, calls


def _cli(board, monkeypatch, argv):
    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: contextlib.nullcontext(board))
    return cli.kanban_command(parser.parse_args(["kanban", *argv]))


def _claimed_card(board, title="external implementation"):
    """A card whose work is claimed but lives nowhere in this process."""
    return kb.create_task(board, title=title)


# --- the defect: an unrelated live PR must no longer authorise a delete ------

@pytest.mark.parametrize("kwargs", [
    {"survivor_pr": PR},
    {"survivor_ref": f"{URL}#{HEAD}"},
])
def test_unrelated_live_claim_is_refused(board, unrelated, kwargs):
    tid = _claimed_card(board)
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]}, **kwargs)

    message = str(excinfo.value)
    assert "does not name" in message and tid in message
    assert "--survivor-unbound" in message, "the refusal must name the reachable override"
    assert kb.get_task(board, tid).status != "done"
    assert not board.execute(
        "SELECT 1 FROM task_workspace_survivors WHERE task_id = ? AND survivor IS NOT NULL",
        (tid,)).fetchall()
    assert unrelated[1], "the claim must still be checked against the remote"


def test_unrelated_live_claim_does_not_delete_the_workspace(board, unrelated):
    """The consequence, not just the return value: the bytes must survive."""
    tid = _claimed_card(board)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "implementation.py").write_text("work that lives nowhere else\n")

    with pytest.raises(ValueError):
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    assert (ws / "implementation.py").read_text() == "work that lives nowhere else\n"
    assert kb.get_task(board, tid).status != "done"


# --- the binding: a claim that DOES name the card is still accepted ---------

@pytest.mark.parametrize("field", ["headRefName", "title", "body"])
def test_claim_naming_the_card_is_accepted(board, unrelated, field):
    tid = _claimed_card(board)
    unrelated[0][field] = f"kanban/{tid}-fix"
    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["code.py"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["refs"][0]["sha"] == MERGE
    assert not saved["refs"][0].get("unbound")


def test_the_wider_corroboration_is_explicit_only(board, unrelated):
    """Title/body corroborate an OPERATOR's claim, never a text-mined one.

    The explicit path widens `corroborate` because the operator typing the
    number has already vouched for the PR's identity. The mined path must NOT
    widen with it: a PR body that merely mentions a card id would otherwise
    verify itself out of handoff text, which is exactly the trust the mined
    path was built to withhold.
    """
    from hermes_cli import kanban_external_survivor as ext

    tid = _claimed_card(board)
    unrelated[0]["body"] = f"implements {tid}"

    assert ext.verify_pr(PR, mined_for=tid) is None, "mined path must stay branch-only"
    assert ext.verify_pr(PR, mined_for=tid,
                         corroborate=("headRefName", "title", "body")) is not None
    # And end to end: the mined path still refuses this PR ...
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR}",
                         metadata={"changed_files": ["code.py"]})
    # ... while naming it explicitly is accepted on the same PR body.
    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["code.py"]})


# --- the escape hatch stays reachable, and is auditable ---------------------

def test_operator_override_accepts_an_unrelated_branch_and_records_it(board, unrelated):
    """The legitimate case: a human who KNOWS the work landed elsewhere."""
    tid = _claimed_card(board)
    assert kb.complete_task(board, tid, survivor_pr=PR, survivor_unbound=True,
                            metadata={"changed_files": ["code.py"]})
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["sha"] == MERGE
    assert ref["unbound"] is True
    assert ref["claimed_by"], "an unbound authorisation must record WHO made it"
    events = [e for e in kb.list_events(board, tid) if e.kind == "workspace_survivor"]
    assert events and events[-1].payload["refs"][0]["unbound"] is True


def test_override_still_requires_the_claim_to_be_real(board, unrelated):
    """--survivor-unbound relaxes the binding, never the remote verification."""
    tid = _claimed_card(board)
    with pytest.raises(ValueError, match="could not verify"):
        kb.complete_task(board, tid, survivor_ref=f"{URL}#{'c3' * 20}",
                         survivor_unbound=True, metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_cli_exposes_the_override(board, unrelated, monkeypatch):
    tid = _claimed_card(board)
    assert _cli(board, monkeypatch, ["complete", tid, "--result", "shipped",
                                     "--survivor-pr", PR, "--survivor-unbound"]) == 0
    assert kb.get_task(board, tid).status == "done"
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["unbound"] is True


def test_cli_refuses_a_bare_override(board, unrelated, monkeypatch, capsys):
    """The override is a modifier on a claim, not a claim of its own."""
    tid = _claimed_card(board)
    assert _cli(board, monkeypatch, ["complete", tid, "--survivor-unbound"]) == 2
    assert "--survivor-unbound only relaxes" in capsys.readouterr().err
    assert kb.get_task(board, tid).status != "done"
    assert unrelated[1] == [], "a refused invocation must not reach the remote"


def test_cli_refuses_the_override_with_multiple_ids(board, unrelated, monkeypatch, capsys):
    ids = [_claimed_card(board, f"card {n}") for n in range(2)]
    assert _cli(board, monkeypatch,
                ["complete", *ids, "--survivor-pr", PR, "--survivor-unbound"]) == 2
    assert "per-task" in capsys.readouterr().err
    for tid in ids:
        assert kb.get_task(board, tid).status != "done"


# --- the worker surface cannot reach the override ---------------------------

def test_the_tool_surface_cannot_express_the_override():
    """The override is operator-only BY CONSTRUCTION, not by convention.

    A worker naming an unrelated PR is exactly the population this card
    narrowed, so no ``kanban_complete`` tool argument may carry the override,
    and the handler must not name it. This holds both before and after PR #839
    (which adds ``survivor_pr``/``survivor_ref`` to that schema) -- #839 widens
    WHO can name a survivor; it must not widen who can name an UNBOUND one.
    """
    import inspect

    from tools import kanban_tools as kt

    assert "survivor_unbound" not in json.dumps(kt.KANBAN_COMPLETE_SCHEMA)
    assert "survivor_unbound" not in inspect.getsource(kt._handle_complete)


# --- redaction is unchanged on the new refusal branch -----------------------

def test_the_new_refusal_never_echoes_a_credential(board, unrelated, caplog):
    """The binding refusal is a NEW path that echoes the claim: redact it too."""
    claim = f"https://x-access-token:{SECRET}@github.com/example/project.git#{HEAD}"
    tid = _claimed_card(board)
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
    assert SECRET not in json.dumps([e.payload for e in kb.list_events(board, tid)])
