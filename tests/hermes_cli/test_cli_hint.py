"""A printed CLI remedy must be accepted VERBATIM when pasted.

An error message that tells the operator what to type is only a remedy if the
printed string works. Interpolating a user-controlled token into a bare
``--flag <token>`` form breaks in two layers before the command is dispatched,
and the SHELL layer runs first:

* the SHELL re-lexes the printed text: whitespace splits it, ``;&|()`` are
  control operators or syntax errors, ``{a,b}`` brace-expands, ``*?[]`` glob
  against the operator's CWD, and ``$VAR`` / ``` `cmd` `` / ``$(cmd)`` / ``~``
  expand or EXECUTE -- so the printed remedy can run something else entirely;
* ARGPARSE then reads a token beginning with ``-`` as an option rather than as
  the value and refuses with ``expected one argument``.

Either way the state the message exists to escape ends up with no accepted
input at all -- the unreachable-remedy bug. Card t_c9e1a012 (Argus FINDING 3
on PR #842) found it in `kanban_survivor._qualified_hint`, where a repository
directory beginning with `-` printed `--survivor-pr -leading-dash=...`.

THE PASTE SIMULATOR IS A REAL SHELL, and that is load-bearing. Round 1 of this
card simulated the paste with ``shlex.split`` while the implementation ALSO
decided safety with ``shlex.split`` -- the same function on both sides, so the
assertion held by construction and five tokens a real bash mangles or executes
sat green in the suite. Here the printed string is handed to ``/bin/bash``, the
words bash actually produces are fed to the REAL parser, and the bound value
must equal the token that was printed.
"""
import argparse
import os
import shutil
import subprocess
import sys

import pytest

from hermes_cli.cli_hint import hint_arg, hint_value

BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"),
    reason="the paste oracle needs a real POSIX shell",
)

# Tokens a real repository directory name or filesystem path can hold. Each of
# these creates fine as a directory on macOS/Linux, so each is reachable.
HOSTILE = [
    # argparse layer: a value beginning with '-'
    "-leading-dash", "--double-dash", "-",
    # shell layer: word splitting
    "qa output", "two  spaces", "trailing ", "\ttab",
    # shell layer: expansion / substitution -- the printed remedy would EXECUTE
    "$HOME", "`id`", "$(id)", "a$b", "${x}",
    # shell layer: control operators
    "semi;colon", "amp&sand", "pipe|line", "paren(th)", "a&&b",
    # shell layer: brace expansion and globbing (the SILENT, CWD-dependent half)
    "brace{a,b}", "star*glob", "q?mark", "brack[et]",
    # shell layer: quoting
    "quo'te", 'dou"ble', "back\\slash",
    # shell layer: tilde expansion
    "~scratch", "~",
    # shell layer: COMBINED features -- THE discriminator. Every token above
    # carries exactly ONE hostile feature, and a quoter that merely SELECTS a
    # quote style by scanning for an apostrophe is byte-correct on all of them:
    # it emits '$HOME' for the expansion-only token and "quo'te" for the
    # apostrophe-only token, and bash returns both unchanged. Python's repr()
    # is exactly that quoter. It takes an apostrophe AND an expansion in the
    # SAME token to force the choice -- repr picks double quotes for the
    # apostrophe, and double quotes do not stop expansion, so `Ace's $(id)`
    # EXECUTES. Measured: repr is correct for 11/15 of this list and wrong for
    # precisely the combined four (card t_f5323218).
    "don't $HOME", "Ace's $(id)", "it's `id`", 'say "hi" $USER',
    "don't back\\slash",
]
PLAIN = ["plain", "with/slash", "repo@v2", "a+b", "under_score", "repo.name", "CAPS"]


