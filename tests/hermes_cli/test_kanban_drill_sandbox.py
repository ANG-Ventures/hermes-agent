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
