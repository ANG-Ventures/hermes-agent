"""S7 RC-12: no kanban help text teaches a current board, the pointer file or the board env var.

S7 deleted the global board pointer (``kanban/current``, ``boards switch``) and the
``HERMES_KANBAN_BOARD`` variable; a verb names its board (``--board``) or its card does.
Help that still says "the current board" sends the reader looking for a selector that no
longer exists. The one allowed phrasing is the negative statement "There is no current board".
"""
import argparse
import re

from hermes_cli import kanban as kc

_FORBIDDEN = re.compile(r"(?<!no )current board|kanban/current|HERMES_KANBAN_BOARD", re.I)


def _walk_help(parser: argparse.ArgumentParser, path: str = "kanban"):
    if parser.description:
        yield path, parser.description
    for action in parser._actions:
        if action.help and action.help is not argparse.SUPPRESS:
            yield f"{path} {'/'.join(action.option_strings) or action.dest}", action.help
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                if choice.help:
                    yield f"{path} {choice.dest}", choice.help
            for name, sub in action.choices.items():
                yield from _walk_help(sub, f"{path} {name}")


def test_cli_help_never_teaches_a_current_board():
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers(dest="command"))
    hits = sorted({(where, text) for where, text in _walk_help(root) if _FORBIDDEN.search(text)})
    assert hits == []


def test_slash_help_never_teaches_a_current_board():
    assert not _FORBIDDEN.search(kc._SLASH_KANBAN_HELP), _FORBIDDEN.findall(kc._SLASH_KANBAN_HELP)
