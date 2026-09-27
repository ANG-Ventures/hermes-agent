"""``--survivor-pr`` is verified over REST, and ``complete`` says why it did not close.

t_1e080b8d: ``verify_pr`` asked ``gh pr view`` (GraphQL) and GraphQL answered
``Could not resolve to a Repository with the name 'Kyzcreig/fleetreview-router'``
three times while ``gh api repos/Kyzcreig/fleetreview-router/pulls/170`` (REST)
answered merged=true. GraphQL is also ONE per-user rate-limit bucket shared by
every fleet host. The operator got a HELD card and no usable reason at the CLI.
"""
import argparse
import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.hermes_cli._survivor_gh_fake import pr_target

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_external_survivor as ext

HEAD = "9a" * 20
MERGE = "42" * 20
PR = "example/router#170"
GRAPHQL_ERR = (b"GraphQL: Could not resolve to a Repository with the name "
               b"'example/router'. (repository)")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ext, "_QUERY_BACKOFF", 0)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def github(monkeypatch):
    """GitHub as measured on 2026-09-25: GraphQL fails to resolve, REST answers."""
    state = {"branch": "fix/unrelated", "calls": []}
    real = subprocess.run

    def run(args, **kwargs):
        if args and args[0] == "gh":
            state["calls"].append(list(args))
            if args[1:3] == ["pr", "view"] or "graphql" in args:
                return subprocess.CompletedProcess(args, 1, b"", GRAPHQL_ERR)
            if pr_target(args) == ("example/router", "170"):
                payload = {"state": "closed", "merged": True,
                           "merged_at": "2026-09-25T20:00:00Z",
                           "merge_commit_sha": MERGE,
                           "head": {"sha": HEAD, "ref": state["branch"]},
                           "title": "fix(budget): no re-charge", "body": None}
                return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")
            return subprocess.CompletedProcess(args, 1, b"", b"gh: Not Found (HTTP 404)")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def _cli(board, monkeypatch, argv):
    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing", lambda *a, **k: contextlib.nullcontext(board))
    # An operator shell has no chat-session identity; the worker running this
    # suite does, and the home-session guard would refuse before the survivor.
    monkeypatch.setattr(cli, "_caller_session_id", lambda: None)
    return cli.kanban_command(parser.parse_args(["kanban", *argv]))


def test_verify_pr_reads_rest_not_graphql(github):
    github["branch"] = "fix/budget-restart-rebill-t_62c6a323"
    verified = ext.verify_pr(PR, mined_for="t_62c6a323")
    assert verified == {"remote": "https://github.com/example/router.git",
                        "branch": "refs/pull/170/head", "sha": MERGE, "pr": PR,
                        "state": "MERGED", "external": True, "corroborated_by": "branch"}
    assert github["calls"] == [["gh", "api", "repos/example/router/pulls/170"]]


@pytest.mark.parametrize("payload,state,oid", [
    ({"state": "open", "merged": False, "merge_commit_sha": "77" * 20}, "OPEN", HEAD),
    ({"state": "closed", "merged": False, "merged_at": None}, None, None),
])
def test_rest_state_mapping(monkeypatch, payload, state, oid):
    """OPEN keys on the head (REST's test-merge sha is not a merge); CLOSED is refused."""
    payload = dict(payload, head={"sha": HEAD, "ref": "kanban/t_abc12345-fix"})
    monkeypatch.setattr(ext, "_query", lambda args: json.dumps(payload))
    verified = ext.verify_pr(PR, mined_for="t_abc12345")
    if state is None:
        assert verified is None
    else:
        assert (verified["state"], verified["sha"]) == (state, oid)


def test_graphql_outage_no_longer_holds_a_bound_survivor(board, github):
    tid = kb.create_task(board, title="budget fix")
    github["branch"] = f"fix/budget-restart-rebill-{tid}"
    assert kb.complete_task(board, tid, survivor_pr=PR,
                            metadata={"changed_files": ["budget.py"]})
    assert kb.get_task(board, tid).status == "done"
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert (ref["pr"], ref["sha"], ref["corroborated_by"]) == (PR, MERGE, "branch")


def test_cli_complete_prints_the_hold_reason_and_fails(board, github, monkeypatch, capsys):
    tid = kb.create_task(board, title="budget fix")
    github["branch"] = f"fix/budget-restart-rebill-{tid}"
    rc = _cli(board, monkeypatch, ["complete", tid, "--result", "shipped", "--survivor-pr", "example/router#999"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "survivor_unavailable" in err and "could not verify" in err
    assert kb.get_task(board, tid).status != "done"


def test_cli_complete_names_why_a_card_was_routed_not_closed(board, monkeypatch, capsys):
    """A complete that ends in review must say so and why -- never a bare success."""
    tid = kb.create_task(board, title="open pr card")

    def route(conn, task_id, **kwargs):
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "completion_routed_to_review",
                             {"open_prs": ["example/router#171"], "note": ""})
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        return True

    monkeypatch.setattr(kb, "complete_task", route)
    assert _cli(board, monkeypatch, ["complete", tid, "--result", "shipped"]) == 0
    out = capsys.readouterr().out
    assert f"Routed {tid} to review, NOT done" in out and "example/router#171" in out


def test_cli_complete_prints_a_refused_route_reason(board, monkeypatch, capsys):
    tid = kb.create_task(board, title="refused route card")

    def refuse(conn, task_id, **kwargs):
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "completion_route_refused",
                             {"open_prs": ["example/router#171"],
                              "reason": "reviewer equals implementer"})
        return False

    monkeypatch.setattr(kb, "complete_task", refuse)
    assert _cli(board, monkeypatch, ["complete", tid, "--result", "shipped"]) == 1
    assert "reviewer equals implementer" in capsys.readouterr().err
