"""A foreign_ok reason whose takeover comment add_comment would refuse is
refused when the reason is ACCEPTED, before any guarded write journals
(t_705b1458, hermes-agent#2466 Prism 34b90ddab22b).

Before: write_txn recorded the takeover at EXIT, so the guarded mutation's
events and the takeover event were already in the journal when add_comment
refused; SQLite rolled back, the journal kept phantom lines replay restores.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_journal

HOME = "20260922_000000_home"
OTHER = "20260922_111111_other"
BAD_REASON = "fixing the close-on parser"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HERMES_KANBAN_JOURNAL", raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    from hermes_cli import kanban_identity as _ki
    monkeypatch.setattr(_ki, "_runs_under_operator_gateway", lambda name: True)
    monkeypatch.setattr(_ki, "operator_label_proven", lambda _n: True)
    kb.init_db()
    return home


def _release_line_guard_on(monkeypatch):
    """The fleet release-line guard, on: any comment naming the token is refused."""
    from hermes_cli import kanban_close_on_guard as guard

    def check(author, body):
        if "close-on" in (body or ""):
            raise guard.CloseOnLineRefused("close-on token in prose")

    monkeypatch.setattr(guard, "check_comment", check)


def _journal_kinds(conn, since: int) -> list[str]:
    board = kb._journal_board_slug(conn)
    return [e.get("kind") for e in list(kanban_journal.read_entries(board))[since:]]


def _journal_len(conn) -> int:
    return len(list(kanban_journal.read_entries(kb._journal_board_slug(conn))))


def _review_card(conn) -> str:
    tid = kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME)
    run = kb.claim_task(conn, tid, claimer="worker-a:1")
    assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                             expected_run_id=run.current_run_id) is True
    return tid


def test_send_back_with_unwritable_foreign_ok_journals_nothing(kanban_home, monkeypatch):
    """Prism's repro: assign_task with request_changes from a foreign session,
    a foreign_ok reason the release-line guard rejects -> refused before any
    write, card unchanged, zero journal lines."""
    with kb.connect_closing() as conn:
        tid = _review_card(conn)
        before = kb.get_task(conn, tid)
        assert before.status == "review"
        _release_line_guard_on(monkeypatch)
        mark = _journal_len(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok=BAD_REASON):
            with pytest.raises(ValueError, match="close-on token"):
                kb.assign_task(conn, tid, "worker-b", request_changes_reason="rework",
                               operator="apollo")
        after = kb.get_task(conn, tid)
        assert (after.status, after.assignee, after.session_id) == (
            before.status, before.assignee, before.session_id)
        assert _journal_kinds(conn, mark) == []


def test_plain_guarded_write_with_unwritable_foreign_ok_journals_nothing(kanban_home, monkeypatch):
    """The general case: a plain (non-send-back) guarded write."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME)
        assert kb.block_task(conn, tid, reason="needs input")
        _release_line_guard_on(monkeypatch)
        mark = _journal_len(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok=BAD_REASON):
            with pytest.raises(ValueError, match="close-on token"):
                kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert (task.status, task.session_id) == ("blocked", HOME)
        assert _journal_kinds(conn, mark) == []
        assert [c.body for c in kb.list_comments(conn, tid)] == []


def test_record_foreign_action_appends_no_takeover_event_before_its_comment_validates(
    kanban_home, monkeypatch,
):
    """record_foreign_action judges its comment before the takeover event."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME)
        _release_line_guard_on(monkeypatch)
        actor = kb.MutationActor(session_ids=(OTHER,), profile="apollo",
                                 foreign_ok=BAD_REASON)
        mark = _journal_len(conn)
        with pytest.raises(ValueError, match="close-on token"):
            kb.record_foreign_action(conn, tid, "unblock", actor)
        assert _journal_kinds(conn, mark) == []
        assert kb.get_task(conn, tid).session_id == HOME


def test_writable_foreign_ok_still_records_takeover(kanban_home, monkeypatch):
    """A reason the guard accepts keeps today's behaviour."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME)
        assert kb.block_task(conn, tid, reason="needs input")
        _release_line_guard_on(monkeypatch)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="home session is gone"):
            assert kb.unblock_task(conn, tid)
        bodies = [c.body for c in kb.list_comments(conn, tid)]
        assert any(b.startswith("takeover:") and "home session is gone" in b for b in bodies)
