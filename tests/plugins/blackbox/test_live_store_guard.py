"""A test-context process must never open a LIVE Blackbox ``turns.db``.

Card t_46c8caeb (D9). On 2026-09-27 01:27 three ``turn_api_calls`` rows and
three ``prefix_sessions`` rows whose provider/model/turn_id were
``<MagicMock ...>`` reprs landed in the live
``~/.hermes/profiles/daedalus/blackbox/turns.db``: a test harness resolved the
worker's real ``HERMES_HOME``. ``store._connect()`` now refuses that open,
reusing the state.db / kanban.db production-root list and test-context
predicate.

The "live" root is always a FAKE root injected into
``hermes_state._STATE_DB_GUARD_EXTRA_DENY_ROOTS``; nothing here touches the
machine's real ``~/.hermes``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_state
import hermes_test_context
from agent.usage_pricing import CanonicalUsage
from plugins.blackbox import store


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    root = (tmp_path / "prodhome" / ".hermes").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(hermes_state, "_STATE_DB_GUARD_EXTRA_DENY_ROOTS", (root,))
    monkeypatch.setattr(hermes_state, "_STATE_DB_GUARD_BYPASS", False)
    monkeypatch.delenv(hermes_state._STATE_DB_GUARD_BYPASS_ENV, raising=False)
    return root


def _write_mock_call() -> None:
    """The incident shape: a MagicMock-identity zero-token call."""
    store.insert_api_call(
        "<MagicMock name='mock._current_turn_id'>", 0, ts=1.0,
        provider="<MagicMock name='mock.provider'>",
        model="<MagicMock name='mock.model'>",
        usage=CanonicalUsage(), sub_key=None, attribution="wire",
    )


@pytest.mark.parametrize("home_rel", ["", "profiles/daedalus"])
def test_test_context_write_to_live_store_is_refused(live_root, monkeypatch, home_rel):
    home = live_root / home_rel if home_rel else live_root
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert store._db_path() == home / "blackbox" / "turns.db"

    with pytest.raises(store.LiveBlackboxStoreRefused, match="LIVE Blackbox store"):
        _write_mock_call()

    # Refused BEFORE mkdir/connect: not a byte on the live side.
    assert not (home / "blackbox").exists()


def test_hermetic_home_under_nothing_live_still_writes(tmp_path, monkeypatch, live_root):
    home = tmp_path / "sandbox-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_mock_call()
    assert (home / "blackbox" / "turns.db").exists()


def test_scratch_dirs_under_live_root_are_not_live(live_root, monkeypatch):
    """Worktrees / kanban workspaces under the root are scratch, not stores."""
    home = live_root / "kanban" / "workspaces" / "t_x" / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_mock_call()
    assert (home / "blackbox" / "turns.db").exists()


def test_bypass_knobs_are_honoured(live_root, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(live_root))
    monkeypatch.setattr(hermes_state, "_STATE_DB_GUARD_BYPASS", True)
    _write_mock_call()
    assert (live_root / "blackbox" / "turns.db").exists()


def test_inert_in_production(live_root, monkeypatch):
    """A real gateway (not a test context) writes its live store as before."""
    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(live_root / "profiles" / "apollo"))
    _write_mock_call()
    assert (live_root / "profiles" / "apollo" / "blackbox" / "turns.db").exists()


@pytest.mark.parametrize(
    "rel, live",
    [
        ("blackbox/turns.db", True),
        ("profiles/daedalus/blackbox/turns.db", True),
        ("state.db", False),
        ("profiles/daedalus/state.db", False),
        ("hermes-agent/wt/blackbox/turns.db", False),
        ("profiles/daedalus/x/blackbox/turns.db", False),
    ],
)
def test_live_path_shapes(live_root, rel, live):
    assert store._is_live_blackbox_db(live_root / rel, live_root) is live
    assert store._is_live_blackbox_db(Path("/elsewhere") / rel, live_root) is False
