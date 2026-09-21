"""Board tick contention must be observable on the very first skipped tick."""
import argparse
import json
import logging
import os

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_dispatch_lock import kanban_home, conn  # noqa: F401


def test_first_contended_tick_reports_holder(conn, monkeypatch, caplog):
    db_path = kb.kanban_db_path()
    monkeypatch.setattr(kb.time, "monotonic", lambda: 100.0)
    with kb._dispatch_tick_lock(db_path) as held:
        assert held
        monkeypatch.setattr(kb.time, "monotonic", lambda: 107.0)
        with caplog.at_level(logging.WARNING):
            result = kb.dispatch_once(conn, dry_run=True)
        assert result.skipped_locked
        assert result.lock_holder["pid"] == os.getpid()
        assert result.lock_holder["age_seconds"] == 7.0
        assert "_dispatch_tick_lock" in result.lock_holder["acquire_site"]
        assert str(os.getpid()) in caplog.text
        assert "7.0s" in caplog.text
        assert "_dispatch_tick_lock" in caplog.text
    assert kb._read_dispatch_lock_holder(db_path) == {}


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_reports_real_contention(conn, capsys, json_output):
    from hermes_cli.kanban import _cmd_dispatch

    with kb._dispatch_tick_lock(kb.kanban_db_path()) as held:
        assert held
        assert _cmd_dispatch(argparse.Namespace(dry_run=True, json=json_output)) == 0
    output = capsys.readouterr().out
    if json_output:
        data = json.loads(output)
        assert data["skipped_locked"] is True
        assert data["lock_holder"]["pid"] == os.getpid()
    else:
        assert "skipped: board dispatcher lock" in output
        assert str(os.getpid()) in output
        assert "_dispatch_tick_lock" in output


@pytest.mark.parametrize("payload", [b"", b" {", b" []", b' {"pid": "bad"}'])
def test_legacy_or_malformed_stamp_does_not_hide_skip(conn, payload):
    path = kb.kanban_db_path()
    with kb._dispatch_tick_lock(path) as held:
        assert held
        path.with_name(path.name + ".dispatch.lock").write_bytes(payload)
        result = kb.dispatch_once(conn, dry_run=True)
        assert result.skipped_locked
        assert result.lock_holder == {}
