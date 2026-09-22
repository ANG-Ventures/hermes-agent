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
import shutil
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
def test_unrelated_live_claim_is_refused(board, unrelated, kwargs, monkeypatch):
    # The refusal names the override only for a caller with no dispatcher
    # grant (see `_override_hint`). `tests/conftest.py` scrubs those vars, so
    # this would pass implicitly -- assert the operator shape explicitly, so a
    # change to that scrub list cannot silently flip what this test measures.
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
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

    NOTE the bound of this test, which is why the tests below exist: it
    inspects the TOOL schema, and a worker also has a shell. Argus measured a
    dispatched worker reaching ``--survivor-unbound`` through the CLI in two
    environment shapes, so tool-unreachability is necessary and not sufficient.
    """
    import inspect

    from tools import kanban_tools as kt

    assert "survivor_unbound" not in json.dumps(kt.KANBAN_COMPLETE_SCHEMA)
    assert "survivor_unbound" not in inspect.getsource(kt._handle_complete)


# --- the override cannot become STANDING deletion authority -----------------

def _recorded(board, tid, *, unbound):
    """Record a survivor directly, so the arms differ ONLY in ``unbound``."""
    ref = {"remote": URL, "branch": "refs/pull/68/head", "sha": MERGE,
           "pr": PR, "state": "MERGED", "external": True, "repository": "."}
    if unbound:
        ref = dict(ref, unbound=True, claimed_by="someone")
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor, held_reason) "
            "VALUES (?,?,NULL) ON CONFLICT(task_id) DO UPDATE SET "
            "survivor = excluded.survivor, held_reason = NULL",
            (tid, json.dumps({"kind": "ref", "refs": [ref]})))


def _reclaimable(board, tid):
    """The dir-ABSENT reclamation shape, where ``previous`` is the SOLE authority.

    This is the branch the card names (``kanban_survivor.py`` stale-bases /
    missing-workspace): nothing is left in-tree to capture, so whatever
    completion recorded is the only thing standing between the card and a
    delete. With the directory present, an in-tree capture answers first and
    the recorded survivor is never consulted.
    """
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    shutil.rmtree(ws, ignore_errors=True)
    return ws


def test_an_unbound_claim_is_not_reusable_as_reclamation_authority(board):
    """The bound that makes the override safe without an identity check.

    ``preserve(cleanup=True)`` does not re-verify: it reuses whatever
    completion recorded. Without this, one unbound claim becomes STANDING
    authority to discard the workspace on every later reclamation -- the claim
    is never re-tested against the tree actually in front of the caller.

    Scope, stated precisely because the probe measured it: on THIS branch the
    directory is already gone, so what the gate protects here is the reuse of
    an uncorroborated POINTER, not bytes. Where bytes are still on disk and
    capturable, ``preserve`` captures them in-tree and never consults
    ``previous`` at all (kanban card t_de2e348e, probe_r2_branches.py, P2).
    """
    from hermes_cli import kanban_survivor as ks

    tid = _claimed_card(board)
    ws = _reclaimable(board, tid)
    _recorded(board, tid, unbound=True)

    with pytest.raises(ValueError, match="no verifiable external survivor"):
        ks.preserve(board, tid, cleanup=True, workspace=ws)


def test_a_bound_claim_is_still_reusable_as_reclamation_authority(board):
    """Anti-vacuity: the same rig, one field different, and it says YES.

    Without this the test above passes for any reason at all -- including a
    reclamation path that refuses everything.
    """
    from hermes_cli import kanban_survivor as ks

    tid = _claimed_card(board)
    ws = _reclaimable(board, tid)
    _recorded(board, tid, unbound=False)

    survivor = ks.preserve(board, tid, cleanup=True, workspace=ws)
    assert survivor and survivor["refs"][0]["pr"] == PR


def test_a_worker_using_the_cli_override_gains_no_reclamation_authority(
        board, unrelated, monkeypatch):
    """The test Argus named: a dispatched-worker CLI invocation must not buy a delete.

    Argus measured a worker shelling out to
    ``kanban complete <tid> --survivor-pr <unrelated live PR>
    --survivor-unbound`` and COMPLETING the card, in two environment shapes.
    That is not disputed and is not fixed by an identity check -- every
    process-local signal is forgeable at the same uid
    (kanban card t_de2e348e, probe_r2_signals.py: predicate fails open under ``env -u``,
    ancestry under double-fork, ``isatty`` under ``pty.fork``).

    What IS pinned here is the consequence. The worker may close its own card,
    but the claim it recorded is not standing authority for the later
    reclamation that would actually discard the workspace: that HOLDs, and the
    hold is where the bytes are protected. Drop ``_reusable`` and this goes
    red.
    """
    from hermes_cli import kanban_survivor as ks

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_whatever")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "4242")

    tid = _claimed_card(board)
    assert _cli(board, monkeypatch, ["complete", tid, "--result", "shipped",
                                     "--survivor-pr", PR, "--survivor-unbound"]) == 0
    # The worker did complete the card -- reported, not asserted away.
    assert kb.get_task(board, tid).status == "done"
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["unbound"] is True

    # ... and that is where its authority stops.
    ws = _reclaimable(board, tid)
    with pytest.raises(ValueError, match="no verifiable external survivor"):
        ks.preserve(board, tid, cleanup=True, workspace=ws)


# --- the refusal must not hand a worker the escape --------------------------

def test_the_refusal_names_the_override_for_an_operator(board, unrelated, monkeypatch):
    """An operator shell has no dispatcher grant: the hint is useful there."""
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    tid = _claimed_card(board)
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})
    assert "--survivor-unbound" in str(excinfo.value)


@pytest.mark.parametrize("grant", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"])
def test_the_refusal_withholds_the_override_from_a_worker(board, unrelated, monkeypatch, grant):
    """The refusal is forwarded to a worker's model and persisted as held_reason.

    Argus measured #848's first refusal text telling a dispatched worker the
    exact flag that converts its own refusal into a completion. The refusal
    must still say WHY it refused -- it just must not hand over the command.
    """
    monkeypatch.setenv(grant, "t_whatever")
    tid = _claimed_card(board)
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    message = str(excinfo.value)
    assert "--survivor-unbound" not in message, "the escape must not be advertised to a worker"
    assert "does not name" in message and tid in message, "it must still explain the refusal"
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held and "--survivor-unbound" not in (held[0] or "")


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
