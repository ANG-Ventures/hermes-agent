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
