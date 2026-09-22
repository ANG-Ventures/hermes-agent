"""The four P1s FleetReview recorded on #848's head 0757be86, closed.

#848 merged (squash 8300bd7513) one hour after a trusted 5-member FleetReview
record on that exact head reported four unresolved P1. Two of them re-opened
the hole #848 was written to close.

Each test here is RED on 8300bd7513 and computes its expectation independently
of the function under test: the remote is a fixture whose content is fixed by
construction, the consequence asserted is a byte on disk or a refusal, and the
gate functions (``_reusable``, ``verify_ref``) are driven through the real
``preserve``/``complete_task`` path rather than called directly where the
finding is about a path, not a predicate.

  1. ``_reusable`` dropped the ``isinstance(ref, dict)`` guard every other
     ``previous["refs"]`` reader in the module applies. A non-dict entry raised
     ``AttributeError``, which is in NEITHER ``preserve``'s nor
     ``remove_workspace_dir``'s ``except`` tuple -- so it escaped past
     ``_hold()`` and skipped the fail-closed contract entirely.
  2. ``--survivor-unbound`` was invocation-wide: it stripped the task-id
     binding from EVERY claim, so an operator overriding one repository's claim
     silently accepted the others with no check that they name the card, and
     cost the correctly-bound ones their reclamation authority.
  3. An unbound-by-MENTION claim still satisfied the COMPLETION path.
     ``_reusable`` runs only with ``cleanup=True`` against a recorded row, so
     it never saw a claim arriving through ``_external``'s ``explicit`` arm.
  4. ``verify_ref`` keyed its matches by oid, so later ``ls-remote`` lines
     overwrote earlier ones: one SHA with several tips (an ordinary
     fast-forward leaving the topic branch alive, or a tag) collapsed to
     whichever refname was emitted last, and the binding -- now load-bearing on
     the explicit path too -- was tested against an arbitrary ref.
"""
import contextlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as ks
from hermes_cli import kanban_external_survivor as ext

HEAD = "a1" * 20
MERGE = "b2" * 20
PR = "example/project#68"
PR2 = "example/other#12"
URL = "https://github.com/example/project.git"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    """A scriptable remote: ``prs`` per slug, ``refs`` as raw ls-remote lines.

    The seam is ``subprocess.run``, strictly below ``_query``, so nothing under
    test is ever consulted about what the remote said.
    """
    state = {
        "prs": {},
        "refs": [f"{HEAD}\trefs/heads/someone-elses/unrelated-work"],
    }
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            slug = args[args.index("--repo") + 1]
            view = state["prs"].get(slug)
            if view is None:
                return subprocess.CompletedProcess(args, 1, b"", b"gh: not found")
            return subprocess.CompletedProcess(args, 0, json.dumps(view).encode(), b"")
        if "ls-remote" in args and "-C" not in args:
            body = "".join(f"{line}\n" for line in state["refs"])
            return subprocess.CompletedProcess(args, 0, body.encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def _pr(branch="someone-elses/unrelated-work", title="work", body="work"):
    return {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE},
            "headRefName": branch, "title": title, "body": body}


def _card(board, *, workspace=True):
    tid = kb.create_task(board, title="external implementation")
    if workspace:
        ws = kb.resolve_workspace(kb.get_task(board, tid))
        kb.set_workspace_path(board, tid, ws)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "implementation.py").write_text("work that lives nowhere else\n")
    return tid


# --- finding 1: _reusable must not crash past the fail-closed contract ------

def test_a_malformed_recorded_ref_holds_instead_of_escaping_the_contract(board):
    """A non-dict ``refs`` entry must HOLD, never raise ``AttributeError``.

    RED on 8300bd7513: ``_reusable`` did ``ref.get("unbound")`` unguarded, so a
    recorded row holding a non-dict entry raised ``AttributeError`` -- absent
    from ``preserve``'s ``except (OSError, sqlite3.Error,
    subprocess.SubprocessError, ValueError)`` tuple, so it escaped past
    ``_hold()``. The expectation is computed independently: the row is written
    here by hand, and the contract asserted is the module's own documented one
    (``SurvivorUnavailable`` is a ``ValueError``, and a ``held_reason`` lands).
    """
    tid = _card(board, workspace=False)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    shutil.rmtree(ws, ignore_errors=True)
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor",
            (tid, json.dumps({"kind": "ref", "refs": ["not-a-dict"]})),
        )

    with pytest.raises(ValueError):
        ks.preserve(board, tid, cleanup=True, workspace=ws)

    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held and held[0], "the fail-closed contract must have recorded a HOLD"


