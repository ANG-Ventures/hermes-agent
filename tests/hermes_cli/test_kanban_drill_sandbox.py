"""Drills and tests cannot reach the live board through a nested HERMES_HOME (t_65791cd2).

2026-10-10, twice: a drill pointed ``HERMES_HOME`` at a scratch dir under
``~/.hermes/profiles/<p>/cache/scratch`` (the fleet's default ``TMPDIR``).
``get_default_hermes_root()`` maps every path under the native root to the
native root, so ``kanban_home()`` returned the LIVE root and 8 fixture cards
landed on the live default board, the second time with
``HERMES_KANBAN_SANDBOX=1`` set and every pin unset.

Each test fakes the live root as ``tmp_path/.hermes`` (the conftest already
points the platform default there) and pins the guard's passwd-anchored live
root to the same directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def live_root(tmp_path, monkeypatch) -> Path:
    import hermes_constants

    root = hermes_constants._get_platform_default_hermes_home()
    assert root == tmp_path / ".hermes", "conftest no longer isolates the platform default"
    root.mkdir()
    monkeypatch.setattr(kb, "_LIVE_KANBAN_ROOT_MEMO", (root.resolve(),))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    for name in kb._KANBAN_PATH_PIN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return root


def _scratch_under(root: Path) -> Path:
    scratch = root / "profiles" / "daedalus" / "cache" / "scratch" / "drill"
    scratch.mkdir(parents=True)
    return scratch


def test_suite_runs_every_test_sandboxed():
    import os

    assert kb.kanban_sandbox_enabled(), os.environ.get("HERMES_KANBAN_SANDBOX")


def test_sandbox_keeps_a_nested_home_off_the_live_board(live_root, monkeypatch):
    """The t_7a6f4611 shape: SANDBOX=1, pins unset, home in a profile's scratch."""
    scratch = _scratch_under(live_root)
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")

    assert kb.kanban_home() == scratch
    assert kb.kanban_db_path() == scratch / "kanban.db"
    kb.init_db()
    with kb.connect() as conn:
        for i in range(4):
            kb.create_task(conn, title=f"drill {i}", assignee="builder")
    assert not (live_root / "kanban.db").exists()
    with kb.connect() as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 4


def test_nested_non_profile_home_without_sandbox_refuses(live_root, monkeypatch):
    """The t_db9ca661 shape: no flag at all. Refuse, never resolve the live board."""
    monkeypatch.setenv("HERMES_HOME", str(_scratch_under(live_root)))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — proves the refusal with the sandbox off
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="neither the root nor a profile home"):
        kb.kanban_db_path()
    assert not (live_root / "kanban.db").exists()


def test_live_root_refused_under_pytest_when_sandbox_unset(live_root, monkeypatch):
    """Refusal is proven when the sandbox is unset: PYTEST_CURRENT_TEST alone refuses."""
    monkeypatch.setenv("HERMES_HOME", str(live_root))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — proves the refusal with the sandbox off
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="PYTEST_CURRENT_TEST"):
        kb.kanban_home()


@pytest.mark.parametrize("marker", ["HERMES_TEST_ISOLATION", "HERMES_KANBAN_DRILL"])
def test_isolation_markers_refuse_the_live_root_outside_pytest(live_root, monkeypatch, marker):
    """A child that stripped PYTEST_* still carries our marker; a drill names its card."""
    monkeypatch.setenv("HERMES_HOME", str(live_root / "profiles" / "daedalus"))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — proves the refusal with the sandbox off
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setenv(marker, "t_65791cd2")
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match=marker):
        kb.kanban_home()


def test_live_db_pin_refused_in_isolation(live_root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — proves the refusal with the sandbox off
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_root / "kanban.db"))
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="HERMES_KANBAN_DB"):
        kb.kanban_db_path()


def test_sandbox_with_home_at_the_live_root_refuses(live_root, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(live_root))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="no isolated board"):
        kb.kanban_home()


