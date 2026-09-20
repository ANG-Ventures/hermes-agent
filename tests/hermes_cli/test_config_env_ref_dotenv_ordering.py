"""``${env:VAR}`` refs must not warn before ``.env`` has been loaded.

Several entrypoints import ``hermes_cli.config`` — which expands config
refs as an import side effect (``_inject_profile_env_vars`` →
``providers.list_providers`` → ``plugins._get_enabled_plugins`` →
``load_config``) — strictly BEFORE they call ``load_hermes_dotenv()``.
A variable that lives only in ``~/.hermes/.env`` therefore looked unset at
that moment and printed

    Config ref '${env:X}': X is not set (check ~/.hermes/.env); keeping the
    literal placeholder

on every ``hermes <subcommand>`` invocation, even though the resolved value
was always correct (``load_config()``'s env-ref snapshot re-expands once the
environment changes — #58514).

These tests pin both halves of the contract: silence for a dotenv-only ref,
and a real warning for a genuinely-missing one.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import env_loader
from hermes_cli.config import _expand_env_vars

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_dotenv_flag(monkeypatch):
    """Each test owns the process-global "have we loaded .env yet" flag."""
    monkeypatch.setattr(env_loader, "_DOTENV_LOADED", False, raising=False)


# ---------------------------------------------------------------------------
# dotenv_pending() — the predicate the warning is gated on
# ---------------------------------------------------------------------------


def test_dotenv_pending_true_when_env_file_exists_and_unloaded(
    monkeypatch, tmp_path
):
    (tmp_path / ".env").write_text("FOO=bar\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert env_loader.dotenv_pending() is True


def test_dotenv_pending_false_when_no_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert env_loader.dotenv_pending() is False


def test_dotenv_pending_false_once_loaded(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("FOO=bar\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(env_loader, "_DOTENV_LOADED", True, raising=False)
    assert env_loader.dotenv_pending() is False


def test_load_hermes_dotenv_sets_the_loaded_flag(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ORDERING_PROBE=1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert env_loader.dotenv_pending() is True
    env_loader.load_hermes_dotenv(
        hermes_home=tmp_path, load_external_secrets=False
    )
    assert env_loader.dotenv_loaded() is True
    assert env_loader.dotenv_pending() is False


# ---------------------------------------------------------------------------
# The expander's warning behavior
# ---------------------------------------------------------------------------


def test_unresolved_ref_is_silent_while_dotenv_pending(
    monkeypatch, tmp_path, caplog
):
    """A pending .env suppresses the warning but NOT the placeholder."""
    (tmp_path / ".env").write_text("ONLY_IN_DOTENV=x\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("ONLY_IN_DOTENV", raising=False)

    with caplog.at_level("WARNING"):
        out = _expand_env_vars("${env:ONLY_IN_DOTENV}")

    assert out == "${env:ONLY_IN_DOTENV}"
    assert "keeping the literal placeholder" not in caplog.text


def test_unresolved_ref_still_warns_once_dotenv_is_loaded(
    monkeypatch, tmp_path, caplog
):
    """The real signal survives: after .env load, a missing var warns."""
    (tmp_path / ".env").write_text("SOMETHING_ELSE=x\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(env_loader, "_DOTENV_LOADED", True, raising=False)
    monkeypatch.delenv("TRULY_MISSING_REF", raising=False)

    with caplog.at_level("WARNING"):
        out = _expand_env_vars("${env:TRULY_MISSING_REF}")

    assert out == "${env:TRULY_MISSING_REF}"
    assert "TRULY_MISSING_REF is not set" in caplog.text


def test_unresolved_ref_warns_when_no_dotenv_exists_at_all(
    monkeypatch, tmp_path, caplog
):
    """No .env on disk → nothing is pending → warn as before."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("NO_DOTENV_REF", raising=False)

    with caplog.at_level("WARNING"):
        _expand_env_vars("${env:NO_DOTENV_REF}")

    assert "NO_DOTENV_REF is not set" in caplog.text


# ---------------------------------------------------------------------------
# End-to-end: a FRESH process on the real CLI import ordering
# ---------------------------------------------------------------------------


_E2E_PROGRAM = textwrap.dedent(
    """
    # Exactly the CLI's ordering: hermes_cli.config is imported (and expands
    # config refs as an import side effect) BEFORE load_hermes_dotenv() runs.
    import hermes_cli.config as _cfg
    from hermes_cli.env_loader import load_hermes_dotenv

    load_hermes_dotenv(load_external_secrets=False)

    from hermes_cli.config import load_config

    c = load_config()
    w = c["gateway"]["webhook"]
    print("SECRET=" + w["secret"])
    print("OTHER=" + w["other"])
    """
)


def _run_fresh_cli_process(home: Path) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", _E2E_PROGRAM],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_fresh_process_resolves_dotenv_only_ref_without_warning(tmp_path):
    """The reported bug, end to end, in a real subprocess.

    ``secret`` reads a var that exists ONLY in ``.env``; ``other`` reads a
    var that exists nowhere. The first must resolve silently, the second
    must still warn — one run proves both directions.
    """
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        textwrap.dedent(
            """\
            model:
              default: claude-opus-4-6
            gateway:
              webhook:
                secret: '${env:ORDERING_DOTENV_ONLY}'
                other: '${env:ORDERING_NOWHERE}'
            """
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "ORDERING_DOTENV_ONLY=resolved-from-dotenv\n", encoding="utf-8"
    )

    proc = _run_fresh_cli_process(home)
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, combined
    assert "SECRET=resolved-from-dotenv" in proc.stdout, combined
    assert "OTHER=${env:ORDERING_NOWHERE}" in proc.stdout, combined
    # The dotenv-only var must never be reported as unset...
    assert "ORDERING_DOTENV_ONLY is not set" not in combined, combined
    # ...while the genuinely-missing one still is.
    assert "ORDERING_NOWHERE is not set" in combined, combined
