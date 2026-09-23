"""The production-root deny-list must agree with the resolver that picks the store.

Card t_5bfcbf14. ``_real_platform_state_root()`` unconditionally appends
``.hermes`` to the account home, but ``hermes_constants.get_default_hermes_root``
— which is what ``kanban_home()`` actually resolves the board through — returns
``HERMES_HOME`` ITSELF when it points outside ``~/.hermes``.

The repo's own container image is exactly that shape (``Dockerfile``: ``useradd
-u 10000 -m -d /opt/data hermes`` + ``ENV HERMES_HOME=/opt/data``), so the live
board is ``/opt/data/kanban.db`` while the deny-list only ever named
``/opt/data/.hermes``. Measured in a real ``python:3.11-slim`` container: the
guard returned ALLOWED on both #853 and #865. Green on every developer Mac,
structurally unable to fire in the official image.

These tests pin BOTH directions, because only the pair discriminates:

* the deployment root MUST be production (or the guard is inert in Docker), and
* a redirected/hermetic ``HERMES_HOME`` must NOT be (or the t_c64b9d44 false
  positive returns and people disarm the guard globally, which is how the two
  previous mitigations died).

The discriminator is the OS ACCOUNT home, which a test cannot move: a real
deployment's ``HERMES_HOME`` is at or under it; a hermetic root is a tmpdir
elsewhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

import hermes_state

pwd = pytest.importorskip("pwd", reason="POSIX-only: the anchor reads passwd")


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


# --- the gap this card closes ----------------------------------------------


def test_container_shape_hermes_home_is_a_production_root(monkeypatch):
    """``HERMES_HOME=<account home>`` (the Dockerfile shape) IS production.

    In the image, the account home and ``HERMES_HOME`` are the same directory
    (``/opt/data``) and the board sits directly in it — so the root the
    deny-list names must be that directory, not ``<it>/.hermes``.
    """
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account))

    assert hermes_state._deployed_hermes_home_root() == account
    assert account in hermes_state._production_state_roots()


def test_container_shape_live_board_is_refused_from_a_test(monkeypatch):
    """The board the container's own resolver picks must be REFUSED.

    Read-only: ``_assert_live_board_write_allowed`` only resolves paths and
    reads env; it opens no connection and creates no file.
    """
    from hermes_cli import kanban_db as kb

    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    board = (account / "kanban.db").resolve()
    assert kb.kanban_home().resolve() == account  # the resolver picks it
    with pytest.raises(kb.LiveBoardWriteRefused):
        kb._assert_live_board_write_allowed(board)


def test_container_shape_state_db_is_refused_from_a_test(monkeypatch):
    """The sibling store moves with it — one root definition, two guards."""
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account))

    with pytest.raises(RuntimeError, match="live-system guard"):
        hermes_state._ensure_test_isolation(account / "state.db")


def test_container_profile_home_resolves_to_its_root(monkeypatch):
    """``HERMES_HOME=<root>/profiles/<name>`` denies ``<root>``.

    Matches ``get_default_hermes_root``, which walks a profile home up to its
    root; the Docker profile layout is ``/opt/data/profiles/<name>``.
    """
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account / "profiles" / "coder"))

    assert hermes_state._deployed_hermes_home_root() == account


# --- the false positive that must NOT come back ----------------------------


def test_a_hermetic_hermes_home_is_not_a_production_root(tmp_path, monkeypatch):
    """The isolation idiom must not manufacture a production root.

    766 test files redirect ``HOME``/``HERMES_HOME`` to a tmpdir. If widening
    the deny-list to honour ``HERMES_HOME`` classified those as production,
    this is the t_c64b9d44 regression (2 files / 24 tests red).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert hermes_state._deployed_hermes_home_root() is None
    assert tmp_path.resolve() not in hermes_state._production_state_roots()


