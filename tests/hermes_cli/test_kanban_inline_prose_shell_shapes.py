"""Inline --body prose with shell shapes is refused when an agent types it into a shell (hermes-home t_b67cbb31).

The fleet's terminal guard hooks scan command TEXT; `-> .` inside a comment body read as a redirect into the live
tree. Ruling no-prose-exceptions-in-shell-scanners: the hooks stay strict and prose with shell shapes travels via
--body-file, which no hook scans. One refusal per shape per verb; --body-file and a program's argv pass.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_verb_input as kvi

SESSION = "20261102_180000_operator"
SHAPED = {"->": "_dep_python -> uv run", ">": "a > b", "|": "a | b", "$(": "see $(date) here", "`": "run `ls` here"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    for key in ("HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY", "HERMES_AGENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="builder", session_id=SESSION)
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human", expected_run_id=impl.current_run_id)
    return tid


@pytest.fixture
def typed(monkeypatch):
    monkeypatch.setattr(kvi, "_typed_in_agent_shell", lambda: True)


def _argv(verb, tid, flag_text):
    if verb == "create":
        return f"--board default create title --assignee alice {flag_text}"
    if verb == "request-changes":
        return f"request-changes {tid} {flag_text} --finding f --lens all=done --why"
    return f"comment {tid} {flag_text}"


@pytest.mark.parametrize("verb", kvi.INLINE_PROSE_VERBS)
@pytest.mark.parametrize("shape", list(SHAPED))
def test_inline_body_with_shell_shape_is_refused(board, typed, verb, shape):
    with kb.connect() as conn:
        before = len(kb.list_comments(conn, board)) if hasattr(kb, "list_comments") else None
    out = cli.run_slash(_argv(verb, board, "--body " + shlex.quote(SHAPED[shape])))
    assert "prose with shell shapes: use --body-file" in out, out
    assert f"`{shape}`" in out, out
    if before is not None:
        with kb.connect() as conn:
            assert len(kb.list_comments(conn, board)) == before


@pytest.mark.parametrize("verb", kvi.INLINE_PROSE_VERBS)
def test_body_file_carries_the_same_text(board, typed, tmp_path, verb):
    f = tmp_path / "b.md"
    f.write_text(" ".join(SHAPED.values()) + "\n", encoding="utf-8")
    out = cli.run_slash(_argv(verb, board, f"--body-file {f}"))
    assert "prose with shell shapes" not in out, out


def test_comment_positional_text_is_refused_too(board, typed):
    out = cli.run_slash(f"comment {board} " + shlex.quote("_dep_python -> uv run"))
    assert "prose with shell shapes: use --body-file" in out, out


def test_plain_inline_body_still_works(board, typed):
    out = cli.run_slash(f"comment {board} --body " + shlex.quote("plain words, no shapes"))
    assert "prose with shell shapes" not in out, out
    with kb.connect() as conn:
        assert any("plain words" in c.body for c in kb.list_comments(conn, board))


def test_program_argv_is_not_refused(board):
    """Not typed into an agent shell (a script / service passing argv): no refusal, the comment lands."""
    out = cli.run_slash(f"comment {board} --body " + shlex.quote("_dep_python -> uv run"))
    assert "prose with shell shapes" not in out, out


def test_detector_reads_the_terminal_wrapper(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT", "true")
    wrapper = ("bash -c source /tmp/snap.sh >/dev/null 2>&1 || true export AI_AGENT=\"${AI_AGENT:-hermes-agent}\" "
               "HERMES_AGENT=\"${HERMES_AGENT:-true}\" builtin cd -- /x || exit 126 eval 'hermes kanban comment'")
    monkeypatch.setattr(kvi, "_parent_args", lambda: wrapper)
    assert kvi._typed_in_agent_shell()
    monkeypatch.setattr(kvi, "_parent_args", lambda: "bash /tmp/script.sh")
    assert not kvi._typed_in_agent_shell()
    monkeypatch.setattr(kvi, "_parent_args", lambda: wrapper)
    monkeypatch.delenv("HERMES_AGENT")
    assert not kvi._typed_in_agent_shell()


def test_detector_on_a_real_process_tree(tmp_path):
    """The real reader: a child of a shell that carries the wrapper marks answers True, a grandchild does not."""
    probe = tmp_path / "p.py"
    probe.write_text("import sys; sys.path.insert(0, %r)\nfrom hermes_cli import kanban_verb_input as k\n"
                     "print(k._typed_in_agent_shell())\n" % str(Path(cli.__file__).resolve().parents[1]))
    script = tmp_path / "s.sh"
    script.write_text("%s %s\ntrue\n" % (sys.executable, probe))
    env = dict(os.environ, HERMES_AGENT="true")
    wrap = 'export HERMES_AGENT="${HERMES_AGENT:-true}"; eval \'%s\'; true'
    direct = subprocess.run(["bash", "-c", wrap % f"{sys.executable} {probe}"], env=env, capture_output=True,
                            text=True, timeout=30)
    nested = subprocess.run(["bash", "-c", wrap % f"bash {script}"], env=env, capture_output=True, text=True,
                            timeout=30)
    assert direct.stdout.strip() == "True", direct
    assert nested.stdout.strip() == "False", nested
