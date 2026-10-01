"""t_4b826d5c: a foreign-session status/timing override (``--operator`` /
``--takeover``) announces itself (FOREIGN CHANGE comment + ``foreign_change``
event) and cannot overturn a NEWER human ruling the home session holds.

Incident 2026-09-30 21:58: one operator session re-scheduled another session's
cards to 10-04 against Ace's later ruling, and the home session found out by
accident 10 minutes later."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BASE_PATH = Path(__file__).with_name("test_kanban_home_session.py")
_spec = importlib.util.spec_from_file_location("_home_session_base", _BASE_PATH)
base = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("_home_session_base", base)
_spec.loader.exec_module(base)

kb = base.kb
kc = base.kc
HOME, OTHER = base.HOME, base.OTHER
kanban_home = base.kanban_home  # re-export the fixture

OLD_MSG = "1555000000000000000"
NEW_MSG = "1555251991385743474"
NEWER_MSG = "1555300000000000000"


def _card(conn, **kw):
    return base._card(conn, **kw)


def _comments(conn, tid):
    return [c.body for c in kb.list_comments(conn, tid)]


def _foreign_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "foreign_change"]


def _home_ruling(conn, tid, msg=NEW_MSG, session=HOME):
    kb.add_comment(
        conn, tid, author="apollo",
        body=f"APOLLO: ACE ruled run phase 4 now (msg {msg})",
        session_ref=kb.derive_session_ref(session),
    )


# --- parsing --------------------------------------------------------------


def test_ruling_msg_id_parses_standard_form():
    assert kb.ruling_msg_id(f"Ace in #cc-native: park it (msg {NEW_MSG})") == int(NEW_MSG)
    assert kb.ruling_msg_id(f"msg {OLD_MSG} then msg {NEW_MSG}") == int(NEW_MSG)
    assert kb.ruling_msg_id("Ace: park it (msg 12345)") is None  # too short
    assert kb.ruling_msg_id(f"message{NEW_MSG}") is None
    assert kb.ruling_msg_id(None) is None


# --- 1. announce ------------------------------------------------------------


def test_operator_unblock_posts_foreign_change_comment_and_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator="Ace via Apollo: run it"):
            assert kb.unblock_task(conn, tid)
        assert _comments(conn, tid) == [
            f"FOREIGN CHANGE by {OTHER} (Ace via Apollo: run it) [unblock]"
        ]
        ev = _foreign_events(conn, tid)
        assert len(ev) == 1
        assert ev[0].payload["action"] == "unblock"
        assert ev[0].payload["via"] == "--operator"
        assert ev[0].payload["home"] == HOME
        assert ev[0].payload["by_sessions"] == [OTHER]
        assert ev[0].payload["cited_msg"] is None


def test_takeover_schedule_posts_foreign_change(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, blocked=False)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok=f"Ace: park to 10-04 (msg {NEW_MSG})"):
            assert kb.schedule_task(conn, tid, reason="10-04")
        bodies = _comments(conn, tid)
        assert (f"FOREIGN CHANGE by {OTHER} (Ace: park to 10-04 (msg {NEW_MSG})) "
                f"[schedule]") in bodies
        ev = _foreign_events(conn, tid)
        assert len(ev) == 1
        assert ev[0].payload["via"] == "--takeover"
        assert ev[0].payload["cited_msg"] == NEW_MSG


def test_non_status_verb_is_not_announced(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _home_ruling(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="one-off model pin"):
            assert kb.set_task_model(conn, tid, "m")
        assert _foreign_events(conn, tid) == []
        assert not [b for b in _comments(conn, tid) if b.startswith("FOREIGN CHANGE")]


def test_home_session_change_is_not_announced(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(HOME,), profile="apollo"):
            assert kb.unblock_task(conn, tid)
        assert _foreign_events(conn, tid) == []


# --- 2. ruling precedence ---------------------------------------------------


@pytest.mark.parametrize("reason", [
    f"Ace via Apollo #kanban-cc: park to 10-04 (msg {OLD_MSG})",  # older id
    "Ace via Apollo #kanban-cc: park to 10-04",                    # no id
])
def test_older_or_uncited_operator_ruling_is_refused(kanban_home, reason):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _home_ruling(conn, tid)
        before = _comments(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator=reason):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                kb.unblock_task(conn, tid)
        msg = str(exc.value)
        assert (f"home session holds a newer human ruling (msg {NEW_MSG}); "
                f"re-home with --takeover or ask in ") in msg
        assert kb.get_task(conn, tid).status == "blocked"
        assert _comments(conn, tid) == before
        assert _foreign_events(conn, tid) == []


def test_takeover_with_older_ruling_is_refused_too(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, blocked=False)
        _home_ruling(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok=f"Ace: park (msg {OLD_MSG})"):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                kb.schedule_task(conn, tid, reason="10-04")
        assert f"(msg {NEW_MSG})" in str(exc.value)
        assert kb.get_task(conn, tid).status != "scheduled"


@pytest.mark.parametrize("cited", [NEW_MSG, NEWER_MSG])
def test_same_or_newer_cited_ruling_is_allowed_and_announced(kanban_home, cited):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _home_ruling(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator=f"Ace via Apollo: changed mind (msg {cited})"):
            assert kb.unblock_task(conn, tid)
        assert len(_foreign_events(conn, tid)) == 1


def test_home_operator_override_event_counts_as_ruling(kanban_home):
    # The home session relayed its ruling on ANOTHER card via --operator; on
    # this card its own operator_override event carries the id.
    with kb.connect_closing() as conn:
        tid = _card(conn)
        kb._append_event(conn, tid, "operator_override", {
            "action": "unblock", "reason": f"Ace: keep blocked (msg {NEW_MSG})",
            "by_sessions": [HOME], "by_profile": "apollo", "home": HOME,
        })
        conn.commit()
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator=f"Ace: run (msg {OLD_MSG})"):
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.unblock_task(conn, tid)


def test_foreign_sessions_ruling_comment_does_not_count(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _home_ruling(conn, tid, session="20260922_222222_third")
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator="Ace via Apollo: run it"):
            assert kb.unblock_task(conn, tid)


def test_home_comment_without_ruling_shape_does_not_count(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        kb.add_comment(conn, tid, author="apollo",
                       body=f"progress note, see msg {NEW_MSG}",
                       session_ref=kb.derive_session_ref(HOME))
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               operator="Ace via Apollo: run it"):
            assert kb.unblock_task(conn, tid)

