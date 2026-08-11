"""Zero-byte kanban ``.db`` files must not exist, and must not read as empty boards.

Two halves of one defect:

1. **Source.** ``sqlite3.connect()`` creates the file before any schema runs,
   so a ``connect()`` whose init raises partway leaves a 0-byte ``.db`` behind.
2. **Readers.** ``sqlite3.connect('file:<zero-byte>.db?mode=ro', uri=True)``
   succeeds and reports an empty ``sqlite_master``, so a reader concludes
   "this board is empty" instead of "I opened the wrong file".

Measured on a live fleet: 14 zero-byte ``.db`` stubs under ``~/.hermes/kanban``,
one of which sat next to a 7.6 MB board holding 247 tasks.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    """Isolated kanban root. Strips every ``HERMES_KANBAN_*`` path pin.

    ``kanban_db_path()`` checks ``HERMES_KANBAN_DB`` BEFORE anything
    ``HERMES_HOME``-derived, and the dispatcher pins it into every worker's
    env — so ``monkeypatch.setenv("HERMES_HOME", ...)`` alone resolves to the
    LIVE production board when this suite runs inside a kanban worker.
    """
    for key in [k for k in list(__import__("os").environ) if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    resolved = kb.kanban_db_path(board="probe")
    assert str(resolved).startswith(str(tmp_path)), f"sandbox escape: {resolved}"
    return home


def _forget(path: Path) -> None:
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))


# --------------------------------------------------------------------------
# The fail-open this suite exists to close (characterisation of stdlib).
# --------------------------------------------------------------------------


def test_stdlib_readonly_open_of_zero_byte_db_fails_open(tmp_path):
    """Baseline: a bare ``mode=ro`` open of a 0-byte file reports an EMPTY DB.

    Not a test of our code — it pins the stdlib behaviour that makes a stray
    zero-byte ``.db`` dangerous, so the guard below has a documented reason
    to exist.
    """
    stub = tmp_path / "ban-forensics.db"
    stub.touch()
    assert stub.stat().st_size == 0

    conn = sqlite3.connect(stub.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # No error. No tables. Indistinguishable from a board with no tasks.
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (2) Readers fail LOUD.
# --------------------------------------------------------------------------


def test_connect_readonly_raises_on_zero_byte_db(tmp_path):
    stub = tmp_path / "ban-forensics.db"
    stub.touch()

    with pytest.raises(kb.KanbanDbNotABoardError) as excinfo:
        kb.connect_readonly(stub)

    message = str(excinfo.value)
    assert str(stub) in message, "error must name the offending path"
    assert "0 bytes" in message
    assert "tasks" in message


def test_connect_readonly_raises_on_sqlite_file_without_tasks_table(tmp_path):
    """A real SQLite file that is not a board is still not a board.

    Guards against a check that only looks at file size: a non-empty ``.db``
    from some other subsystem must not read as a kanban board either.
    """
    other = tmp_path / "notes.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.commit()
    conn.close()
    assert other.stat().st_size > 0

    with pytest.raises(kb.KanbanDbNotABoardError):
        kb.connect_readonly(other)


def test_connect_readonly_opens_a_real_board(kanban_root):
    """The other half of the born-red pair: a VALID board still opens cleanly."""
    db_path = kb.kanban_db_path(board="probe")
    kb.init_db(board="probe")
    with contextlib.closing(kb.connect(db_path)) as conn:
        kb.create_task(conn, title="real task")

    conn = kb.connect_readonly(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_connect_readonly_missing_file_raises_filenotfound(tmp_path):
    """A missing path is a different error than a stub — and names the path."""
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError) as excinfo:
        kb.connect_readonly(missing)
    assert str(missing) in str(excinfo.value)


def test_connect_readonly_never_creates_the_file(tmp_path):
    """A read-only probe must not manufacture the very stub it guards against."""
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        kb.connect_readonly(missing)
    assert not missing.exists()


def test_error_hints_at_the_canonical_path_for_a_sibling_stub(tmp_path):
    """``boards/<slug>.db`` → hint at ``boards/<slug>/kanban.db``.

    This is the exact live shape: a 0-byte ``boards/ban-forensics.db`` beside
    a real ``boards/ban-forensics/kanban.db``.
    """
    boards = tmp_path / "kanban" / "boards"
    (boards / "ban-forensics").mkdir(parents=True)
    real = boards / "ban-forensics" / "kanban.db"
    conn = sqlite3.connect(str(real))
    conn.executescript(kb.SCHEMA_SQL)
    conn.commit()
    conn.close()

    stub = boards / "ban-forensics.db"
    stub.touch()

    with pytest.raises(kb.KanbanDbNotABoardError) as excinfo:
        kb.connect_readonly(stub)
    assert str(real) in str(excinfo.value), "must point the reader at the real board"


def test_error_hints_at_kanban_db_for_a_board_db_stub(tmp_path):
    """``boards/<slug>/board.db`` → hint at ``boards/<slug>/kanban.db``."""
    board_dir = tmp_path / "kanban" / "boards" / "house-voice"
    board_dir.mkdir(parents=True)
    real = board_dir / "kanban.db"
    conn = sqlite3.connect(str(real))
    conn.executescript(kb.SCHEMA_SQL)
    conn.commit()
    conn.close()

    stub = board_dir / "board.db"
    stub.touch()

    with pytest.raises(kb.KanbanDbNotABoardError) as excinfo:
        kb.connect_readonly(stub)
    assert str(real) in str(excinfo.value)


def test_no_hint_when_there_is_no_real_counterpart(tmp_path):
    """A wrong guess is worse than no guess — stay silent when unsure."""
    boards = tmp_path / "kanban" / "boards"
    boards.mkdir(parents=True)
    stub = boards / "never-existed.db"
    stub.touch()

    with pytest.raises(kb.KanbanDbNotABoardError) as excinfo:
        kb.connect_readonly(stub)
    assert "Did you mean" not in str(excinfo.value)


def test_readonly_guard_does_not_autocreate_schema(tmp_path):
    """Explicitly NOT the fix: the stub must stay empty, not become a board.

    Auto-initialising the schema would manufacture a real-looking empty board
    and make the confusion permanent.
    """
    stub = tmp_path / "stub.db"
    stub.touch()
    with pytest.raises(kb.KanbanDbNotABoardError):
        kb.connect_readonly(stub)
    assert stub.stat().st_size == 0, "guard must not write schema into the stub"


# --------------------------------------------------------------------------
# (1) Source: no zero-byte .db left behind.
# --------------------------------------------------------------------------


def test_connect_leaves_no_zero_byte_stub_when_init_fails(kanban_root):
    """The half-init that created the live stubs.

    ``_sqlite_connect`` has already created the file by the time WAL setup
    runs; if that raises, the caller sees the error but a 0-byte ``.db``
    stays on disk as a decoy for every future reader.
    """
    db_path = kb.kanban_db_path(board="probe")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _forget(db_path)

    with mock.patch(
        "hermes_state.apply_wal_with_fallback",
        side_effect=RuntimeError("simulated init failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated init failure"):
            kb.connect(db_path)

    assert not db_path.exists(), (
        f"connect() left a zero-byte stub at {db_path} — every read-only "
        f"reader will now report this board as EMPTY instead of erroring"
    )


def test_connect_failure_leaves_no_zero_byte_sidecars(kanban_root):
    """WAL/SHM sidecars of a discarded stub go too, not just the .db."""
    db_path = kb.kanban_db_path(board="probe")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _forget(db_path)

    with mock.patch(
        "hermes_state.apply_wal_with_fallback",
        side_effect=RuntimeError("simulated init failure"),
    ):
        with pytest.raises(RuntimeError):
            kb.connect(db_path)

    leftovers = sorted(p.name for p in db_path.parent.glob(db_path.name + "*")
                       if p.is_file() and not p.name.endswith(".init.lock"))
    assert leftovers == [], f"stub artifacts left behind: {leftovers}"


def test_connect_failure_preserves_an_existing_populated_db(kanban_root):
    """🔴 The guard must NEVER delete real data.

    A failed init against an EXISTING board leaves that board untouched —
    the cleanup applies only to a file this ``connect()`` just created.
    """
    db_path = kb.kanban_db_path(board="probe")
    kb.init_db(board="probe")
    with contextlib.closing(kb.connect(db_path)) as conn:
        kb.create_task(conn, title="precious")
    size_before = db_path.stat().st_size
    assert size_before > 0
    _forget(db_path)

    with mock.patch(
        "hermes_state.apply_wal_with_fallback",
        side_effect=RuntimeError("simulated init failure"),
    ):
        with pytest.raises(RuntimeError):
            kb.connect(db_path)

    assert db_path.exists(), "an existing board must survive a failed re-open"
    assert db_path.stat().st_size == size_before

    conn = kb.connect_readonly(db_path)
    try:
        titles = [r[0] for r in conn.execute("SELECT title FROM tasks")]
    finally:
        conn.close()
    assert titles == ["precious"]


def test_successful_connect_still_creates_a_real_board(kanban_root):
    """Negative control: the cleanup path does not fire on success."""
    db_path = kb.kanban_db_path(board="probe")
    _forget(db_path)
    conn = kb.connect(db_path)
    try:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    finally:
        conn.close()
    assert db_path.exists() and db_path.stat().st_size > 0