def _bash_words(printed, cwd):
    """The argv a REAL bash produces for `printed`, or None if bash refuses.

    NUL-delimited so a word containing a newline survives the round trip.
    """
    proc = subprocess.run(
        [str(BASH), "-c", 'printf "%s\\0" ' + printed],
        capture_output=True, cwd=cwd,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    return out.split("\0")[:-1] if out else []


@pytest.fixture
def hostile_cwd(tmp_path):
    """A CWD holding names that MATCH the glob tokens above.

    Globbing is the silent half of the bug: bash leaves `star*glob` literal
    only when nothing matches. With `starXglob` on disk the same printed hint
    binds a different value and nothing errors. The oracle must run somewhere
    the glob can actually hit, or it cannot see that failure mode.
    """
    for name in ("starXglob", "qAmark", "bracke", "brace"):
        (tmp_path / name).mkdir()
    return str(tmp_path)


def _kanban_parser():
    from hermes_cli import kanban as kanban_cli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban_cli.build_parser(parser.add_subparsers(dest="command"))
    return parser


@requires_bash
@pytest.mark.parametrize("token", HOSTILE + PLAIN)
def test_hint_arg_survives_a_real_shell_then_argparse(token, hostile_cwd):
    """The helper's output round-trips through /bin/bash and the real parser."""
    parser = argparse.ArgumentParser(prog="prog", add_help=False)
    parser.add_argument("--thing")

    printed = hint_arg("--thing", token)
    words = _bash_words(printed, hostile_cwd)

    assert words is not None, f"bash refused the printed hint {printed!r}"
    args = parser.parse_args(words)
    assert args.thing == token, f"printed {printed!r} bound {args.thing!r}"


@requires_bash
@pytest.mark.parametrize("token", HOSTILE + PLAIN)
def test_hint_value_survives_a_real_shell_as_one_unchanged_word(token, hostile_cwd):
    """A bare positional (``cd <path>``) is re-lexed by the shell too."""
    printed = hint_value(token)
    words = _bash_words(printed, hostile_cwd)

    assert words == [token], f"printed {printed!r} produced {words!r}"


@requires_bash
@pytest.mark.parametrize(
    "title",
    ["$HOME", "`id`", "a b", "quo'te", "plain",
     # COMBINED: apostrophe AND expansion in one title. See the HOSTILE list's
     # note -- the single-feature titles above cannot tell shlex.quote from a
     # quoter that merely picks a quote style by looking for an apostrophe.
     "don't $HOME", "Ace's $(id)", "it's `id`"],
)
def test_the_resume_hint_does_not_expand_a_session_title(title, hostile_cwd,
                                                         monkeypatch, capsys):
    """SIBLING SITE (same class, different spelling): the session-resume hints
    printed `hermes -c "<title>"` with HAND-ROLLED double quotes.

    Double quotes stop word splitting but NOT parameter expansion or command
    substitution, so a session a user named ``$HOME`` or ``` `id` `` printed a
    remedy that expanded or EXECUTED when pasted. Drives the REAL print site
    (`_print_tui_exit_summary`) and then a real bash -- reconstructing the
    string here instead would be the round-1 tautology all over again.
    """
    import hermes_state

    from hermes_cli import main as cli_main

    class _FakeDB:
        def get_session(self, sid):
            return {"message_count": 3, "input_tokens": 1, "output_tokens": 1}

        def get_session_title(self, sid):
            return title

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB, raising=False)
    cli_main._print_tui_exit_summary("sess_1")

    printed = [ln.strip() for ln in capsys.readouterr().out.splitlines()
               if ln.strip().startswith("hermes --tui -c ")]
    assert printed, "the exit summary must print a title-resume hint"

    words = _bash_words(printed[0], hostile_cwd)
    assert words == ["hermes", "--tui", "-c", title], \
        f"printed {printed[0]!r} produced {words!r}"


@requires_bash
def test_the_live_checkout_block_message_names_a_clonable_path(tmp_path, hostile_cwd):
    """SIBLING SITE: `self_repo_guard` refuses a write to the live checkout and
    prints `git clone --shared <root> <scratch>/<task>` as the way out.

    `root` is the install path, so the refusal's remedy split for an install
    under a path with a space -- an unreachable remedy on a guard whose whole
    job is to route the operator somewhere safe.
    """
    from tools.self_repo_guard import _block_message

    root = tmp_path / "My Projects" / "hermes-agent"
    root.mkdir(parents=True)

    message = _block_message("git commit", root)
    clone = message.split("`git clone --shared ", 1)[1].split("`", 1)[0]
    # `<task>` is a fill-in-the-blank the operator replaces; substitute it the
    # way they would so the PATH is what is under test, not the blank.
    pasted = "git clone --shared " + clone.replace("<task>", "t_123")
    words = _bash_words(pasted, hostile_cwd)

    assert words is not None, f"bash refused {pasted!r}"
    assert words[3] == str(root), words


@pytest.mark.parametrize("token", PLAIN)
def test_the_readable_form_is_kept_when_no_escaping_is_needed(token):
    """Quoting everything would be safe but unreadable; plain tokens stay plain.

    This is the over-fix guard: a helper that always returned the quoted
    `'--flag=value'` form would pass every round-trip test above while making
    the common message worse.
    """
    assert hint_arg("--thing", token) == f"--thing {token}"


