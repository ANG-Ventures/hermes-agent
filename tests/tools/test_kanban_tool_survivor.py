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
    """Answer remote lookups affirmatively; no gate may rest on a network failure."""
    state = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
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

    out = complete(summary="approved", survivor_pr=PR)

    assert "error" not in out, out
    task, run = task_state(worker_env)
    assert task.status == "done"
    ref = run.metadata["survivor"]["refs"][0]
    assert ref["pr"] == PR and ref["sha"] == MERGE
    assert remote[1], "must consult the remote, not accept the tool argument"


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
    """The refusal names a remedy; the schema is what makes it callable."""
    from tools import kanban_tools as kt
    props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert {"survivor_pr", "survivor_ref"} <= set(props)
    assert all(props[k]["type"] == "string" for k in ("survivor_pr", "survivor_ref"))


def test_non_string_survivor_argument_is_rejected_not_coerced(worker_env, remote):
    """A malformed claim must fail loudly, never reach the verifier as junk."""
    out = complete(summary="approved", survivor_pr={"repo": "example/project"})
    assert "survivor_pr must be a string" in out.get("error", ""), out
    assert remote[1] == [], "must not consult the remote on a malformed claim"
