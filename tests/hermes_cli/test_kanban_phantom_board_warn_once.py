"""list_boards() must warn about a given phantom board dir at most once per process.

Live incident (2026-09-23..25): one empty phantom dir (kanban.db, 0 tasks, no
board.json) produced the "ignoring phantom board dir" warning on every
watcher/dispatcher tick (~5 s) — 23k lines in gateway.error.log that buried
real errors.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in [k for k in list(kb.os.environ) if k.startswith("HERMES_KANBAN_")]:
        if key != "HERMES_KANBAN_SANDBOX":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(kb, "_PHANTOM_BOARD_WARNED", set())
    kb._INITIALIZED_PATHS.clear()
    kb._BOARD_ALIAS_CACHE = (None, {})
    yield tmp_path
    kb._INITIALIZED_PATHS.clear()
    kb._BOARD_ALIAS_CACHE = (None, {})


def _phantom_hits(caplog):
    return [r for r in caplog.records if "ignoring phantom board dir" in r.getMessage()]


def test_phantom_warning_emitted_once_across_repeated_scans(kanban_home, caplog):
    kb.connect(board="phantom").close()

    with caplog.at_level("WARNING", logger=kb._log.name):
        for _ in range(5):
            slugs = [b.get("slug") for b in kb.list_boards()]
            assert "phantom" not in slugs  # still skipped on every scan

    hits = _phantom_hits(caplog)
    assert len(hits) == 1
    assert "phantom" in hits[0].getMessage()


def test_distinct_phantom_gets_its_own_single_warning(kanban_home, caplog):
    kb.connect(board="phantom").close()
    kb.list_boards()

    kb.connect(board="phantom-two").close()
    caplog.clear()
    with caplog.at_level("WARNING", logger=kb._log.name):
        kb.list_boards()
        kb.list_boards()

    hits = _phantom_hits(caplog)
    assert len(hits) == 1
    assert "phantom-two" in hits[0].getMessage()