def test_the_reaper_holds_on_a_malformed_recorded_ref_rather_than_raising(board):
    """The consequence at the reaper: ``remove_workspace_dir`` returns False.

    RED on 8300bd7513: its own ``except`` tuple does not catch ``AttributeError``
    either, so the reaper tick RAISED instead of holding -- the same class of
    outage as the ``_repos`` PermissionError incident. Driven on the real
    reclamation path that reaches ``_reusable``: the recorded repository has
    vanished from an otherwise empty workspace, so the recorded row is the sole
    authority. The bytes asserted are written and read back here.
    """
    tid = _card(board, workspace=False)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    good = {"remote": URL, "branch": f"refs/heads/kanban/{tid}", "sha": HEAD,
            "repository": "repo", "external": True}
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, "
            "survivor = excluded.survivor",
            (tid, json.dumps({"repo": HEAD}),
             json.dumps({"kind": "ref", "refs": [["still", "not", "a", "dict"], good]})),
        )

    assert ks.remove_workspace_dir(board, tid, ws) is False
    assert ws.is_dir(), "the workspace must survive a malformed recovery index"
    held = board.execute(
        "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,)).fetchone()
    assert held and held[0], "the reaper must HOLD, not raise"


def test_the_same_reaper_rig_without_the_malformed_entry_still_reclaims(board):
    """Anti-vacuity: one entry different, and the reaper proceeds.

    Without this, the test above passes for a reclamation path that refuses
    everything -- which would be a different, equally real, defect.
    """
    tid = _card(board, workspace=False)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    good = {"remote": URL, "branch": f"refs/heads/kanban/{tid}", "sha": HEAD,
            "repository": "repo", "external": True}
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases, "
            "survivor = excluded.survivor",
            (tid, json.dumps({"repo": HEAD}),
             json.dumps({"kind": "ref", "refs": [good]})),
        )

    assert ks.remove_workspace_dir(board, tid, ws) is True
    assert not ws.exists()


def test_a_well_formed_bound_row_is_still_reused(board):
    """Anti-vacuity for the two above: same rig, a DICT ref, and it is REUSED."""
    tid = _card(board, workspace=False)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    shutil.rmtree(ws, ignore_errors=True)
    row = {"kind": "ref", "refs": [{"remote": URL, "branch": f"refs/heads/kanban/{tid}",
                                    "sha": HEAD, "repository": "."}]}
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor",
            (tid, json.dumps(row)),
        )

    assert ks.preserve(board, tid, cleanup=True, workspace=ws)["refs"][0]["sha"] == HEAD


# --- finding 2: the override must be PER-CLAIM ------------------------------

def test_the_override_does_not_unbind_the_other_claims_in_the_invocation(board, remote):
    """Overriding one repository's claim must not accept the others unchecked.

    RED on 8300bd7513: ``unbound`` was one parameter applied uniformly
    (``mined_for=None if unbound else task_id``), so the second, UNRELATED claim
    -- a typo, a stale number, a pasted PR -- was silently recorded as that
    repository's provenance. The ground truth is fixed by construction: neither
    PR names the card, and the operator vouched for exactly one of them.
    """
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch="totally/unrelated")
    remote["prs"]["example/other"] = _pr(branch="also/unrelated")

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_pr=[f"kept={PR}", f"gone={PR2}"],
                         survivor_unbound=["gone"])

    # Only `gone` was vouched for. `kept` names nothing, so it must still be
    # refused on its own merits rather than ride the other claim's override.
    assert "does not name" in str(excinfo.value) and PR in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"


def test_an_overridden_claim_does_not_cost_its_siblings_their_binding(board, remote):
    """The second harm: a bound sibling must stay BOUND, i.e. still reusable.

    RED on 8300bd7513: ``_verified_explicit`` stamped ``unbound=True`` on every
    claim in the invocation, so ``_reusable`` refused the whole recorded row and
    the correctly-bound claims lost their reclamation authority too. Asserted on
    the recorded row and then on the real reclamation pass.
    """
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch=f"kanban/{tid}-fix")
    remote["prs"]["example/other"] = _pr(branch="totally/unrelated")

    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                            survivor_pr=[f"kept={PR}", f"gone={PR2}"],
                            survivor_unbound=["gone"])

    refs = {r["repository"]: r for r in
            kb.latest_run(board, tid).metadata["survivor"]["refs"]}
    assert refs["gone"]["unbound"] is True, "the overridden claim is the unbound one"
    assert not refs["kept"].get("unbound"), "its sibling must keep its binding"


