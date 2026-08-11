"""The routing-write half of the live-DB isolation guard.

Forensic background (card t_7f0d11af, measured 2026-08-10): the production
``~/.hermes/state.db`` held **3,044** ``gateway_routing`` rows whose ``scope``
was a pytest tempdir (``/private/var/.../pytest-of-*/...``), against 357 real
rows — 89% of the table was test residue, accumulated 2026-07-11..07-26.

The shape that produced them is specific, and it is NOT "a test passed the
production path on purpose":

    SessionStore(sessions_dir=<hermetic tmp>, config=...)

``sessions_dir`` is hermetic, so ``_routing_scope()`` is a tmp path and the
rows look sandboxed — but the store binds its database with an **argless**
``hermes_state.SessionDB()``. If that resolution reaches the real
``~/.hermes/state.db`` (a subprocess child without ``HERMES_HOME``, a
collection-time import that froze ``DEFAULT_DB_PATH`` before the redirect, a
developer shell exporting the production home), every routing write lands in
the live database under a tmp scope. Scope-keying kept it inert — real routing
lookups filter on the real scope — but that is a property of the schema, not a
guarantee anyone designed.

``tests/hermes_state/test_live_db_isolation_guard.py`` covers ``SessionDB``
construction. These tests cover the **write path that actually leaked**: they
drive ``SessionStore``'s real routing persistence and assert the production
table is unreachable from it.

Behavioral, not source-reading: each test drives real objects and asserts
outcomes. The production DB is only ever *counted* (read-only URI), never
written — the guard is expected to fire first, and the counts prove it did.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import hermes_state
from gateway.config import GatewayConfig
from gateway.session import SessionStore

#: The real Hermes root, derived the same way the guard itself derives it
#: (``hermes_state._real_platform_state_root``) — via ``os.path.expanduser``,
#: NOT ``Path.home()`` (tests monkeypatch that) and NOT ``HERMES_REAL_HOME``
#: (in a kanban worker env that is the plain home dir, not the Hermes root).
REAL_ROOT = (Path(os.path.expanduser("~")) / ".hermes").resolve()
PROD_DB = REAL_ROOT / "state.db"


def _prod_routing_rows() -> int | None:
    """Count rows in the production ``gateway_routing``, read-only.

    Returns ``None`` when there is no production DB to read (CI, a fresh
    machine) so the tests degrade to "nothing to protect" instead of
    inventing a number. Never creates the file: a plain ``sqlite3.connect``
    on a missing path CREATES it, so this uses an explicit ``mode=ro`` URI.
    """
    if not PROD_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        return int(conn.execute("SELECT count(*) FROM gateway_routing").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@pytest.fixture
def production_home(monkeypatch):
    """Recreate the escape: HERMES_HOME back at the real root.

    Also neutralizes the hermetic conftest's ``DEFAULT_DB_PATH`` re-pin, so
    the argless resolver follows the (production-pointing) environment — the
    state a subprocess child or a pre-redirect import actually lands in.
    """
    monkeypatch.setenv("HERMES_HOME", str(REAL_ROOT))
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH
    )


class TestRoutingWritePathCannotReachProduction:
    """The leak shape itself: hermetic scope, production database."""

    def test_session_store_with_tmp_scope_cannot_bind_production_db(
        self, tmp_path, production_home
    ):
        """SessionStore(sessions_dir=<tmp>) must NOT open the production DB.

        This is the exact construction that wrote 3,044 tmp-scoped rows into
        the live table. The store's own contract is that a guard error is
        re-raised rather than swallowed into the JSONL fallback, so the
        assertion is that it raises — not that it quietly degrades.
        """
        before = _prod_routing_rows()

        with pytest.raises(RuntimeError, match="live-system guard"):
            SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())

        after = _prod_routing_rows()
        assert after == before, (
            f"production gateway_routing changed during a guarded construction: "
            f"{before} -> {after}"
        )

    def test_routing_save_lands_in_the_sandbox_not_production(self, tmp_path):
        """The happy path still works, and writes ONLY to the sandbox.

        Without this, a guard that refused everything would look identical to
        a guard that works. Drives a real routing persist against a hermetic
        HERMES_HOME and asserts the row is in the sandbox DB while the
        production count is untouched.
        """
        before = _prod_routing_rows()

        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
        assert store._db is not None, "hermetic store should have a database"

        db_path = Path(store._db.db_path).resolve()
        assert db_path != PROD_DB
        # The sandbox DB must be inside the test's own tmp tree, by real path
        # containment (not str.startswith — sibling dirs like t0 / t01 would
        # both match a string prefix).
        db_path.relative_to(tmp_path.resolve())

        scope = store._routing_scope()
        store._db.save_gateway_routing_entry("guard-probe-key", "{}", scope=scope)

        entries = store._db.load_gateway_routing_entries(scope=scope)
        assert "guard-probe-key" in entries

        store._db.close()

        after = _prod_routing_rows()
        assert after == before, (
            f"a hermetic routing write reached production: {before} -> {after}"
        )

    def test_routing_scope_is_the_sandbox_path(self, tmp_path):
        """The scope must be the hermetic sessions_dir.

        A tmp scope is what made the leaked rows *look* sandboxed while living
        in the production file; pinning the scope's identity here keeps the
        two halves of the invariant (scope AND database) explicit.
        """
        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
        try:
            assert store._routing_scope() == str(sessions_dir.resolve())
        finally:
            if store._db is not None:
                store._db.close()


class TestProductionRoutingTableStaysClean:
    """Standing assertion over the operator's real database."""

    def test_no_pytest_scoped_rows_in_production_routing(self):
        """The production routing index must hold no test-shaped scopes.

        This is the regression detector for the original finding: if a future
        change reopens the leak, a full suite run leaves rows whose scope is a
        pytest tempdir and this fails RED with the offending scopes named.

        Skips when there is no production DB (CI) — there is nothing to
        protect, and inventing a pass would be worse than saying so.
        """
        if not PROD_DB.exists():
            pytest.skip("no production state.db on this machine")

        try:
            conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            pytest.skip(f"production state.db unreadable: {exc}")

        try:
            scopes = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT scope FROM gateway_routing"
                ).fetchall()
            ]
        except sqlite3.Error as exc:
            pytest.skip(f"production gateway_routing unreadable: {exc}")
        finally:
            conn.close()

        markers = ("pytest-of-", "pytest-", "hermes-test-", "/tmp/", "basetemp")
        polluted = [
            s
            for s in scopes
            if any(m in s for m in markers) and not s.startswith(str(REAL_ROOT))
        ]
        assert not polluted, (
            "production gateway_routing holds test-scoped rows — the "
            "pytest→production leak (card t_7f0d11af) has returned. Offending "
            f"scopes: {polluted[:5]}{'...' if len(polluted) > 5 else ''}"
        )
