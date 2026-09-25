"""The ``kanban_complete`` TOOL must reach the survivor escape hatch it names.

`preserve()` refuses a completion whose implementation it cannot find and points
at ``--survivor-pr`` / ``--survivor-ref``. That remedy was reachable from the CLI
and the library but NOT from the tool — the agent-facing surface where the
refusal is actually read, so a worker read a hint it had no way to act on.

These tests pin the remedy AND the guard: a remote-VERIFIED operator claim
satisfies the card through the tool, anything less still fails closed, and the
tool is never a softer path than the flag.
"""
from __future__ import annotations

import hashlib
import os
import json
import subprocess
from pathlib import Path

import pytest

HEAD = "a1" * 20
MERGE = "b2" * 20
PR = "example/project#68"
URL = "https://github.com/example/project.git"
STALE = "af0d85e37470550d554abb89a5cd51039dbbe358"


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a claimed task this process OWNS."""
    import os

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    for pin in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(pin, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    # This process owns the run, so the expected_run_id guard applies — the
    # property a shell-out to the CLI silently loses.
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(os.getpid()))
    return tid


@pytest.fixture
def remote(monkeypatch):
    """Answer remote lookups affirmatively; no gate may rest on a network failure.

    ``headRefName`` starts UNRELATED to any card on purpose. An explicit
    ``--survivor-pr``/``survivor_pr`` must corroborate the card that names it
    (kanban t_de2e348e), so existence is not relevance: a test wanting the
    happy path calls :func:`names_card`, one wanting the refusal leaves it.
    """
    state = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE},
             "headRefName": "someone/unrelated-work", "title": "", "body": ""}
    calls = []
    real = subprocess.run

    def run(args, **kwargs):
        if args and args[0] == "gh":
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, json.dumps(state).encode(), b"")
        if "ls-remote" in args:
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args, 0, f"{HEAD}\trefs/heads/feature\n".encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state, calls


def names_card(remote, tid):
    """Make the claimed PR corroborate THIS card, the way a real one would."""
    remote[0]["headRefName"] = f"operator/{tid}-landed-elsewhere"
    return remote


def stale_bases(tid):
    """Make the card's recorded repository vanish while its workspace survives.

    This is the exact state whose refusal names ``--survivor-pr``.
    """
    from hermes_cli import kanban_db as kb
    with kb.connect_closing() as conn:
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
    return ws


def complete(**args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_complete(args))


def task_state(tid):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid), kb.latest_run(conn, tid)


def test_tool_survivor_pr_completes_a_stale_bases_card(worker_env, remote):
    """The case that was unreachable from the tool: dir exists, repo gone."""
    stale_bases(worker_env)
    names_card(remote, worker_env)

    out = complete(summary="approved", survivor_pr=PR)

    assert "error" not in out, out
    task, run = task_state(worker_env)
    assert task.status == "done"
    ref = run.metadata["survivor"]["refs"][0]
    assert ref["pr"] == PR and ref["sha"] == MERGE
    assert remote[1], "must consult the remote, not accept the tool argument"


def test_the_tool_cannot_complete_on_a_pr_that_does_not_name_the_card(worker_env, remote):
    """Teeth for the test above: existence is not relevance on the TOOL surface.

    The fixture PR is live and MERGED but names some other branch. #848 binds
    an explicit claim to the card, and the tool must inherit that binding
    rather than be the softer path -- otherwise a worker closes its own card
    against any live PR and the workspace's bytes lose their protection.
    """
    ws = stale_bases(worker_env)

    out = complete(summary="approved", survivor_pr=PR)

    assert "does not name" in out.get("error", ""), out
    assert task_state(worker_env)[0].status != "done"
    assert (ws / "qa-output" / "verdict.md").is_file(), "the workspace must survive"


@pytest.mark.parametrize("smuggled", [
    {"survivor_unbound": True},
    {"unbound": True},
    {"survivor_unbound": "1"},
    {"metadata": {"survivor_unbound": True}},
])
def test_the_tool_cannot_smuggle_the_operator_override(worker_env, remote, smuggled):
    """RUNTIME arm for the CLI-only override, not a source grep.

    `--survivor-unbound` accepts a live PR with NO tie to the card. It is an
    operator flag; the tool surface must have no expression of it at all, so
    drive the real tool handler with each plausible spelling and assert the
    binding still refuses.
    """
    ws = stale_bases(worker_env)

    out = complete(summary="approved", survivor_pr=PR, **smuggled)

    assert out.get("error"), out
    assert task_state(worker_env)[0].status != "done"
    assert (ws / "qa-output" / "verdict.md").is_file(), "the workspace must survive"


def test_tool_survivor_pr_that_does_not_verify_still_refuses(worker_env, remote):
    """A tool argument is a claim, not authority."""
    remote[0]["state"] = "CLOSED"
    stale_bases(worker_env)

    out = complete(summary="approved", survivor_pr=PR)

    assert "survivor" in out.get("error", "").lower(), out
    assert task_state(worker_env)[0].status != "done"


def test_tool_cannot_satisfy_the_guard_from_prose_alone(worker_env, remote):
    """Without the argument, naming the PR in the summary must not complete it.

    Text is a hint; the relaxed stale-bases branch is reachable only by an
    explicit, verified claim. If prose sufficed, the new argument would be
    decoration and the guard would already be gone.
    """
    stale_bases(worker_env)

    out = complete(summary=f"approved, shipped {PR} at {HEAD}",
                   metadata={"changed_files": ["code.py"]})

    assert "survivor" in out.get("error", "").lower(), out
    assert task_state(worker_env)[0].status != "done"


def test_tool_survivor_ref_rejection_is_redacted(worker_env, remote):
    """An unverifiable ref may carry a token: the tool's error must not echo it."""
    leaky = f"https://oauth2:ghp_SECRET123@github.com/example/project.git#{'c3' * 20}"

    out = complete(summary="approved", survivor_ref=leaky)

    error = out.get("error", "")
    assert "could not verify" in error, out
    assert "ghp_SECRET123" not in error
    assert "oauth2" not in error

    from hermes_cli import kanban_db as kb
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
            (worker_env,)).fetchall()
        assert all("ghp_SECRET123" not in (r[0] or "") for r in rows)
        assert not any("ghp_SECRET123" in json.dumps(e.payload)
                       for e in kb.list_events(conn, worker_env))


