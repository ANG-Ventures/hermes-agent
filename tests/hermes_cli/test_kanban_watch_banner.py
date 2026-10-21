"""Watch identifies the board selected by the production command parser."""

import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_ops
from hermes_cli.kanban import kanban_command
from hermes_cli.kanban_parser import build_parser


@pytest.mark.kanban_bare_verb_refuse
@pytest.mark.parametrize(
    "environment,explicit,expected",
    # S7: a HERMES_KANBAN_BOARD in the environment is inert; watch names its board or is refused.
    [(None, "beta", "beta"), ("alpha", "beta", "beta"), ("beta", None, None)],
)
def test_watch_names_resolved_board(tmp_path, monkeypatch, capsys, environment, explicit, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("alpha")
    kb.create_board("beta")
    if environment:
        monkeypatch.setenv("HERMES_KANBAN_BOARD", environment)
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    argv = ["kanban"] + (["--board", explicit] if explicit else []) + ["watch"]

    def interrupt(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(kanban_ops.time, "sleep", interrupt)
    rc = kanban_command(parser.parse_args(argv))
    captured = capsys.readouterr()
    if expected is None:
        assert rc == 2
        assert "watch needs a board: pass --board <slug>" in captured.err
    else:
        assert rc == 0
        assert f"initial board '{expected}'" in captured.out
