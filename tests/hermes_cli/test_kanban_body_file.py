"""--body-file tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# --body-file (t_f7e11e44): markdown with backticks / $(...) must reach the
# board byte-exact without ever passing through a double-quoted shell string.

HOSTILE = "run `whoami` then $(date -u)\n"


def test_create_body_file_path_is_byte_exact(kanban_home, tmp_path):
    f = tmp_path / "body.md"
    f.write_text(HOSTILE, encoding="utf-8")
    created = json.loads(kc.run_slash(f"create 't' --assignee alice --body-file {f} --json"))
    with kb.connect() as conn:
        # create_task prepends an origin stamp; the payload itself is literal.
        assert HOSTILE.strip() in kb.get_task(conn, created["id"]).body


def test_create_body_file_stdin(kanban_home, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(HOSTILE))
    created = json.loads(kc.run_slash("create 't' --assignee alice --body-file - --json"))
    with kb.connect() as conn:
        # create_task prepends an origin stamp; the payload itself is literal.
        assert HOSTILE.strip() in kb.get_task(conn, created["id"]).body


def test_create_body_and_body_file_conflict(kanban_home, tmp_path):
    f = tmp_path / "body.md"
    f.write_text("x", encoding="utf-8")
    out = kc.run_slash(f"create 't' --assignee alice --body y --body-file {f}")
    assert "mutually exclusive" in out
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_comment_body_file_stdin(kanban_home, monkeypatch):
    import io
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO(HOSTILE))
    kc.run_slash(f"comment {tid} --body-file -")
    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert HOSTILE.strip() in bodies


def test_comment_requires_exactly_one_body_source(kanban_home, tmp_path):
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    f = tmp_path / "c.md"
    f.write_text("hi", encoding="utf-8")
    assert "not both" in kc.run_slash(f"comment {tid} words --body-file {f}")
    assert "body required" in kc.run_slash(f"comment {tid}")
    with kb.connect() as conn:
        assert kb.list_comments(conn, tid) == []


def test_comment_positional_text_unchanged(kanban_home):
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    kc.run_slash(f"comment {tid} plain words here")
    with kb.connect() as conn:
        assert [c.body for c in kb.list_comments(conn, tid)] == ["plain words here"]


# #1166 regression (t_91dd1c39): `text` became nargs="*", so stock argparse
# bound it to [] on meeting --author and rejected the trailing text. Both
# orderings must parse, through the same top-level tree `hermes` builds.

def _cli(argv):
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="command")
    build = kc.build_parser(sub)
    build.set_defaults(func=kc.kanban_command)
    try:
        args = top.parse_args(["kanban", *argv])
    except SystemExit as exc:
        return exc.code
    return kc.kanban_command(args)


def _bodies(tid):
    with kb.connect() as conn:
        return [(c.author, c.body) for c in kb.list_comments(conn, tid)]


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["--author", "apollo", "before text"],
        ["before text", "--author", "apollo"],
        ["--author", "apollo", "before", "text"],
        ["before", "--author", "apollo", "text"],
    ],
)
def test_comment_author_either_side_of_text(kanban_home, argv_tail):
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    assert _cli(["comment", tid, *argv_tail]) == 0
    assert _bodies(tid) == [("apollo", "before text")]


def test_comment_author_before_text_via_slash(kanban_home):
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    out = kc.run_slash(f"comment {tid} --author apollo 'hello world'")
    assert "unrecognized" not in out
    assert _bodies(tid) == [("apollo", "hello world")]


def test_comment_body_file_alone_and_missing_body_rc(kanban_home, tmp_path, capsys):
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
    f = tmp_path / "c.md"
    f.write_text("from file", encoding="utf-8")
    assert _cli(["comment", tid, "--author", "apollo", "--body-file", str(f)]) == 0
    assert _bodies(tid) == [("apollo", "from file")]
    capsys.readouterr()
    assert _cli(["comment", tid, "--author", "apollo"]) == 2
    assert "comment body required (TEXT or --body-file)" in capsys.readouterr().err
    assert len(_bodies(tid)) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "t", "--body", "opening post", "--assignee", "alice", "--json"],
        ["create", "--assignee", "alice", "--body", "opening post", "t", "--json"],
    ],
)
def test_create_body_either_order(kanban_home, argv, capsys):
    assert _cli(argv) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.title == "t"
    assert "opening post" in task.body


def test_block_kind_before_reason(kanban_home):
    # Same class: `reason` is nargs="*" on block/schedule/promote.
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    args = top.parse_args(["kanban", "block", "t_x", "--kind", "needs_input", "why", "now"])
    assert args.reason == ["why", "now"] and args.kind == "needs_input"
    args = top.parse_args(["kanban", "schedule", "t_x", "--ids", "t_y", "--", "later"])
    assert args.reason == ["later"] and args.ids == ["t_y"]
