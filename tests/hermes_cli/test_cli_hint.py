"""A printed CLI remedy must be accepted VERBATIM when pasted.

An error message that tells the operator what to type is only a remedy if the
printed string works. Interpolating a user-controlled token into a bare
``--flag <token>`` form breaks in two layers before the command is dispatched:

* the SHELL splits a token containing whitespace into two words, so the
  trailing half arrives as a stray positional;
* ARGPARSE reads a token beginning with ``-`` as an option rather than as the
  value and refuses with ``expected one argument``.

Either way the state the message exists to escape ends up with no accepted
input at all -- the unreachable-remedy bug. Card t_c9e1a012 (Argus FINDING 3
on PR #842) found it in `kanban_survivor._qualified_hint`, where a repository
directory beginning with `-` printed `--survivor-pr -leading-dash=...`.

Every test here asserts by ROUND-TRIP: take the string the tool prints, split
it the way a shell would (`shlex.split`), feed the words to the REAL parser,
and require the value argparse binds to equal the token that was printed.
Asserting on the string's shape instead is what let the original bug ship --
the old regression test pinned `--survivor-pr -lead=owner/repo#N` as correct.
"""
import argparse
import shlex

import pytest

from hermes_cli.cli_hint import hint_arg

# Tokens a real repository directory name or filesystem path can hold. The
# leading-dash and whitespace entries are the two failure shapes; the rest are
# the regression half -- they must keep working and keep their readable
# unquoted spelling.
HOSTILE = ["-leading-dash", "--double-dash", "-", "qa output", "two  spaces", "trailing "]
PLAIN = ["plain", "with/slash", "repo@v2", "a+b", "~scratch", "ünïcode", "under_score"]


def _paste(printed):
    """Split `printed` the way a shell would when the operator pastes it."""
    return shlex.split(printed)


def _kanban_parser():
    from hermes_cli import kanban as kanban_cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban_cli.build_parser(parser.add_subparsers(dest="command"))
    return parser


@pytest.mark.parametrize("token", HOSTILE + PLAIN)
def test_hint_arg_round_trips_through_shell_and_argparse(token):
    """The helper's output survives both layers for every token shape."""
    parser = argparse.ArgumentParser(prog="prog", add_help=False)
    parser.add_argument("--thing")

    printed = hint_arg("--thing", token)
    args = parser.parse_args(_paste(printed))

    assert args.thing == token, f"printed {printed!r} bound {args.thing!r}"


@pytest.mark.parametrize("token", PLAIN)
def test_hint_arg_keeps_the_readable_form_when_no_escaping_is_needed(token):
    """Quoting everything would be safe but unreadable; plain tokens stay plain.

    This is the over-fix guard: a helper that always returned the quoted
    `'--flag=value'` form would pass every round-trip test above while making
    the common message worse.
    """
    assert hint_arg("--thing", token) == f"--thing {token}"


@pytest.mark.parametrize("key", HOSTILE + PLAIN)
def test_qualified_hint_is_accepted_verbatim_by_the_real_kanban_parser(key):
    """CARD t_c9e1a012: the survivor refusal's remedy must parse as printed.

    Drives the real `hermes kanban complete` parser, and re-splits the bound
    value with the module's own `_split_qualifier` -- so a hint that parses but
    binds a mangled key still fails.
    """
    from hermes_cli import kanban_survivor as survivor

    printed = survivor._qualified_hint([key])
    args = _kanban_parser().parse_args(["kanban", "complete", "t_x", *_paste(printed)])

    assert args.survivor_pr, f"printed {printed!r} bound no value"
    assert survivor._split_qualifier(args.survivor_pr[0]) == (key, "owner/repo#N"), printed


def test_a_leading_dash_repository_key_is_rejected_before_the_fix():
    """Pin the exact defect shape, so the fix cannot silently regress to it.

    The bare `--survivor-pr -leading-dash=...` spelling is what the module
    printed before card t_c9e1a012; feeding it to the real parser must still
    fail, which is what makes the test above a discriminating oracle rather
    than a tautology about whatever the helper happens to emit.
    """
    with pytest.raises(SystemExit):
        _kanban_parser().parse_args(
            ["kanban", "complete", "t_x", "--survivor-pr", "-leading-dash=owner/repo#N"]
        )


def test_the_session_repair_remedy_is_accepted_verbatim(tmp_path, monkeypatch, capsys):
    """SIBLING SITE (same class): `hermes sessions repair`'s failure path prints
    a `hermes sessions recover --source <path>` remedy. The path comes from
    `report["backup_path"]`, so a backup under a directory with a space (a
    normal macOS path) printed a command the shell split in two -- measured as
    `error: unrecognized arguments` from the real CLI, exit 2.

    Drives the real print site and the real parser, not the helper.
    """
    import hermes_state

    from hermes_cli import sessions_cmd

    backup = tmp_path / "My Drive" / "state.db.bak"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"")
    db = tmp_path / "state.db"
    db.write_bytes(b"")

    # `cmd_sessions` imports these from `hermes_state` at call time.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db, raising=False)
    monkeypatch.setattr(hermes_state, "_db_opens_cleanly",
                        lambda p: "database disk image is malformed", raising=False)
    monkeypatch.setattr(
        hermes_state, "repair_state_db_schema",
        lambda *a, **k: {"repaired": False, "error": "disk image is malformed",
                         "backup_path": str(backup)},
        raising=False,
    )

    sessions_cmd.cmd_sessions(argparse.Namespace(sessions_action="repair", no_backup=False))

    printed = [ln.strip() for ln in capsys.readouterr().out.splitlines()
               if "sessions recover" in ln]
    assert printed, "the repair failure must print a recover remedy"

    for line in printed:
        # Strip the trailing line-continuation backslash the message prints.
        words = _paste(line.rstrip("\\").strip())
        assert words[:3] == ["hermes", "sessions", "recover"], line
        parser = argparse.ArgumentParser(prog="hermes sessions recover", add_help=False)
        parser.add_argument("--source", required=True)
        args, _rest = parser.parse_known_args(words[3:])
        assert args.source == str(backup), f"printed {line!r} bound {args.source!r}"