def test_a_tmpdir_under_the_account_home_is_not_a_production_root(monkeypatch):
    """Containment is the WRONG discriminator; exact equality is the right one.

    A mutation run caught this: with ``--basetemp`` outside the account home
    the containment version passed every false-positive test, so the tests were
    green for a reason unrelated to the property. ``~/.hermes/hermes-agent/<wt>``
    worktrees and ``~/.hermes/kanban/workspaces/<task>`` scratch dirs sit under
    the account home and legitimately hold throwaway DBs; promoting each of
    them to "a production root" refuses all of them.
    """
    account = _account_home()
    for under in (
        account / ".hermes" / "hermes-agent" / "wt",
        account / ".hermes" / "kanban" / "workspaces" / "t_dead",
        account / "scratch-tmp",
    ):
        monkeypatch.setenv("HERMES_HOME", str(under))
        assert hermes_state._deployed_hermes_home_root() is None, under


def test_a_hermetic_board_is_still_allowed(tmp_path, monkeypatch):
    """End-to-end: the hermetic board must open."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    kb._assert_live_board_write_allowed((tmp_path / "kanban.db").resolve())
    hermes_state._ensure_test_isolation(tmp_path / "state.db")


def test_a_redirected_home_pointing_at_a_tmp_root_is_not_production(
    tmp_path, monkeypatch
):
    """Redirecting ``$HOME`` as well must not make the tmp root production.

    The pair (``HOME`` and ``HERMES_HOME`` both moved) is the full hermetic
    idiom; the account home is what refuses to move with it.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    assert hermes_state._deployed_hermes_home_root() is None
    roots = hermes_state._production_state_roots()
    assert (tmp_path / ".hermes").resolve() not in roots


def test_a_worktree_under_the_real_root_is_not_its_own_production_root(
    monkeypatch,
):
    """``HERMES_HOME`` under ``~/.hermes`` yields no EXTRA root.

    ``~/.hermes/hermes-agent/...`` worktrees and
    ``~/.hermes/kanban/workspaces/<task>/...`` scratch dirs are under the
    account home, so the containment test alone would promote each of them to
    "a production root" and refuse every throwaway DB inside them. The
    platform root already covers anything genuinely under ``~/.hermes``.
    """
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account / ".hermes" / "hermes-agent" / "wt"))

    assert hermes_state._deployed_hermes_home_root() is None


def test_the_standard_host_deployment_adds_no_second_root(monkeypatch):
    """``HERMES_HOME=~/.hermes`` must not double-list the platform root."""
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account / ".hermes"))

    roots = hermes_state._production_state_roots()
    assert roots.count((account / ".hermes").resolve()) == 1


# --- fail-safe, never blind -------------------------------------------------


def test_no_hermes_home_yields_no_extra_root(monkeypatch):
    """Unset ``HERMES_HOME`` leaves the previous behaviour untouched."""
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert hermes_state._deployed_hermes_home_root() is None


def test_an_unknowable_account_home_yields_no_extra_root(monkeypatch):
    """No passwd entry (Windows, stripped install, arbitrary container UID).

    Without the discriminator there is no safe way to tell a deployment root
    from a tmpdir, so the widened arm stands down rather than guessing — the
    platform-root arm still answers, exactly as it did before this card.
    """
    monkeypatch.setattr(hermes_state, "_os_account_home", lambda: None)
    monkeypatch.setenv("HERMES_HOME", "/opt/data")

    assert hermes_state._deployed_hermes_home_root() is None
    assert hermes_state._production_state_roots()  # never empty => never blind


def test_a_missing_account_home_does_not_fall_back_to_a_redirected_home(
    tmp_path, monkeypatch
):
    """Falling back to ``$HOME`` here would destroy the discriminator.

    ``$HOME`` is exactly what a hermetic test rewrites, so a fallback would
    accept any ``HERMES_HOME`` that equals the redirected ``$HOME`` and
    reintroduce the t_c64b9d44 false positive on every host where passwd is
    unreadable. This asserts the two apart — on a normal dev box the account
    home and ``$HOME`` agree, and a test that did not separate them would pass
    against the fallback too (a mutation run proved that).
    """
    monkeypatch.setattr(hermes_state, "_os_account_home", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert hermes_state._deployed_hermes_home_root() is None
    assert tmp_path.resolve() not in hermes_state._production_state_roots()


def test_the_real_account_root_is_still_production(monkeypatch):
    """t_c64b9d44's discriminator survives this change."""
    account = _account_home()
    monkeypatch.setenv("HERMES_HOME", str(account))

    assert (account / ".hermes").resolve() in hermes_state._production_state_roots()