def test_a_bare_override_is_refused_when_it_cannot_say_which_claim(board, remote):
    """A bare flag on a multi-claim invocation is exactly the old behaviour."""
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch=f"kanban/{tid}-fix")
    remote["prs"]["example/other"] = _pr(branch="totally/unrelated")

    with pytest.raises(ValueError, match="per-claim"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_pr=[f"kept={PR}", f"gone={PR2}"],
                         survivor_unbound=True)
    assert kb.get_task(board, tid).status != "done"


def test_a_bare_override_still_works_for_the_single_claim_shape(board, remote):
    """Anti-vacuity: the historical spelling is not broken by the narrowing."""
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch="totally/unrelated")

    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                            survivor_pr=PR, survivor_unbound=True)
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["unbound"] is True


def test_an_override_naming_no_claim_is_refused(board, remote):
    """A typo'd override must not read as though the binding had been relaxed."""
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch="totally/unrelated")

    with pytest.raises(ValueError, match="names no claim"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]},
                         survivor_pr=[f"kept={PR}"], survivor_unbound=["kpet"])


# --- the CLI surface for the per-claim override ------------------------------

def test_the_cli_parses_the_per_claim_override_and_reaches_the_kernel(board, remote, monkeypatch):
    """The narrowed override is only real if the CLI can express it.

    RED on 8300bd7513: ``--survivor-unbound`` was ``action="store_true"``, so
    ``--survivor-unbound gone`` did not parse as an override of that claim at all
    -- argparse took ``gone`` as a positional. The expectation is read off the
    RECORDED row, not off the parser: the vouched-for claim is unbound and its
    sibling is not.
    """
    import argparse
    import contextlib as _contextlib

    from hermes_cli import kanban as cli

    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch=f"kanban/{tid}-fix")
    remote["prs"]["example/other"] = _pr(branch="totally/unrelated")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: _contextlib.nullcontext(board))
    argv = ["kanban", "complete", tid,
            "--survivor-pr", f"kept={PR}", "--survivor-pr", f"gone={PR2}",
            "--survivor-unbound", "gone",
            "--metadata", '{"changed_files": ["code.py"]}']

    assert cli.kanban_command(parser.parse_args(argv)) == 0

    refs = {r["repository"]: r for r in
            kb.latest_run(board, tid).metadata["survivor"]["refs"]}
    assert refs["gone"]["unbound"] is True
    assert not refs["kept"].get("unbound")


def test_the_cli_bare_override_is_unchanged_for_a_single_claim(board, remote, monkeypatch):
    """Anti-vacuity: the historical spelling must still parse and still work."""
    import argparse
    import contextlib as _contextlib

    from hermes_cli import kanban as cli

    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch="totally/unrelated")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing",
                        lambda *a, **k: _contextlib.nullcontext(board))
    argv = ["kanban", "complete", tid, "--survivor-pr", PR, "--survivor-unbound",
            "--metadata", '{"changed_files": ["code.py"]}']

    assert cli.kanban_command(parser.parse_args(argv)) == 0
    assert kb.latest_run(board, tid).metadata["survivor"]["refs"][0]["unbound"] is True


# --- finding 3: a MENTION must not satisfy the completion path either -------

def test_a_mention_cannot_delete_the_workspace_on_the_completion_path(board, remote):
    """The hole ``_reusable`` structurally could not see.

    RED on 8300bd7513: a title/body match was marked ``unbound`` and RETURNED as
    an explicit survivor, and ``_external``'s ``explicit`` arm is taken before
    the ``cleanup`` arm ``_reusable`` guards -- so "does not address t_..."
    authorised the completion, and ``preserve`` treats a verified explicit
    survivor as authority over unpushed work. The byte asserted is written and
    read back in this test.
    """
    tid = _card(board)
    ws = Path(kb.get_task(board, tid).workspace_path)
    remote["prs"]["example/project"] = _pr(body=f"follow-up to {tid}; does not address it")

    with pytest.raises(ValueError, match="mention"):
        kb.complete_task(board, tid, survivor_pr=PR,
                         metadata={"changed_files": ["code.py"]})

    assert (ws / "implementation.py").read_text() == "work that lives nowhere else\n"
    assert kb.get_task(board, tid).status != "done"


