"""Pre-dispatch file-scope collision warnings."""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def _running_task(conn, *, body: str = "source") -> str:
    task_id = kb.create_task(conn, title="source", body=body, assignee="coder")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    return task_id


def _ready_task(conn, body: str) -> str:
    return kb.create_task(conn, title="new", body=body, assignee="coder")


def _comment_changed_files(conn, task_id: str, *paths: str) -> None:
    kb.add_comment(
        conn,
        task_id,
        "worker",
        "review-required handoff:\n```json\n"
        + json.dumps({"changed_files": list(paths)}, indent=2)
        + "\n```",
    )


def test_real_incident_replay_names_both_card_ids(conn, caplog):
    new_id = "t_e2096e27"
    existing_id = "t_6f8721d6"
    new_body = (
        "Extend `wirelog-coverage-check.py` from 2 lanes to ALL SIX. "
        "Current file: `~/.hermes/scripts/wirelog-coverage-check.py:106`."
    )
    real_handoff_shape = json.dumps({
        "branch": "ban-forensics/t_6f8721d6-wire-capture-provisioning",
        "changed_file_count": 23,
        "changed_files": [
            "scripts/claude-cpx",
            "scripts/wirelog-coverage-check.py",
            "scripts/wirelog-coverage-cron.sh",
        ],
    })
    scopes = {
        existing_id: kb._extract_reported_changed_files(real_handoff_shape)
    }
    result = kb.DispatchResult()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        kb._check_dispatch_file_collisions(
            conn,
            result,
            task_id=new_id,
            body=new_body,
            reported_scopes=scopes,
            dry_run=True,
        )

    assert result.collision_warnings == [
        (new_id, existing_id, ["scripts/wirelog-coverage-check.py"])
    ]
    assert new_id in caplog.text
    assert existing_id in caplog.text


def test_dry_run_warns_on_incident_shape_and_names_both_cards(conn, caplog):
    source = _running_task(conn)
    _comment_changed_files(conn, source, "scripts/wirelog-coverage-check.py")
    new = _ready_task(
        conn,
        "Extend `wirelog-coverage-check.py` from 2 lanes. Current file: "
        "`~/.hermes/scripts/wirelog-coverage-check.py:106`.",
    )

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_warnings == [
        (new, source, ["scripts/wirelog-coverage-check.py"])
    ]
    assert new in caplog.text
    assert source in caplog.text
    assert "scripts/wirelog-coverage-check.py" in caplog.text
    assert kb.list_comments(conn, new) == []


def test_real_dispatch_persists_warning_on_both_cards_without_blocking(conn):
    source = _running_task(conn)
    _comment_changed_files(conn, source, "scripts/worker.py")
    new = _ready_task(conn, "Change `scripts/worker.py`.")

    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123)

    assert result.spawned[0][0] == new
    assert kb.get_task(conn, new).status == "running"
    assert result.collision_warnings == [(new, source, ["scripts/worker.py"])]
    new_warning = kb.list_comments(conn, new)[0].body
    source_warning = kb.list_comments(conn, source)[1].body
    for warning in (new_warning, source_warning):
        assert "DISPATCH COLLISION WARNING" in warning
        assert new in warning
        assert source in warning
        assert "scripts/worker.py" in warning
        assert "dispatch continues" in warning.lower()
    events = [e for e in kb.list_events(conn, new) if e.kind == "dispatch_collision_warning"]
    assert len(events) == 1


def test_collision_comment_transaction_finishes_before_claim(
    conn,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _running_task(conn)
    _comment_changed_files(conn, source, "src/shared.py")
    new = _ready_task(conn, "Modify `src/shared.py`.")
    original_record = kb._record_dispatch_collision_warning
    transaction_states: list[tuple[str, bool]] = []

    def record_with_boundary_probe(*args, **kwargs):
        transaction_states.append(("before", conn.in_transaction))
        original_record(*args, **kwargs)
        transaction_states.append(("after", conn.in_transaction))

    monkeypatch.setattr(
        kb, "_record_dispatch_collision_warning", record_with_boundary_probe
    )
    spawn_calls: list[str] = []

    def spawn(task, _workspace):
        spawn_calls.append(task.id)
        return 123

    result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert transaction_states == [("before", False), ("after", False)]
    assert spawn_calls == [new]
    assert result.spawned[0][0] == new
    claimed = kb.get_task(conn, new)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.current_run_id is not None
    assert conn.in_transaction is False


def test_running_scope_can_come_from_structured_run_metadata(conn):
    source = _running_task(conn)
    run_id = kb.get_task(conn, source).current_run_id
    assert run_id is not None
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps({"changed_files": ["src/router.py"]}), run_id),
    )
    conn.commit()
    new = _ready_task(conn, "Modify `src/router.py`.")

    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_warnings == [(new, source, ["src/router.py"])]


