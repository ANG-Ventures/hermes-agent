"""The ref-integrity post-condition (tests/hermes_cli/conftest.py) itself.

t_f577ddd5: a transition must never leave a committed row naming a staged
attachment copy it deleted. These tests prove the checker sees that shape and
stays quiet on a clean transition.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.hermes_cli._kanban_ref_integrity import (
    assert_no_dangling_staged_refs,
    dangling_staged_refs,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "all")
    kb.init_db()
    return home


def _review_with_artifact(conn):
    tid = kb.create_task(conn, title="slice", assignee="builder")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, ws)
    artifact = ws / "evidence.json"
    artifact.write_bytes(b"{}")
    claimed = kb.claim_task(conn, tid)
    assert kb.request_review(
        conn, tid, summary="done", metadata={"artifacts": [str(artifact)]},
        expected_run_id=claimed.current_run_id, reviewer="human",
    ) is True
    return tid


def test_clean_review_transition_has_no_dangling_refs(kanban_home):
    with kb.connect() as conn:
        tid = _review_with_artifact(conn)
        stored = Path(kb.list_attachments(conn, tid)[0].stored_path)
        assert stored.is_file()
        assert dangling_staged_refs(conn, tid) == []


@pytest.mark.allow_dangling_staged_refs
def test_deleted_staged_copy_is_reported_on_every_committed_row(kanban_home):
    with kb.connect() as conn:
        tid = _review_with_artifact(conn)
        Path(kb.list_attachments(conn, tid)[0].stored_path).unlink()
        found = dangling_staged_refs(conn, tid)
        assert {f.split("#")[0] for f in found} == {
            "task_attachments", "task_runs", "task_events",
        }
        with pytest.raises(AssertionError, match="request_review"):
            assert_no_dangling_staged_refs(conn, tid, after="request_review")


@pytest.mark.allow_dangling_staged_refs
def test_post_commit_cleanup_of_staged_copies_is_caught(kanban_home, monkeypatch):
    """The #1859 shape, reproduced generically: the transition commits, then
    a later step fails and its cleanup discards the copies the committed rows
    name. The post-condition must flag it even though the call raised."""
    real_txn = kb._request_review_txn

    def commit_then_fail(*a, **k):
        real_txn(*a, **k)
        raise OSError("disk full after commit")

    monkeypatch.setattr(kb, "_request_review_txn", commit_then_fail)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="builder")
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, ws)
        artifact = ws / "evidence.json"
        artifact.write_bytes(b"{}")
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(OSError):
            kb.request_review(
                conn, tid, summary="done", metadata={"artifacts": [str(artifact)]},
                expected_run_id=claimed.current_run_id, reviewer="human",
            )
        assert kb.get_task(conn, tid).status == "review"
        with pytest.raises(AssertionError, match="staged copies that do not exist"):
            assert_no_dangling_staged_refs(conn, tid, after="request_review")