def test_tool_schema_exposes_both_survivor_arguments():
    """The refusal names a remedy; the schema is what makes it callable.

    A multi-repository loss needs one claim PER vanished repository, so the
    schema has to admit an array too — a ``"type": "string"`` here would print
    a remedy the tool's own validation rejects, with no accepted input at all.
    """
    from tools import kanban_tools as kt
    props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert {"survivor_pr", "survivor_ref"} <= set(props)
    for key in ("survivor_pr", "survivor_ref"):
        assert props[key]["type"] == ["string", "array"], props[key]
        assert props[key]["items"] == {"type": "string"}
        # The qualifier is the load-bearing part of the remedy: an unqualified
        # list is refused by the kernel as ambiguous.
        assert "=" in props[key]["description"]


def test_non_string_survivor_argument_is_rejected_not_coerced(worker_env, remote):
    """A malformed claim must fail loudly, never reach the verifier as junk."""
    out = complete(summary="approved", survivor_pr={"repo": "example/project"})
    assert "survivor_pr must be a string" in out.get("error", ""), out
    assert remote[1] == [], "must not consult the remote on a malformed claim"


# --- multi-repository loss: one claim per vanished repository ---------------

OTHER = "example/other#9"


@pytest.fixture
def remote_multi(monkeypatch):
    """Answer per-repository, so a copied ref cannot masquerade as provenance."""
    calls = []
    real = subprocess.run

    def oid(slug, number):
        # Deterministic, distinct per (repo, PR) — the whole point is that ref
        # N is not ref 0 wearing a different repository key.
        return hashlib.sha1(f"{slug}#{number}".encode()).hexdigest()

    def run(args, **kwargs):
        if args and args[0] == "gh":
            calls.append(list(args))
            number = args[args.index("view") + 1]
            slug = args[args.index("--repo") + 1]
            # #848 (landed after this fixture was written) requires a live PR to
            # NAME the card it vouches for; corroborate through headRefName the
            # same way the single-repo fixture above does.
            tid = os.environ.get("HERMES_KANBAN_TASK", "")
            payload = {"state": "MERGED", "headRefOid": oid(slug, number),
                       "mergeCommit": {"oid": oid(slug, number)},
                       "headRefName": f"operator/{tid}-landed-elsewhere",
                       "title": "", "body": ""}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def two_vanished_repos(tid):
    """Two recorded repositories gone while the workspace's evidence survives.

    This is the state whose refusal prints
    ``--survivor-pr <repo>=owner/repo#N`` — the remedy the tool could not send.
    """
    from hermes_cli import kanban_db as kb
    with kb.connect_closing() as conn:
        ws = kb.resolve_workspace(kb.get_task(conn, tid))
        (ws / "qa-output").mkdir(parents=True, exist_ok=True)
        (ws / "qa-output" / "verdict.md").write_text("APPROVED\n")
        kb.set_workspace_path(conn, tid, ws)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
                (tid, json.dumps({"gone-a": STALE, "gone-b": STALE})),
            )
    return ws