def test_the_completion_and_reclamation_paths_agree_on_a_mention(board, remote):
    """The two paths must answer the SAME question the same way.

    The defect was a disagreement, not a missing check: reclamation refused a
    mention while completion accepted it. Drive both against one fixture and
    assert the verdicts match, without either reading the other's code.
    """
    tid = _card(board, workspace=False)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    remote["prs"]["example/project"] = _pr(title=f"umbrella changelog mentioning {tid}")

    verdicts = []
    for call in (
        lambda: ks.preserve(board, tid, metadata={"changed_files": ["code.py"]},
                            survivor_pr=PR),
        lambda: ks.preserve(board, tid, cleanup=True, workspace=ws, survivor_pr=PR),
    ):
        with contextlib.suppress(ks.SurvivorUnavailable):
            verdicts.append(bool(call()))
            continue
        verdicts.append(False)

    assert verdicts == [False, False], "a mention must not be authority on EITHER path"


def test_a_branch_bound_claim_still_completes(board, remote):
    """Anti-vacuity for the mention tests: a real binding is still accepted."""
    tid = _card(board, workspace=False)
    remote["prs"]["example/project"] = _pr(branch=f"kanban/{tid}-fix")

    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["code.py"]})
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert ref["corroborated_by"] == "branch" and not ref.get("unbound")


# --- finding 4: an ambiguous SHA must be REFUSED, not resolved by luck ------

def test_an_ambiguous_sha_is_not_bound_against_an_arbitrary_tip(board, remote):
    """One SHA, several tips: the binding must not depend on emission order.

    RED on 8300bd7513: ``matches[oid] = ref`` kept only the LAST line, so
    ``len(matches)`` was still 1 and the binding was evaluated against whichever
    refname ``ls-remote`` emitted last. refname-sorted output puts
    ``refs/heads/main`` after ``refs/heads/kanban/<tid>-...``, which is the
    ordinary fast-forward-with-topic-branch-alive shape.

    The expectation is computed independently: BOTH orderings are driven over
    the same two refs and the two verdicts must be EQUAL. That is an invariant
    of the function, not a snapshot of its current answer, so it cannot be
    satisfied by luck in one direction.
    """
    tid = "t_ambiguous"
    topic = f"{HEAD}\trefs/heads/kanban/{tid}-fix"
    main = f"{HEAD}\trefs/heads/main"

    remote["refs"] = [topic, main]
    last_is_main = ext.verify_ref(f"{URL}#{HEAD}", mined_for=tid)
    remote["refs"] = [main, topic]
    last_is_topic = ext.verify_ref(f"{URL}#{HEAD}", mined_for=tid)

    assert last_is_main == last_is_topic, "the verdict must not depend on ls-remote order"


def test_an_ambiguous_sha_that_the_binding_cannot_narrow_is_refused(board, remote):
    """Two tips that BOTH name the card: refuse rather than pick a provenance.

    ``branch`` is persisted as this survivor's provenance and read back by
    reclamation, so choosing one of several would publish a recovery-index entry
    that is a guess -- the same harm the module refuses when one operator claim
    would vouch for several missing repositories.
    """
    tid = "t_ambiguous"
    remote["refs"] = [f"{HEAD}\trefs/heads/kanban/{tid}-a",
                      f"{HEAD}\trefs/heads/kanban/{tid}-b"]

    assert ext.verify_ref(f"{URL}#{HEAD}", mined_for=tid) is None


def test_the_binding_still_narrows_a_tag_beside_the_topic_branch(board, remote):
    """Anti-vacuity: an ambiguity the binding CAN resolve must still resolve.

    A commit does not stop naming the card because it also carries a tag, so
    refusing every multi-tip SHA would kill the feature rather than fix it.
    """
    tid = "t_ambiguous"
    remote["refs"] = [f"{HEAD}\trefs/heads/kanban/{tid}-fix",
                      f"{HEAD}\trefs/tags/v1.2.3"]

    verified = ext.verify_ref(f"{URL}#{HEAD}", mined_for=tid)
    assert verified is not None
    assert verified["branch"] == f"refs/heads/kanban/{tid}-fix"
    assert verified["corroborated_by"] == "branch"
