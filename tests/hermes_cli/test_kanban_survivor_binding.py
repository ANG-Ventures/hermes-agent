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
    # The refusal MESSAGE never names the override, in any environment: it is
    # persisted as held_reason and replayed to workers by kanban_show, so the
    # hint is rendered at the CLI boundary instead (see the CLI tests below).
    # Assert the operator shape explicitly, so a change to the conftest scrub
    # list cannot silently flip what this test measures.
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    tid = _claimed_card(board)
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]}, **kwargs)

    message = str(excinfo.value)
    assert "does not name" in message and tid in message
    assert "--survivor-unbound" not in message, "the flag must not be in the persisted text"
    assert excinfo.value.override_hint, "the override must still be reachable for a renderer"
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

def test_claim_naming_the_card_in_its_branch_is_bound(board, unrelated):
    """The head branch ties the PR's CONTENT to the card: a full binding."""
    tid = _claimed_card(board)
    unrelated[0]["headRefName"] = f"kanban/{tid}-fix"
    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["code.py"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["refs"][0]["sha"] == MERGE
    assert saved["refs"][0]["corroborated_by"] == "branch"
    assert not saved["refs"][0].get("unbound")


@pytest.mark.parametrize("field", ["title", "body"])
def test_a_mention_in_the_title_or_body_is_refused(board, unrelated, field, monkeypatch):
    """A card id in PR prose is a MENTION, not a tie to this card's work.

    An umbrella changelog, a dependency note, even "does not address t_..."
    satisfies a substring test. #848's round-5 FleetReview found that recording
    such a claim `unbound` and letting it COMPLETE anyway closed only half the
    hole: ``_reusable`` guards the recorded row on the ``cleanup=True`` pass and
    never sees a claim arriving through ``_external``'s ``explicit`` arm, so the
    completion path could still delete unpushed work on a mention alone. A
    mention now takes the same refusal an unrelated live claim takes.
    """
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    tid = _claimed_card(board)
    unrelated[0][field] = f"follow-up to {tid}; does not address it"

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    message = str(excinfo.value)
    assert f"only in its {field}" in message and "mention" in message
    assert "--survivor-unbound" not in message, "the flag must not be in the persisted text"
    assert excinfo.value.override_hint, "the override must still be reachable for a renderer"
    assert kb.get_task(board, tid).status != "done"


@pytest.mark.parametrize("field", ["title", "body"])
def test_a_mention_does_not_delete_the_workspace(board, unrelated, field):
    """The CONSEQUENCE, not the return value: the bytes must survive a mention.

    This is the arm ``_reusable`` could never cover. The expectation is computed
    independently of the function under test -- the file is written here and
    read back here -- and the path driven is the real ``complete_task``.
    """
    tid = _claimed_card(board)
    unrelated[0][field] = f"mentions {tid} in passing"
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "implementation.py").write_text("work that lives nowhere else\n")

    with pytest.raises(ValueError):
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    assert (ws / "implementation.py").read_text() == "work that lives nowhere else\n"
    assert kb.get_task(board, tid).status != "done"


@pytest.mark.parametrize("field", ["title", "body"])
def test_a_mention_the_operator_vouches_for_is_accepted_but_stays_unbound(
        board, unrelated, field):
    """Anti-vacuity: the override is the door, and it is still only one-shot.

    The operator who typed the number may still vouch for it. What they buy is
    THIS completion -- never standing delete authority, which ``_reusable``
    refuses on the recorded ``unbound`` flag.
    """
    from hermes_cli import kanban_survivor as ks

    tid = _claimed_card(board)
    unrelated[0][field] = f"follow-up to {tid}"
    assert kb.complete_task(board, tid, survivor_pr=PR, survivor_unbound=True,
                            metadata={"changed_files": ["code.py"]})
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["unbound"] is True and ref["claimed_by"]

    ws = _reclaimable(board, tid)
    with pytest.raises(ValueError, match="no verifiable external survivor"):
        ks.preserve(board, tid, cleanup=True, workspace=ws)


