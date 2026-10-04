"""Live-DB test-isolation guard and the per-process "last init error" record.
Every SessionDB construction resolves its path through _ensure_test_isolation
so a pytest-context process (env OR ancestry) can never open a production
state.db; env-based so subprocess children are protected too."""

import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Optional

# Field evidence: pytest fixture rows landed in the production state.db and a
# pytest-spawned child flipped the journal mode under the live WAL writer.

#: Env twin of ``_STATE_DB_GUARD_BYPASS`` for child processes (a module global
#: cannot cross a process boundary, and ancestry arms the guard there).
_STATE_DB_GUARD_BYPASS_ENV = "HERMES_STATE_DB_GUARD_BYPASS"


def _real_platform_state_root() -> Optional[Path]:
    """The REAL platform-default Hermes root. Avoids ``Path.home()`` /
    ``hermes_constants`` (tests monkeypatch Path.home to a tempdir).

    Anchored on the OS ACCOUNT home (``hermes_state._os_account_home``, the passwd entry) rather
    than ``os.path.expanduser("~")``, which on POSIX is just ``$HOME``: reading ``$HOME`` made this
    answer "production" for the tmpdir of any test using the hermetic ``monkeypatch.setenv("HOME",
    tmp_path)`` idiom, so both guards refused a hermetic board (2026-09-21: 2 files / 24 tests red).
    A guard that fires on the standard isolation idiom teaches people to disarm it globally; keeping
    it precise is what keeps it armed. The account home is still not monkeypatchable from inside a
    test, so the property the old anchor protected is preserved."""
    try:
        from hermes_state import _os_account_home
        home = _os_account_home() or Path(os.path.expanduser("~"))
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA", "").strip()
            root = Path(base) / "hermes" if base else home / "AppData" / "Local" / "hermes"
        else:
            root = home / ".hermes"
        return root.resolve()
    except Exception:
        return None


#: Exported by the hermetic conftest alongside the HERMES_HOME redirect. Unlike
#: PYTEST_* it is OURS and inherits by default, so a child carrying it that
#: resolves a production DB is by definition an isolation escape.
# : Env marker exported by the hermetic test conftest at the same moment it : redirects ``HERMES_HOME`` to
# the per-session tmp isolation root. Unlike ``PYTEST_*`` (owned by pytest, and : routinely scrubbed by
# tests that rebuild a child environment), this marker : is OURS: it declares "this process tree is running
# under Hermes test : isolation", and it inherits into subprocess children by default — so a : child that
# received the patched ``HERMES_HOME`` also received the marker, : and a child that resolves a production DB
# while carrying it is, by : definition, an isolation escape (#82770).
# One definition of the test-context predicate, in the leaf module ``hermes_test_context`` (no
# dependency graph: ``hermes_state`` -> ``agent.redact`` snapshots its toggle at import). Re-exported
# here so the guard, ``hermes_state`` and ``managed_scope`` all bind the SAME function
# (tests/hermes_cli/test_managed_scope_test_context.py).
from hermes_test_context import (  # noqa: E402,F401
    _TEST_ISOLATION_MARKER_ENV,
    _has_pytest_ancestor,
    _in_test_context,
    _process_looks_like_pytest,
    _running_under_pytest,
)


def _is_production_state_db(resolved: Path, root: Path) -> bool:
    """*resolved* is ``<root>/state.db`` or ``<root>/profiles/<name>/state.db``;
    deeper scratch paths (repo worktrees) are deliberately NOT matched."""
    if resolved.parent == root:
        return True
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        return False
    return len(parts) == 3 and parts[0] == "profiles"


# Test-only SessionDB instance registry. Under the hermetic suite every
# successfully constructed SessionDB is added to this WeakSet so the autouse
# teardown in tests/conftest.py (_close_leaked_session_dbs) can close whatever
# a test forgot to close. Dozens of tests build SessionDB() directly and never
# close it; each instance holds a writer connection plus pooled readers, and a
# single-process run over tests/hermes_cli/ accumulated 16-25 GB RSS (OOM
# incident 20260816). The per-file runner masks this in CI; the registry fixes
# the class at the source instead of patching ~40 test files.
#
# Population is gated on the isolation marker (exported by tests/conftest.py
# before any test module imports). Production processes never populate it;
# do not "simplify" the gate away. WeakSet membership never pins an instance.
_test_instance_registry: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _register_test_instance(db: Any) -> None:
    """Track *db* for suite-level teardown closing (test-isolation runs only)."""
    if os.environ.get(_TEST_ISOLATION_MARKER_ENV):
        try:
            _test_instance_registry.add(db)
        except Exception:  # pragma: no cover — registry must never break init
            pass


# Last SessionDB() init error, per-process; surfaced by /resume-style slash
# commands so users know WHY. Only SessionDB.__init__ writes it.
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()


def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear with None) the most recent init failure. __init__ never
    clears on success: a concurrent open would erase the cause another thread's
    /resume is about to format."""
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Most recent state.db init failure (None if none/never attempted)."""
    return _last_init_error