@pytest.mark.parametrize("home", ["", "root", "profiles/argus"])
def test_production_homes_still_share_the_root(live_root, monkeypatch, home):
    """REAL_HOME/profile-home design intact: root and profile homes share the root board."""
    for name in ("HERMES_KANBAN_SANDBOX", "PYTEST_CURRENT_TEST", "HERMES_TEST_ISOLATION",
                 "HERMES_KANBAN_DRILL"):
        monkeypatch.delenv(name, raising=False)
    if home:
        target = live_root if home == "root" else live_root / home
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(target))
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    assert kb.kanban_home() == live_root
    assert kb.kanban_db_path() == live_root / "kanban.db"


# --- Prism round 1 on #1873 ------------------------------------------------


def test_sandbox_refuses_a_home_that_is_a_live_named_board(live_root, monkeypatch):
    """HERMES_HOME=<live>/kanban/boards/proj would make the live proj DB the sandbox board."""
    board = live_root / "kanban" / "boards" / "proj"
    board.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(board))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="live board state"):
        kb.kanban_db_path()


def test_pin_inside_one_task_workspace_is_not_the_live_board(live_root, monkeypatch):
    """A scratch DB inside <live>/kanban/workspaces/<task>/ stays usable in isolation."""
    scratch = live_root / "kanban" / "workspaces" / "t_2f909ab6" / "kanban.db"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — exercises the pin path itself
    monkeypatch.setenv("HERMES_KANBAN_DB", str(scratch))
    assert kb.kanban_db_path() == scratch
    for live_pin in (live_root / "kanban" / "boards" / "proj" / "kanban.db", live_root / "kanban" / "workspaces"):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(live_pin))
        with pytest.raises(kb.KanbanLiveBoardRefusedError):
            kb.kanban_db_path()


def test_deployed_root_is_a_live_root_for_isolation(tmp_path, monkeypatch):
    """Container shape: HERMES_HOME == account home (/opt/data); a drill must refuse it."""
    import hermes_state

    deployed = tmp_path / "opt-data"
    deployed.mkdir()
    monkeypatch.setattr(kb, "_LIVE_KANBAN_ROOT_MEMO", ((tmp_path / "native" / ".hermes").resolve(),))
    monkeypatch.setattr(hermes_state, "_deployed_hermes_home_root", lambda: deployed.resolve())
    monkeypatch.setenv("HERMES_HOME", str(deployed))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="no isolated board"):
        kb.kanban_home()


def test_explicit_db_path_is_refused_for_a_drill(tmp_path, monkeypatch):
    """kb.connect(db_path=<live>/kanban.db) bypasses kanban_db_path(); the write guard refuses."""
    import hermes_state
    import hermes_test_context

    live = (tmp_path / "prodhome" / ".hermes")
    live.mkdir(parents=True)
    monkeypatch.setattr(hermes_state, "_STATE_DB_GUARD_EXTRA_DENY_ROOTS", (live.resolve(),))
    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DRILL", "t_65791cd2")
    with pytest.raises(kb.LiveBoardWriteRefused, match="HERMES_KANBAN_DRILL"):
        kb.connect(db_path=live / "kanban.db")
    assert not (live / "kanban.db").exists()


def test_container_board_stays_live_after_the_drill_redirects_its_home(tmp_path, monkeypatch):
    """Prism round 2: /opt/data with HERMES_HOME=/tmp/drill must still be live (pin + write)."""
    import hermes_state
    import hermes_test_context

    account = tmp_path / "opt-data"
    account.mkdir()
    (account / "kanban.db").touch()
    monkeypatch.setattr(hermes_state, "_os_account_home", lambda: account)
    monkeypatch.setattr(kb, "_LIVE_KANBAN_ROOT_MEMO", ((account / ".hermes").resolve(),))
    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "drill"))
    monkeypatch.setenv("HERMES_KANBAN_DRILL", "t_65791cd2")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — the drill marker alone must refuse
    assert hermes_state._deployed_hermes_home_root() is None  # the redirect hid it before
    monkeypatch.setenv("HERMES_KANBAN_DB", str(account / "kanban.db"))
    with pytest.raises(kb.KanbanLiveBoardRefusedError, match="HERMES_KANBAN_DB"):
        kb.kanban_db_path()
    monkeypatch.delenv("HERMES_KANBAN_DB")
    with pytest.raises(kb.LiveBoardWriteRefused, match="HERMES_KANBAN_DRILL"):
        kb.connect(db_path=account / "kanban.db")
    assert (account / "kanban.db").stat().st_size == 0