@requires_bash
@pytest.mark.parametrize("key", HOSTILE + PLAIN)
def test_qualified_hint_is_accepted_verbatim_by_the_real_kanban_parser(key, hostile_cwd):
    """CARD t_c9e1a012: the survivor refusal's remedy must parse as printed.

    Drives the real `hermes kanban complete` parser with the words a real bash
    produces, and re-splits the bound value with the module's own
    `_split_qualifier` -- so a hint that parses but binds a mangled key still
    fails.
    """
    from hermes_cli import kanban_survivor as survivor

    printed = survivor._qualified_hint([key])

    # A key the qualifier GRAMMAR cannot express (it holds ':', '?', '#' or
    # '=') has no command form at all; `_qualified_hint` correctly prints a
    # prose admission instead, which is not something to paste. Decide which
    # arm applies with the module's own predicate, not by the string's shape.
    if survivor._split_qualifier(f"{key}=x") != (key, "x"):
        assert "no qualified form exists" in printed, printed
        return

    words = _bash_words(printed, hostile_cwd)

    assert words is not None, f"bash refused the printed hint {printed!r}"
    args = _kanban_parser().parse_args(["kanban", "complete", "t_x", *words])
    assert args.survivor_pr, f"printed {printed!r} bound no value"
    assert survivor._split_qualifier(args.survivor_pr[0]) == (key, "owner/repo#N"), printed


@requires_bash
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


@requires_bash
def test_the_bare_form_of_an_expanding_key_really_is_dangerous(hostile_cwd):
    """The discriminator's premise, measured rather than assumed.

    Round 1 shipped a `shlex.split` guard that called `$HOME` and `` `id` ``
    safe-to-print-bare. This asserts what a REAL bash does with that bare
    spelling, so the round-trip tests above are known to be discriminating and
    not merely describing whatever the helper emits.
    """
    assert _bash_words("--thing $HOME", hostile_cwd) == ["--thing", os.environ["HOME"]]
    # backticks EXECUTE: the printed remedy would run `id`.
    executed = _bash_words("--thing `id`", hostile_cwd)
    assert executed is not None and executed[1].startswith("uid="), executed
    # a control operator makes bash run a second command, not pass a word.
    assert _bash_words("--thing paren(th)", hostile_cwd) is None


@requires_bash
@pytest.mark.parametrize("sha", ["abc1234", None])
def test_the_update_rollback_remedy_is_accepted_verbatim(tmp_path, sha):
    """SIBLING SITE (same class, worst blast radius): `hermes update`'s
    rollback-FAILED branch prints `cd <PROJECT_ROOT> && git reset --hard <sha>`.

    PROJECT_ROOT is a filesystem path, so an install under `/Users/x/My
    Projects/` printed a `cd` the shell split in two -- an unreachable remedy
    on the one path where the operator has least slack. Both arms (a captured
    pre-pull SHA and the reflog fallback) print the same `cd`.
    """
    from hermes_cli import update_cmd

    root = tmp_path / "My Projects" / "hermes-agent"
    root.mkdir(parents=True)

    printed = update_cmd._manual_rollback_remedy(root, sha)

    # The no-SHA arm prints a `<prev-sha>` placeholder the operator fills in;
    # substitute it the way they would, so what is under test is the
    # interpolated PATH rather than the deliberate blank.
    pasted = printed.replace("<prev-sha>", "abc1234")

    # Run the printed line for real and ask the shell where it LANDED. The
    # `git` half is expected to fail (bogus sha, not a repo); what is under
    # test is that the `cd` reached the directory the message named instead of
    # splitting on the space and cd-ing to `<tmp>/My`.
    proc = subprocess.run(
        [str(BASH), "-c", pasted + "; printf '%s' \"$PWD\""],
        capture_output=True, cwd=str(tmp_path),
    )
    landed = proc.stdout.decode("utf-8", errors="replace")

    assert landed == str(root), f"printed {printed!r} landed in {landed!r}"


@requires_bash
def test_the_session_repair_remedy_is_accepted_verbatim(tmp_path, monkeypatch, capsys):
    """SIBLING SITE (same class): `hermes sessions repair`'s failure path prints
    a `hermes sessions recover --source <path>` remedy. The path comes from
    `report["backup_path"]`, so a backup under a directory with a space (a
    normal macOS path) printed a command the shell split in two -- measured as
    `error: unrecognized arguments` from the real CLI, exit 2.

    Drives the real print site and a real bash, not the helper.
    """
    import hermes_state

    from hermes_cli import sessions_cmd

    backup = tmp_path / "My Drive (old)" / "state.db.bak"
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
        words = _bash_words(line.rstrip("\\").strip(), str(tmp_path))
        assert words is not None, f"bash refused the printed remedy {line!r}"
        assert words[:3] == ["hermes", "sessions", "recover"], line
        parser = argparse.ArgumentParser(prog="hermes sessions recover", add_help=False)
        parser.add_argument("--source", required=True)
        args, _rest = parser.parse_known_args(words[3:])
        assert args.source == str(backup), f"printed {line!r} bound {args.source!r}"
