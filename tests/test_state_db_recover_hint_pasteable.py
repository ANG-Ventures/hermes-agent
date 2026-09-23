"""The `sqlite3 <db> ".recover"` remedy must survive the shell when pasted.

`hermes_state` prints a salvage command at three sites on the DB-corruption
recovery path — the exhausted-repair-budget diagnostic and both
``_backup_db_file`` disk-space refusals. Each interpolated the DB path bare
into a backticked span the operator is told to paste, so a ``HERMES_HOME``
holding a space (``My Drive``, or the Windows ``C:/Users/<First Last>``
default) printed a remedy that split into two words:

    printed  : sqlite3 /Users/x/My Drive/hermes/state.db ".recover"
    bash argv: ['sqlite3', '/Users/x/My', 'Drive/hermes/state.db', '.recover']
    sqlite3  : exit=1  Error: near "Drive": syntax error

Unreachable remedy on the one path where the operator has least slack. Found
by Argus as FINDING F3 in the round-2 review of PR #889 (card t_b649d6d1);
``hermes_cli.cli_hint.hint_value`` is the choke point that already owns this.

THE PASTE SIMULATOR IS A REAL SHELL, and the print path is the REAL one. The
string is not reconstructed here: ``sessions_cmd.cmd_sessions`` is driven with
a real damaged DB under a real HERMES_HOME, its stdout is captured, the
backticked span is extracted from that captured text, and the words come from
``/bin/bash``. Asserting on the string's shape — or simulating the paste with
``shlex`` while the implementation decides safety with ``shlex`` — is the
tautology round 1 of the parent card was blocked for.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil as _shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_state
from hermes_cli import sessions_cmd

BASH = _shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"),
    reason="the paste oracle needs a real POSIX shell",
)


def _bash_words(printed: str, cwd: str):
    """The argv a REAL bash produces for `printed`, or None if bash refuses.

    NUL-delimited so a word containing whitespace survives the round trip.
    """
    proc = subprocess.run(
        [str(BASH), "-c", 'printf "%s\\0" ' + printed],
        capture_output=True,
        cwd=cwd,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    return out.split("\0")[:-1] if out else []


def _spaced_home(tmp_path: Path) -> Path:
    """A HERMES_HOME holding a space, as Google Drive and Windows both give."""
    home = tmp_path / "My Drive" / "hermes"
    home.mkdir(parents=True)
    return home


def _damaged_db(home: Path) -> Path:
    """A file that really does fail to open as a database."""
    db = home / "state.db"
    db.write_bytes(b"not a database")
    assert hermes_state._db_opens_cleanly(db) is not None
    return db


def _run_repair(db: Path, monkeypatch) -> str:
    """Captured stdout of the REAL `hermes sessions repair` command path."""
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db, raising=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sessions_cmd.cmd_sessions(
            argparse.Namespace(sessions_action="repair", no_backup=False)
        )
    return buf.getvalue()


def _printed_sqlite3_remedy(out: str) -> str:
    """The backticked salvage command as the operator sees it on screen."""
    spans = [s for s in re.findall(r"`([^`]+)`", out) if "sqlite3" in s]
    assert spans, f"no sqlite3 remedy reached the screen:\n{out}"
    return spans[0]


def _exhaust_the_repair_budget(db: Path) -> None:
    """Write the REAL ledger the real exhaustion probe reads."""
    hermes_state._repair_ledger_path(db).write_text(
        json.dumps(
            {
                "fingerprint": hermes_state._db_fingerprint(db),
                "failed_attempts": hermes_state._MAX_PERSISTENT_REPAIR_ATTEMPTS,
            }
        ),
        encoding="utf-8",
    )
    assert hermes_state._persistent_repair_attempts_exhausted(db)


@requires_bash
def test_exhausted_repair_remedy_pastes_as_one_path(tmp_path, monkeypatch):
    """Site 1: `_persistent_repair_exhausted_error`, the terminal diagnostic."""
    db = _damaged_db(_spaced_home(tmp_path))
    _exhaust_the_repair_budget(db)

    remedy = _printed_sqlite3_remedy(_run_repair(db, monkeypatch))
    words = _bash_words(remedy, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {remedy!r}"
    assert words == ["sqlite3", str(db), ".recover"], (
        f"printed {remedy!r} produced {words!r}"
    )


@requires_bash
def test_low_disk_backup_refusal_remedy_pastes_as_one_path(tmp_path, monkeypatch):
    """Site 2: `_backup_db_file`'s nearly-full-volume refusal.

    Reaches the screen because a refused pre-repair backup is a HARD STOP
    whose reason is folded into ``report['error']``, which `cmd_sessions`
    prints verbatim.
    """
    db = _damaged_db(_spaced_home(tmp_path))
    tight = type(
        "Usage",
        (),
        {
            "total": 10_000_000_000,
            "used": 0,
            "free": hermes_state._REPAIR_BACKUP_MIN_FREE_BYTES // 2,
        },
    )()

    with patch("shutil.disk_usage", return_value=tight):
        out = _run_repair(db, monkeypatch)

    remedy = _printed_sqlite3_remedy(out)
    words = _bash_words(remedy, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {remedy!r}"
    assert words == ["sqlite3", str(db), ".recover"], (
        f"printed {remedy!r} produced {words!r}"
    )


@requires_bash
def test_unknown_disk_space_refusal_remedy_pastes_as_one_path(tmp_path, monkeypatch):
    """Site 3: `_backup_db_file`'s fail-closed disk-space-unknown branch."""
    db = _damaged_db(_spaced_home(tmp_path))

    with patch("shutil.disk_usage", side_effect=OSError("cannot statvfs")):
        out = _run_repair(db, monkeypatch)

    remedy = _printed_sqlite3_remedy(out)
    words = _bash_words(remedy, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {remedy!r}"
    assert words == ["sqlite3", str(db), ".recover"], (
        f"printed {remedy!r} produced {words!r}"
    )


def test_the_readable_form_is_kept_for_an_ordinary_path(tmp_path):
    """Over-fix guard: a path needing no escaping must not grow quotes.

    Quoting unconditionally would pass every paste assertion above while
    making the message worse for the overwhelmingly common install path.
    """
    db = tmp_path / "hermes" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database")

    message = hermes_state._persistent_repair_exhausted_error(db)
    assert f'`sqlite3 {db} ".recover"`' in message, message


@requires_bash
def test_a_home_that_would_EXECUTE_is_neutralised(tmp_path, monkeypatch):
    """Word splitting is the reported symptom; substitution is the worse half.

    ``HERMES_HOME`` is an arbitrary operator-set env var, so a directory named
    ``$(...)`` prints a remedy that RUNS something. The same quoting closes
    both; this pins that it does.

    (A literal backtick in the path is deliberately NOT exercised: it would
    terminate the backticked span the message uses to delimit the command, so
    no quoting decision inside the span can recover it — a message-format
    limit, not something ``hint_value`` can or should fix.)
    """
    home = tmp_path / "$(id) and a space" / "hermes"
    home.mkdir(parents=True)
    db = _damaged_db(home)
    _exhaust_the_repair_budget(db)

    remedy = _printed_sqlite3_remedy(_run_repair(db, monkeypatch))
    words = _bash_words(remedy, str(tmp_path))

    assert words == ["sqlite3", str(db), ".recover"], (
        f"printed {remedy!r} produced {words!r}"
    )


def test_every_sqlite3_recover_remedy_in_hermes_state_is_escaped():
    """Class guard: no site may re-introduce a bare interpolation.

    The three call sites are one class, and a fourth is cheap to add. Pins the
    source shape so a new `sqlite3 {db_path} ".recover"` cannot land silently
    without its own paste test.
    """
    source = Path(hermes_state.__file__).read_text(encoding="utf-8")
    bare = re.findall(r"sqlite3 \{(?!hint_value\()[^}]*\}", source)
    assert not bare, f"unescaped sqlite3 remedy interpolation(s): {bare}"
    assert source.count('sqlite3 {hint_value(str(db_path))}') == 3, (
        "expected exactly the three known sqlite3 salvage sites"
    )


def test_os_environ_hermes_home_reaches_the_db_path_verbatim(tmp_path, monkeypatch):
    """Reachability, not argued: the real resolver keeps the space.

    Without this the fix could be dismissed as guarding an impossible path.
    """
    from hermes_constants import get_hermes_home

    home = _spaced_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert " " in str(get_hermes_home())
    assert str(get_hermes_home()) == str(home)
    assert os.environ["HERMES_HOME"] == str(home)