def test_a_branch_bound_claim_is_still_reusable(board, unrelated):
    """Anti-vacuity for the two tests above: same rig, branch match, REUSED."""
    from hermes_cli import kanban_survivor as ks

    tid = _claimed_card(board)
    unrelated[0]["headRefName"] = f"kanban/{tid}-fix"
    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["code.py"]})
    ws = _reclaimable(board, tid)
    survivor = ks.preserve(board, tid, cleanup=True, workspace=ws)
    assert survivor and survivor["refs"][0]["pr"] == PR


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
    widened = ext.verify_pr(PR, mined_for=tid, corroborate=("headRefName", "title", "body"))
    assert widened is not None and widened["corroborated_by"] == "body"
    # And end to end: the mined path still refuses this PR ...
    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR}",
                         metadata={"changed_files": ["code.py"]})
    # ... naming it explicitly is still a MENTION and is refused too (#848 r5) ...
    with pytest.raises(ValueError, match="only in its body"):
        kb.complete_task(board, tid, survivor_pr=PR,
                         metadata={"changed_files": ["code.py"]})
    # ... and the operator override is what accepts it.
    assert kb.complete_task(board, tid, survivor_pr=PR, survivor_unbound=True,
                            metadata={"changed_files": ["code.py"]})


# --- a transient remote failure must not be stated as a fact about the claim -

@pytest.fixture
def flaky(monkeypatch):
    """A remote that NAMES the card, with per-call failure injection.

    The seam is ``subprocess.run`` -- strictly BELOW ``_query``, so the
    function under test is never asked what it thinks, and the ground truth is
    fixed by construction: the branch names the card, so the honest outcome is
    ACCEPT. ``fail`` holds the 1-based call ordinals that exit non-zero, which
    is exactly what an ordinary rate limit or auth hiccup looks like.
    """
    state = {"fail": set(), "calls": 0, "branch": None}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh" or ("ls-remote" in args and "-C" not in args):
            state["calls"] += 1
            if state["calls"] in state["fail"]:
                return subprocess.CompletedProcess(args, 1, b"", b"gh: API rate limit exceeded")
            if args[0] == "gh":
                view = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE},
                        "headRefName": state["branch"], "title": "work", "body": "work"}
                return subprocess.CompletedProcess(args, 0, json.dumps(view).encode(), b"")
            return subprocess.CompletedProcess(
                args, 0, f"{HEAD}\trefs/heads/{state['branch']}\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


@pytest.mark.parametrize("fail,expect_bound", [
    (set(), True),          # control A: nothing injected -> the claim is ACCEPTED
    ({1}, False),           # the defect: the BOUND call blips
    ({1, 2}, False),        # control C: nothing answers at all
])
def test_a_transient_remote_failure_is_never_reported_as_irrelevance(
        board, flaky, fail, expect_bound):
    """"The remote did not answer" and "the remote said no" are different facts.

    ``_verified_explicit`` chose its refusal text by comparing two INDEPENDENT
    round-trips, and ``_query`` returned ``None`` on any non-zero exit, OSError
    or 15 s timeout. So a blip on the first call made the second one -- which
    asks a WEAKER question -- succeed, and the kernel stated as fact that a
    claim it had never checked "is live but does not name" the card. That
    sentence is persisted to ``held_reason`` and to the ``workspace_held``
    event ``kanban_show`` replays, and the remedy the CLI then offers is
    ``--survivor-unbound`` -- so a network blip laundered a legitimately BOUND
    claim into a recorded UNBOUND one, which ``_reusable`` refuses as
    reclamation authority forever (Argus round 3).

    Ground truth here is BOUND in every arm: the PR's head branch names the
    card. Arms A and C are working controls, so the middle arm is signal.
    """
    tid = _claimed_card(board)
    flaky["branch"] = f"kanban/{tid}-fix"
    flaky["fail"] = fail

    if expect_bound:
        assert kb.complete_task(board, tid, survivor_pr=PR,
                                metadata={"changed_files": ["code.py"]})
        assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["corroborated_by"] == "branch"
        return

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    assert "could not verify" in str(excinfo.value)
    assert "does not name" not in str(excinfo.value), (
        "a remote that did not answer says NOTHING about the claim")
    assert not excinfo.value.override_hint, (
        "dropping the binding is not the remedy for a network blip")
    # The channels a human and a redispatched worker actually read.
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held and "could not verify" in (held[0] or "")
    assert "does not name" not in (held[0] or "")
    assert "does not name" not in json.dumps([e.payload for e in kb.list_events(board, tid)])
    assert kb.get_task(board, tid).status != "done"


def test_a_transient_failure_on_a_ref_claim_is_reported_as_unverifiable(board, flaky):
    """Same seam on the other claim shape: ``--survivor-ref`` / git ls-remote."""
    tid = _claimed_card(board)
    flaky["branch"] = f"kanban/{tid}-fix"
    flaky["fail"] = {1}

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, survivor_ref=f"{URL}#{HEAD}",
                         metadata={"changed_files": ["code.py"]})
    assert "could not verify" in str(excinfo.value)
    assert "does not name" not in str(excinfo.value)


