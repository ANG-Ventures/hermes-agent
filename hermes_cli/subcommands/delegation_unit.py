"""Hidden ``hermes -p <profile> delegation-unit`` subcommand: the kanban-runtime unit runner.

Spawned only by the kanban dispatcher for a card on the internal ``delegations`` board
(docs/dev/delegate-background-on-kanban-runtime.md §5.3). A CLI subcommand, not ``python -m``, so
the runner gets the same bootstrap as every ``hermes -p <profile> chat`` worker: the profile
override before any import, the profile ``.env`` and ``agent.process_env_files``.
"""
from __future__ import annotations


def _cmd(args) -> int:
    from tools.delegation_unit_worker import cmd_delegation_unit

    return cmd_delegation_unit(args)


def build_delegation_unit_parser(subparsers) -> None:
    parser = subparsers.add_parser("delegation-unit")
    parser.set_defaults(func=_cmd)
    # Hidden from ``hermes --help``: drop the listing entry add_parser made (dispatch still works).
    subparsers._choices_actions = [a for a in subparsers._choices_actions if a.dest != "delegation-unit"]
