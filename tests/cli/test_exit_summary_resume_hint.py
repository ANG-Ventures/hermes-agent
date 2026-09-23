"""Tests for the CLI exit summary's resume hint, including profile-flag support."""

import shutil
import subprocess
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from cli import HermesCLI

BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"),
    reason="the paste oracle needs a real POSIX shell",
)


def _bash_words(printed, cwd):
    """The argv a REAL bash produces for ``printed``, or None if bash refuses.

    NUL-delimited so a word containing a newline survives the round trip.
    Mirrors ``tests/hermes_cli/test_cli_hint.py::_bash_words`` — the oracle for
    the twin print site. A shell is the only honest paste simulator here:
    ``shlex.split`` is what the implementation quotes WITH, so using it on both
    sides asserts nothing.
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
    """A CWD holding names that MATCH the glob titles below.

    Globbing is the silent half: bash leaves ``star*glob`` literal only when
    nothing matches, so the oracle must run somewhere the glob can hit.
    """
    for name in ("starXglob", "qAmark"):
        (tmp_path / name).mkdir()
    return str(tmp_path)


def _make_cli(session_id="20260524_000001_abc123"):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.session_id = session_id
    # _print_exit_summary requires a populated conversation history (msg_count > 0)
    # to print the resume hint at all. One synthetic user turn is enough.
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = None
    cli_obj._session_db = None
    cli_obj.session_start = datetime.now()
    return cli_obj


class TestExitSummaryResumeHint:
    """The exit-line ``Resume this session with:`` hint must include the
    active profile (`-p <name>`) so session IDs round-trip across
    profile boundaries — sessions live under `~/.hermes-profiles/<profile>/`,
    so a hint copied without `-p` from a non-default profile won't find
    the session.
    """

    def test_resume_hint_no_profile_flag_on_default(self, capsys):
        cli_obj = _make_cli()
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary()
        out = capsys.readouterr().out
        # No `-p` for the default profile.
        assert "hermes --resume 20260524_000001_abc123" in out
        assert " -p " not in out

    def test_resume_hint_no_profile_flag_on_custom(self, capsys):
        cli_obj = _make_cli()
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="custom"):
            cli_obj._print_exit_summary()
        out = capsys.readouterr().out
        # "custom" is the standard HERMES_HOME indicator — no -p needed.
        assert "hermes --resume 20260524_000001_abc123" in out
        assert " -p " not in out

    def test_resume_hint_includes_profile_flag_for_named_profile(self, capsys):
        cli_obj = _make_cli()
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="dev"):
            cli_obj._print_exit_summary()
        out = capsys.readouterr().out
        assert "hermes --resume 20260524_000001_abc123 -p dev" in out

    @requires_bash
    @pytest.mark.parametrize(
        "title",
        [
            "My Cool Session",
            # quoting: THE DISCRIMINATOR. A hand-rolled f"'{t}'" is
            # byte-identical to shlex.quote for every title WITHOUT an
            # apostrophe, so a title that has one is the only fixture that can
            # tell correct quoting from naive quoting. Without it bash gets
            # `hermes -c 'don't'` and dies on `unexpected EOF`.
            "quo'te",
            'dou"ble',
            # expansion / substitution: the printed hint would EXECUTE
            "$HOME",
            "`id`",
            "$(id)",
            # word splitting
            "a b",
            # control operators
            "semi;colon",
            "pipe|line",
            # globbing (silent, CWD-dependent — see hostile_cwd)
            "star*glob",
            "q?mark",
            # tilde expansion
            "~",
        ],
    )
    def test_title_resume_hint_round_trips_through_a_real_shell(
        self, title, hostile_cwd, capsys
    ):
        """When a session title is available, the ``hermes -c <title>`` hint
        must survive a REAL shell unchanged and still carry ``-p <profile>``
        for non-default profiles.

        Asserts the ROUND TRIP (printed -> /bin/bash -> argv) rather than a
        literal spelling. A literal pins one quoting style and goes stale the
        moment the site's quoting changes (that is card t_0c5ac29a); worse,
        with the lone no-apostrophe fixture "My Cool Session" it could not
        discriminate ``shlex.quote(t)`` from a naive ``f"'{t}'"`` at all,
        because the two emit identical bytes for that string. The round trip
        cannot go stale and kills the naive spelling by construction.

        Mirrors the oracle already gating the twin print site,
        ``tests/hermes_cli/test_cli_hint.py::
        test_the_resume_hint_does_not_expand_a_session_title``.
        """
        cli_obj = _make_cli()
        fake_db = MagicMock()
        fake_db.get_session_title.return_value = title
        cli_obj._session_db = fake_db

        with patch("hermes_cli.profiles.get_active_profile_name", return_value="dev"):
            cli_obj._print_exit_summary()
        out = capsys.readouterr().out

        printed = [
            ln.strip() for ln in out.splitlines() if ln.strip().startswith("hermes -c ")
        ]
        assert printed, f"the exit summary must print a title-resume hint; got {out!r}"

        words = _bash_words(printed[0], hostile_cwd)
        assert words is not None, f"bash refused the printed hint {printed[0]!r}"
        assert words == ["hermes", "-c", title, "-p", "dev"], \
            f"printed {printed[0]!r} produced {words!r}"
        assert "hermes --resume 20260524_000001_abc123 -p dev" in out

    def test_resume_hint_falls_back_when_profile_lookup_fails(self, capsys):
        """If `get_active_profile_name` raises (e.g. profiles module
        missing during ``hermes update`` mid-flight), fall back to no
        flag rather than crashing the exit summary.
        """
        cli_obj = _make_cli()
        with patch(
            "hermes_cli.profiles.get_active_profile_name",
            side_effect=RuntimeError("profiles unavailable"),
        ):
            cli_obj._print_exit_summary()
        out = capsys.readouterr().out
        # Resume hint still printed without -p.
        assert "hermes --resume 20260524_000001_abc123" in out
        assert " -p " not in out