def test_a_transient_failure_while_MINING_is_not_a_crash(board, flaky):
    """``discover`` states no reason, so it may still treat no-answer as "no".

    The distinction is only load-bearing where a reason is stated. What must
    not happen is the new signal escaping as an unhandled error out of a path
    that previously just moved on to the next candidate.
    """
    tid = _claimed_card(board)
    flaky["branch"] = "someone-elses/unrelated-work"
    flaky["fail"] = {1}

    with pytest.raises(ValueError, match="survivor-pr"):
        kb.complete_task(board, tid, result=f"Shipped {PR}",
                         metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_the_override_also_distinguishes_a_blip_from_a_verdict(board, flaky):
    """``--survivor-unbound`` makes ONE round-trip; it must report it honestly."""
    tid = _claimed_card(board)
    flaky["branch"] = "someone-elses/unrelated-work"
    flaky["fail"] = {1}

    with pytest.raises(ValueError, match="could not verify"):
        kb.complete_task(board, tid, survivor_pr=PR, survivor_unbound=True,
                         metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


# --- the help must describe the gate that will refuse the operator ----------

@pytest.mark.parametrize("flag,metavar,ends_at", [
    ("--survivor-ref", "[REPO=]URL#SHA", "--survivor-pr"),
    ("--survivor-pr", "[REPO=]OWNER/REPO#N", "--survivor-unbound"),
])
def test_the_help_documents_the_task_naming_requirement(flag, metavar, ends_at, capsys):
    """Both claim flags enforce the binding, so both must say so.

    ``verify_ref`` gained the same ``mined_for`` enforcement ``verify_pr`` has,
    but the ``--survivor-ref`` help still described the old contract ("verified
    with git ls-remote") -- user-facing text promising acceptance from a gate
    that now refuses. Asserted as a relation between the two flags rather than
    as a snapshot of either one's wording: whichever flag the binding is
    documented on, it must be documented on both. Read out of the RENDERED
    help an operator sees, so argparse's own wrapping is included.
    """
    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "complete", "--help"])
    rendered = " ".join(capsys.readouterr().out.split())
    # The option list entry is the LAST "<flag> <metavar>"; usage lists it too.
    section = rendered.split(f"{flag} {metavar}")[-1].split(ends_at, 1)[0]
    assert "name this task" in section, (
        f"{flag} enforces the binding; its help must say so")


# --- the escape hatch stays reachable, and is auditable ---------------------