def test_tool_records_distinct_provenance_per_vanished_repository(worker_env, remote_multi):
    """The card: two qualified claims through the TOOL must COMPLETE...

    ...and record TWO refs whose provenance is genuinely per-repository. A
    second ref that is a copy of the first is the recovery-index corruption
    this whole line of work exists to prevent.
    """
    two_vanished_repos(worker_env)

    out = complete(summary="approved",
                   survivor_pr=[f"gone-a={PR}", f"gone-b={OTHER}"])

    assert "error" not in out, out
    task, run = task_state(worker_env)
    assert task.status == "done"
    refs = run.metadata["survivor"]["refs"]
    assert len(refs) == 2, refs
    by_repo = {ref["repository"]: ref for ref in refs}
    assert set(by_repo) == {"gone-a", "gone-b"}
    assert by_repo["gone-a"]["pr"] == PR
    assert by_repo["gone-b"]["pr"] == OTHER
    # Distinct provenance, not one verification stamped onto both.
    assert by_repo["gone-a"]["sha"] != by_repo["gone-b"]["sha"]
    assert by_repo["gone-a"]["remote"] != by_repo["gone-b"]["remote"]
    assert len(remote_multi) == 2, "each claim must be verified on its own"


def test_tool_partial_multi_claim_still_refuses(worker_env, remote_multi):
    """A list is not a licence to under-cover: one claim, two lost repos."""
    two_vanished_repos(worker_env)

    out = complete(summary="approved", survivor_pr=[f"gone-a={PR}"])

    error = out.get("error", "")
    assert "survivor" in error.lower(), out
    assert "gone-b" in error, error
    assert task_state(worker_env)[0].status != "done"


def test_tool_single_bare_string_survivor_is_unchanged(worker_env, remote_multi):
    """The historical scalar shape must keep working exactly as before."""
    stale_bases(worker_env)

    out = complete(summary="approved", survivor_pr=PR)

    assert "error" not in out, out
    task, run = task_state(worker_env)
    assert task.status == "done"
    refs = run.metadata["survivor"]["refs"]
    assert len(refs) == 1 and refs[0]["pr"] == PR


def test_tool_non_string_element_in_survivor_list_is_refused(worker_env, remote_multi):
    """Widening the container must not widen what an element may be."""
    two_vanished_repos(worker_env)

    for bad in (7, {"repo": "example/project"}, [PR]):
        out = complete(summary="approved", survivor_pr=[f"gone-a={PR}", bad])
        error = out.get("error", "")
        assert "survivor_pr must be a string or a list of strings" in error, out
        assert type(bad).__name__ in error, error
        assert task_state(worker_env)[0].status != "done"
    assert remote_multi == [], "must not consult the remote on a malformed claim"


def test_tool_all_blank_survivor_list_normalizes_to_none(worker_env, remote_multi):
    """An empty claim must not reach the verifier as a survivor nobody made."""
    two_vanished_repos(worker_env)

    for blank in ([], ["   "], ["", "  "]):
        out = complete(summary="approved", survivor_pr=blank)
        # No claim was made, so this is the plain fail-closed refusal — never
        # a completion, and never a bogus [""] claim sent to the remote.
        assert "survivor" in out.get("error", "").lower(), (blank, out)
        assert task_state(worker_env)[0].status != "done"
    assert remote_multi == [], "a blank claim must not be verified as one"
