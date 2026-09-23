"""The production-root anchor must be ``$HOME``-redirect proof.

Card t_c64b9d44. The live-system guards (``hermes_state._ensure_test_isolation``
for ``state.db``, ``kanban_db._assert_live_board_write_allowed`` for the board)
both decide "is this production?" from
``hermes_state._real_platform_state_root()``. That function used to read
``os.path.expanduser("~")``, which on POSIX is just ``$HOME`` — and
``monkeypatch.setenv("HOME", tmp_path)`` is THE hermetic-isolation idiom in this
repo (51 test files use it).

So under that idiom the test's OWN tmpdir became "the production root", a
hermetic board at ``<tmp>/.hermes/kanban.db`` WAS the live board, and the guard
refused it: 2 files / 24 tests red (``tests/gateway/test_kanban_dispatcher_standby.py``
21 errors, ``tests/tools/test_execute_code_session_provenance.py`` 3 failed).

The anchor is now the OS ACCOUNT home from the passwd database, which is a
property of the UID and does not move when a process rewrites ``$HOME``.

These tests pin BOTH directions, because only the pair is a discriminator:
the redirected tmpdir must NOT be production, and the real account root must
STILL be production (including while ``$HOME`` is redirected). A test that
only pinned the first would pass just as well if the guard went blind
entirely — the failure mode that matters far more than the false positive.
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
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# --- the false positive this card fixed ------------------------------------


def test_redirected_home_is_not_a_production_root(tmp_path, monkeypatch):
    """The hermetic idiom must not manufacture a production root.

    This is the exact fixture shape that went red: redirect ``$HOME`` to a
    tmpdir and put a board under ``<tmp>/.hermes``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    redirected = (tmp_path / ".hermes").resolve()
    assert hermes_state._real_platform_state_root() != redirected
    assert redirected not in hermes_state._production_state_roots()


def test_hermetic_board_under_a_redirected_home_is_not_the_live_board(
    tmp_path, monkeypatch
):
    """End-to-end shape of the CI red, at the classification boundary."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes"
    home.mkdir()
    board = (home / "kanban.db").resolve()

    assert not any(
        kb._is_production_board_db(board, root)
        for root in kb._production_kanban_roots()
    )
    # And the guard itself must not refuse it (it raises on a live board).
    kb._assert_live_board_write_allowed(board)


def test_state_db_guard_allows_a_hermetic_db_under_a_redirected_home(
    tmp_path, monkeypatch
):
    """The sibling guard on ``state.db`` shares the anchor, so it shares the fix."""
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes"
    home.mkdir()

    hermes_state._ensure_test_isolation(home / "state.db")


# --- the regression that would be far worse --------------------------------


def test_the_real_account_root_is_still_production():
    """The anchor must still name the operator's actual root.

    If this stopped holding, every guard in the class would silently go blind
    and the 2026-09-21 leak (3 fixture cards on the live board, 3 burned
    worker runs) would be reachable again.
    """
    expected = (_account_home() / ".hermes").resolve()

    assert hermes_state._real_platform_state_root() == expected
    assert expected in hermes_state._production_state_roots()


def test_the_real_account_root_survives_a_redirected_home(tmp_path, monkeypatch):
    """Redirecting ``$HOME`` must not DE-classify the real root either.

    This is the discriminator stated as one assertion: moving ``$HOME`` changes
    nothing about which root is production.
    """
    expected = (_account_home() / ".hermes").resolve()
    monkeypatch.setenv("HOME", str(tmp_path))

    assert hermes_state._real_platform_state_root() == expected


def test_live_board_under_the_real_root_is_still_refused_from_a_test(
    tmp_path, monkeypatch
):
    """The guard still fires on the REAL board, even with ``$HOME`` redirected.

    Read-only: ``_assert_live_board_write_allowed`` resolves paths and reads
    env. It opens no connection and writes nothing, so the live board is never
    touched by this test.
    """
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    real_board = (_account_home() / ".hermes" / "kanban.db").resolve()

    with pytest.raises(kb.LiveBoardWriteRefused):
        kb._assert_live_board_write_allowed(real_board)


def test_anchor_falls_back_when_the_account_home_is_unusable(monkeypatch):
    """An unusable passwd entry must fall back, never yield a bogus root.

    Service accounts / container images can carry a non-existent ``pw_dir``.
    Anchoring on it would point the deny-list at a root nothing resolves to —
    disarming the guard silently, which is the failure mode to avoid.
    """
    monkeypatch.setattr(hermes_state, "_os_account_home", lambda: None)
    monkeypatch.setenv("HOME", "/tmp/t_c64b9d44-fallback-home")

    assert hermes_state._real_platform_state_root() == Path(
        "/tmp/t_c64b9d44-fallback-home/.hermes"
    ).resolve()


def test_os_account_home_ignores_a_nonexistent_passwd_entry(monkeypatch):
    """``_os_account_home`` returns None rather than a path that isn't there.

    ``pwd`` is imported inside the function, so patch the stdlib module itself
    — patching an attribute on ``hermes_state`` would not be seen.
    """
    monkeypatch.setattr(
        pwd,
        "getpwuid",
        lambda _uid: type("Entry", (), {"pw_dir": "/nonexistent-t_c64b9d44"})(),
    )

    assert hermes_state._os_account_home() is None