def test_operator_override_accepts_an_unrelated_branch_and_records_it(board, unrelated, monkeypatch):
    """The legitimate case: a human who KNOWS the work landed elsewhere.

    ``claimed_by`` must be the REAL OS user. ``getpass.getuser()`` consults
    ``$LOGNAME``/``$USER``/``$USERNAME`` before the passwd database, so the one
    control this design keeps -- attribution -- would otherwise yield an
    attacker-chosen string: this repo's own threat model has a dispatched
    worker reaching the flag through a shell at the same uid, so it could
    record ``claimed_by="operator"``. Poison those vars and compute the
    expectation from ``pwd``/``os.getuid`` directly, independent of
    ``_claimant``.
    """
    import os
    import pwd

    for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(var, "operator")

    tid = _claimed_card(board)
    assert kb.complete_task(board, tid, survivor_pr=PR, survivor_unbound=True,
                            metadata={"changed_files": ["code.py"]})
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["sha"] == MERGE
    assert ref["unbound"] is True
    uid = os.getuid()
    assert ref["claimed_by"] == f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    assert "operator" not in ref["claimed_by"], "the audit trail must not be $USER"
    assert str(uid) in ref["claimed_by"], "record the numeric uid alongside the name"
    events = [e for e in kb.list_events(board, tid) if e.kind == "workspace_survivor"]
    assert events and events[-1].payload["refs"][0]["unbound"] is True
    assert events[-1].payload["refs"][0]["claimed_by"] == ref["claimed_by"]


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

def test_the_operator_cli_renders_the_override_hint(board, unrelated, monkeypatch, capsys):
    """An operator shell has no dispatcher grant: the hint is useful there."""
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    tid = _claimed_card(board)
    assert _cli(board, monkeypatch, ["complete", tid, "--survivor-pr", PR,
                                     "--metadata", '{"changed_files": ["code.py"]}']) == 1
    assert "--survivor-unbound" in capsys.readouterr().err


@pytest.mark.parametrize("grant", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"])
def test_the_worker_cli_withholds_the_override_hint(board, unrelated, monkeypatch, capsys, grant):
    monkeypatch.setenv(grant, "t_whatever")
    tid = _claimed_card(board)
    assert _cli(board, monkeypatch, ["complete", tid, "--survivor-pr", PR,
                                     "--metadata", '{"changed_files": ["code.py"]}']) == 1
    err = capsys.readouterr().err
    assert "--survivor-unbound" not in err, "the escape must not be advertised to a worker"
    assert "does not name" in err and tid in err, "it must still explain the refusal"


@pytest.mark.parametrize("writer_grant", [None, "HERMES_KANBAN_TASK"])
def test_the_persisted_refusal_never_carries_the_override(board, unrelated, monkeypatch,
                                                          writer_grant):
    """The mitigation must not depend on WHO wrote the record.

    ``_hold`` writes the refusal to ``held_reason`` AND to a ``workspace_held``
    event, and ``kanban_show`` (worker toolset) replays events with full
    payloads. So an OPERATOR's refusal is read later by a worker redispatched
    onto the same card. Deciding the hint from the writer's environment is
    therefore defeated by the realistic sequence; the persisted text must be
    hint-free in BOTH writer shapes.
    """
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    if writer_grant:
        monkeypatch.setenv(writer_grant, "t_whatever")

    tid = _claimed_card(board)
    with pytest.raises(ValueError):
        kb.complete_task(board, tid, survivor_pr=PR, metadata={"changed_files": ["code.py"]})

    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held and "does not name" in (held[0] or ""), "the hold must explain itself"
    assert "--survivor-unbound" not in (held[0] or "")
    # The channel the worker actually reads: kanban_show replays event payloads.
    replayed = json.dumps([e.payload for e in kb.list_events(board, tid)])
    assert "--survivor-unbound" not in replayed


def test_a_redispatched_worker_cannot_read_the_override_off_its_own_card(
        board, unrelated, monkeypatch):
    """End to end on the sequence from the review: operator writes, worker reads.

    The expectation is computed independently of the hint code: it is the
    literal payload ``tools.kanban_tools._handle_show`` returns to a worker's
    model, scanned for the flag.
    """
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    tid = _claimed_card(board)

    # 1. An operator refuses the claim from a shell with no dispatcher grant.
    assert _cli(board, monkeypatch, ["complete", tid, "--survivor-pr", PR,
                                     "--metadata", '{"changed_files": ["code.py"]}']) == 1

    # 2. The card is redispatched; the worker reads its own history.
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kt, "_connect", lambda *a, **k: (kb, board))
    shown = kt._handle_show({"task_id": tid})
    assert tid in shown, "the card must still be readable"
    assert "--survivor-unbound" not in shown


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
