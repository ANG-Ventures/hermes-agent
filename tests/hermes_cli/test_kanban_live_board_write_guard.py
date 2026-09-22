"""``connect()`` must refuse to open the LIVE board from a test/probe process.

Card t_2f909ab6. On 2026-09-21 three fixture cards ("bundle partial" x2,
"partial loss") were created on the live ``~/.hermes/kanban.db`` by probe
scripts, and the dispatcher claimed each one and spawned a real worker against
it — three burned runs. The only guard was ``tests/conftest.py``'s
``HERMES_KANBAN_*`` env scrub, which is PATH-SCOPED: it loads when pytest
collects a file under ``tests/``, so a probe living anywhere else keeps the
dispatcher-injected pin and resolves straight to production. The 2026-08-08
incident (six cards) had the same shape; the mitigation shipped then was a
docstring plus an opt-in ``HERMES_KANBAN_SANDBOX=1`` flag, i.e. something the
probe's author had to remember. It was not remembered.

``kanban_db._assert_live_board_write_allowed`` moves the guard to the
``connect()`` choke point so it holds regardless of where the ``.py`` file sits,
reusing the production-root list and test-context predicate ``state.db``'s
equivalent guard (``hermes_state._ensure_test_isolation``) has carried since the
2026-07-24 WAL incident.

These tests pin BOTH refusal conditions and — just as important — pin that the
guard is inert for every production env shape, because a guard that refused a
real dispatcher would be far worse than the leak.

The "live" board here is always a FAKE root injected into
``hermes_state._STATE_DB_GUARD_EXTRA_DENY_ROOTS``; no test in this file
resolves a path under the machine's real ``~/.hermes``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

import hermes_state
import hermes_test_context
from hermes_cli import kanban_db as kb


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    """Declare a tmp dir to be a PRODUCTION Hermes root.

    ``_production_kanban_roots()`` delegates to
    ``hermes_state._production_state_roots()``, whose
    ``_STATE_DB_GUARD_EXTRA_DENY_ROOTS`` tuple is the documented injection point
    for "also treat this root as production" (``tests/conftest.py`` uses it to
    cover custom-``HERMES_HOME`` deployments). Setting it here relocates what
    the guard considers live, with no dependency on the real ``~/.hermes``.
    """
    root = tmp_path / "prodhome" / ".hermes"
    root.mkdir(parents=True)
    monkeypatch.setattr(
        hermes_state, "_STATE_DB_GUARD_EXTRA_DENY_ROOTS", (root.resolve(),)
    )
    kb._INITIALIZED_PATHS.clear()
    return root


@pytest.fixture
def as_production_process(monkeypatch):
    """Make the test-context predicate answer False, as production does.

    Needed for the inertness assertions: this suite *is* a pytest run, so R1
    would otherwise fire on every live-board open and the "a real dispatcher
    must still be able to write" cases could not be expressed at all. Only the
    predicate is faked; the path resolution under test is untouched.
    """
    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: False)


def _live_db(root: Path) -> Path:
    return root / "kanban.db"


# --- R1: a test-context process may not open a live board ------------------


def test_test_context_refused_on_live_board(live_root, monkeypatch):
    """The 16:05 shape: probe under pytest, pin inherited from the worker env."""
    monkeypatch.setenv("HERMES_HOME", str(live_root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_live_db(live_root)))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    with pytest.raises(kb.LiveBoardWriteRefused) as excinfo:
        kb.connect()
    message = str(excinfo.value)
    assert "HERMES_KANBAN_SANDBOX=1" in message
    assert str(_live_db(live_root)) in message


def test_named_board_of_the_live_root_is_refused_too(live_root, monkeypatch):
    """``<root>/kanban/boards/<slug>/kanban.db`` is a live board as well.

    A leak onto a named board is the same incident with a different slug; only
    the ``default`` board happens to use the back-compat top-level path.
    """
    board_db = live_root / "kanban" / "boards" / "proj" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    with pytest.raises(kb.LiveBoardWriteRefused):
        kb.connect()
    # And refusing must not leave a stub DB or board dir behind.
    assert not board_db.exists()
    assert not board_db.parent.exists()


def test_explicit_db_path_argument_is_also_refused(live_root, monkeypatch):
    """Handing connect() the live path directly is the same leak, other door."""
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    with pytest.raises(kb.LiveBoardWriteRefused):
        kb.connect(db_path=_live_db(live_root))


def test_init_db_is_covered_by_the_same_gate(live_root, monkeypatch):
    """``init_db`` routes through connect(), so it must refuse too."""
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    with pytest.raises(kb.LiveBoardWriteRefused):
        kb.init_db(db_path=_live_db(live_root))


def test_second_connect_cannot_slip_past_the_init_cache(
    live_root, monkeypatch, as_production_process
):
    """The guard runs ahead of ``_INITIALIZED_PATHS``, so it can't be warmed off.

    Without this ordering a probe could open the board once under a benign env,
    populate the per-path cache, then re-open it on the fast path with the
    guard skipped.
    """
    live = _live_db(live_root)
    monkeypatch.setenv("HERMES_HOME", str(live_root))  # production-shaped env
    kb.connect(db_path=live).close()  # allowed, and caches the path
    assert str(live.resolve()) in kb._INITIALIZED_PATHS

    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: True)
    with pytest.raises(kb.LiveBoardWriteRefused):
        kb.connect(db_path=live)


def test_guard_is_not_fooled_by_a_rebuilt_child_environment(
    live_root, monkeypatch
):
    """Ancestry, not just env, decides test context.

    A child spawned with a rebuilt environment loses ``PYTEST_*`` and
    ``HERMES_HOME`` together (#82770) — which is exactly the state in which it
    writes to production. The shared predicate walks the process tree, so
    scrubbing the env vars is not enough to get through.
    """
    for var in ("PYTEST_CURRENT_TEST", "HERMES_IN_PYTEST", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)

    assert hermes_test_context._in_test_context() is True
    with pytest.raises(kb.LiveBoardWriteRefused):
        kb.connect(db_path=_live_db(live_root))


# --- R2: a redirected HERMES_HOME that a pin overrode back to a live board --


def test_redirected_hermes_home_overridden_by_pin_is_refused(
    live_root, tmp_path, monkeypatch, as_production_process
):
    """The 16:30 shape: bare ``python probe.py``, NO pytest marker at all.

    The caller declared a throwaway ``HERMES_HOME`` — it believes it is
    sandboxed — but ``HERMES_KANBAN_DB`` outranks ``HERMES_HOME`` and pulled
    resolution back to the live board. R1 cannot see this one: the process is
    not a test context by env or by ancestry.
    """
    probe_home = tmp_path / "probe-partial-i9lt7ab5"
    probe_home.mkdir()
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(probe_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_live_db(live_root)))

    with pytest.raises(kb.LiveBoardWriteRefused) as excinfo:
        kb.connect()
    assert "HERMES_KANBAN_SANDBOX=1" in str(excinfo.value)


def test_sandbox_flag_is_the_documented_way_through(
    live_root, tmp_path, monkeypatch
):
    """``HERMES_KANBAN_SANDBOX=1`` neutralises the pin, so nothing is refused.

    The refusal message prescribes exactly this; it has to actually work, from
    a test context, with a live-board pin set.
    """
    probe_home = tmp_path / "sandboxed"
    probe_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(probe_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_live_db(live_root)))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="sandboxed fixture")
    assert kb.kanban_db_path() == probe_home / "kanban.db"
    assert (probe_home / "kanban.db").exists()
    assert not _live_db(live_root).exists()
    assert task_id


# --- inertness: the guard must never refuse a real production writer -------


@pytest.mark.parametrize("home_mode", ["unset", "root", "profile_dir"])
def test_production_env_shapes_are_never_refused(
    live_root, monkeypatch, as_production_process, home_mode
):
    """Every shape the fleet actually runs must still open the board RW.

    ``HERMES_HOME`` unset, pointed at the root, or pointed at
    ``<root>/profiles/<name>`` (the dispatcher's worker shape, which also
    carries the injected ``HERMES_KANBAN_DB`` pin). A guard that refused any of
    these would break the dispatcher outright — strictly worse than the leak it
    is closing.
    """
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    if home_mode == "unset":
        monkeypatch.delenv("HERMES_HOME", raising=False)
    elif home_mode == "root":
        monkeypatch.setenv("HERMES_HOME", str(live_root))
    else:
        profile = live_root / "profiles" / "daedalus-opus"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_live_db(live_root)))

    with kb.connect_closing() as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 0


def test_hermetic_tmp_board_is_untouched(tmp_path, live_root, monkeypatch):
    """A board outside every production root is a legitimate documented use.

    This is also what the whole rest of the suite does, so a regression here
    would take the suite down with it.
    """
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    elsewhere = tmp_path / "elsewhere" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(elsewhere))

    with kb.connect_closing() as conn:
        assert kb.create_task(conn, title="fine") is not None
    assert elsewhere.exists()


def test_scratch_paths_under_the_live_root_are_not_boards(
    live_root, monkeypatch
):
    """Containment alone must not condemn a path — only real board layouts.

    Worker scratch dirs live under ``<root>/kanban/workspaces/<task>/`` and
    repo worktrees under ``<root>/hermes-agent/``; a throwaway DB there is not
    the live board and refusing it would break legitimate work.
    """
    scratch = live_root / "kanban" / "workspaces" / "t_2f909ab6" / "kanban.db"
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(scratch))

    with kb.connect_closing() as conn:
        assert kb.create_task(conn, title="scratch fixture") is not None
    assert scratch.exists()
    assert not _live_db(live_root).exists()


def test_readonly_inspection_of_the_live_board_is_still_allowed(
    live_root, monkeypatch, as_production_process
):
    """The gate is on the WRITE path only — diagnostics must keep working.

    An operator triaging a leak needs to read the live board from exactly the
    kind of process the guard refuses to let write.
    """
    live = _live_db(live_root)
    monkeypatch.setenv("HERMES_HOME", str(live_root))  # production-shaped env
    kb.connect(db_path=live).close()  # created as a production writer would

    monkeypatch.setattr(hermes_test_context, "_in_test_context", lambda: True)
    with kb.connect_readonly(db_path=live) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 0