def test_recently_blocked_scope_is_checked_but_stale_block_is_ignored(conn):
    recent = _running_task(conn)
    _comment_changed_files(conn, recent, "src/shared.py")
    assert kb.block_task(conn, recent, reason="review-required")

    stale = _running_task(conn)
    _comment_changed_files(conn, stale, "src/stale.py")
    assert kb.block_task(conn, stale, reason="review-required")
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
        (int(time.time()) - kb.DISPATCH_COLLISION_RECENT_BLOCKED_SECONDS - 1, stale),
    )
    conn.commit()

    new = _ready_task(conn, "Touch `src/shared.py` and `src/stale.py`.")
    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_warnings == [(new, recent, ["src/shared.py"])]


def test_unknown_new_scope_and_unreported_running_scope_are_explicit(conn, caplog):
    reported = _running_task(conn)
    _comment_changed_files(conn, reported, "src/reported.py")
    unknown_new = _ready_task(conn, "Research the dispatch behavior; no files selected yet.")

    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db"):
        first = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert first.collision_scope_unknown == [unknown_new]
    assert "SCOPE UNKNOWN" in caplog.text

    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (unknown_new,))
    conn.commit()
    unreported = _running_task(conn)
    known_new = _ready_task(conn, "Modify `src/reported.py`.")

    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db"):
        second = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert second.collision_warnings == [
        (known_new, reported, ["src/reported.py"])
    ]
    assert second.collision_scope_unreported == [(known_new, [unreported])]
    assert "CHECK PARTIAL" in caplog.text
    assert known_new in caplog.text
    assert unreported in caplog.text


def test_disjoint_reported_scope_does_not_warn(conn):
    source = _running_task(conn)
    _comment_changed_files(conn, source, "src/a.py")
    new = _ready_task(conn, "Modify `src/b.py`.")

    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_warnings == []
    assert result.collision_scope_unknown == []
    assert result.collision_scope_unreported == []


def test_bare_filename_does_not_suffix_match_unrelated_directory(conn):
    source = _running_task(conn)
    _comment_changed_files(conn, source, "package/config.py")
    new = _ready_task(conn, "Modify `config.py`.")

    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_warnings == []
    assert result.collision_scope_unknown == []


def test_lane_vocabulary_is_not_misreported_as_file_scope(conn):
    new = _ready_task(conn, "Compare `apx/bpx` with `cpx/cpr`; no file selected.")

    result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123, dry_run=True)

    assert result.collision_scope_unknown == [new]


def test_collision_check_failure_is_loud_but_never_blocks_spawn(
    conn,
    caplog,
    monkeypatch: pytest.MonkeyPatch,
):
    new = _ready_task(conn, "Modify `src/router.py`.")

    def broken_scope_load(_conn):
        raise RuntimeError("synthetic scope read failure")

    monkeypatch.setattr(kb, "_load_dispatch_collision_scopes", broken_scope_load)
    with caplog.at_level(logging.ERROR, logger="hermes_cli.kanban_db"):
        result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 123)

    assert result.spawned[0][0] == new
    assert result.collision_check_failed == [new]
    assert "FAILED OPEN" in caplog.text
    assert "dispatch continues" in caplog.text


def test_cli_surfaces_collision_and_incomplete_coverage(
    conn,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import kanban as cli
    from hermes_cli import config

    result = kb.DispatchResult(
        collision_warnings=[("t_new", "t_running", ["src/shared.py"])],
        collision_scope_unknown=["t_unknown"],
        collision_scope_unreported=[("t_new", ["t_unreported"])],
        collision_check_failed=["t_failed"],
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda *_a, **_k: result)
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {}})

    json_args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=True
    )
    assert cli._cmd_dispatch(json_args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collision_warnings"] == [{
        "task_id": "t_new",
        "existing_task_id": "t_running",
        "paths": ["src/shared.py"],
    }]
    assert payload["collision_scope_unknown"] == ["t_unknown"]
    assert payload["collision_scope_unreported"] == [{
        "task_id": "t_new",
        "unchecked_task_ids": ["t_unreported"],
    }]
    assert payload["collision_check_failed"] == ["t_failed"]

    text_args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=False
    )
    assert cli._cmd_dispatch(text_args) == 0
    output = capsys.readouterr().out
    assert "t_new overlaps t_running: src/shared.py" in output
    assert "collision scope unknown" in output
    assert "collision check partial" in output
    assert "collision check failed open" in output
