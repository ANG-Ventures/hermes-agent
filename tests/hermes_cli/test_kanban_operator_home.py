"""Operator-homed cards (t_09fea045).

Cron/script minters have no chat session. Their cards used to be born
``unhomed``, which is foreign to every session, so nobody could unblock or
complete them. They now carry the fleet operator pseudo-session
``operator:apollo``, which any operator-profile session owns, and
``hermes kanban create`` refuses to mint an unhomed card implicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

APOLLO_SID = "20260929_180000_apollo"
OTHER_SID = "20260929_190000_other"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    kb.init_db()
    return home


def _operator_card(conn):
    tid = kb.create_task(
        conn, title="rebase X", assignee="human:apollo",
        session_id=kb.OPERATOR_HOME_SESSION, session_explicit=True,
    )
    assert kb.block_task(conn, tid, reason="needs a rebase")
    return tid


def test_operator_home_predicate():
    assert kb.OPERATOR_HOME_SESSION == "operator:apollo"
    assert kb.is_operator_home("operator:apollo")
    assert kb.is_operator_home(" operator:aegis ")
    for sid in (None, "", "operator:", "unhomed", APOLLO_SID):
        assert not kb.is_operator_home(sid)


@pytest.mark.parametrize("profile", sorted(kb.OPERATOR_PROFILES))
def test_any_operator_profile_session_drives_operator_card(kanban_home, profile):
    with kb.connect_closing() as conn:
        tid = _operator_card(conn)
        with kb.mutation_actor(session_ids=(APOLLO_SID,), profile=profile):
            assert kb.unblock_task(conn, tid)
            assert kb.complete_task(conn, tid, result="rebased")
        assert kb.get_task(conn, tid).status == "done"
        # Owned outright: no takeover, no re-home.
        assert kb.get_task(conn, tid).session_id == kb.OPERATOR_HOME_SESSION


@pytest.mark.parametrize("mutate", [
    lambda c, t: kb.unblock_task(c, t),
    lambda c, t: kb.complete_task(c, t, result="x"),
    lambda c, t: kb.archive_task(c, t),
    lambda c, t: kb.triage_resolve_task(c, t, to="todo", reason="r"),
])
def test_non_operator_session_still_refused(kanban_home, mutate):
    with kb.connect_closing() as conn:
        tid = _operator_card(conn)
        with kb.mutation_actor(session_ids=(OTHER_SID,), profile="daedalus"):
            with pytest.raises(kb.ForeignSessionMutationError):
                mutate(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


def test_unhomed_card_stays_foreign_to_operator(kanban_home):
    """Only the operator pseudo-session is shared; ``unhomed`` is unchanged."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy cron card", assignee="human:apollo")
        assert kb.get_task(conn, tid).unhomed
        assert kb.block_task(conn, tid, reason="x")
        with kb.mutation_actor(session_ids=(APOLLO_SID,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError, match="UNHOMED"):
                kb.unblock_task(conn, tid)


def test_operator_origin_line(kanban_home):
    with kb.connect_closing() as conn:
        tid = _operator_card(conn)
        assert kb.get_task(conn, tid).body.startswith("origin: operator (operator:apollo")


def test_child_of_operator_card_inherits_operator_home(kanban_home):
    with kb.connect_closing() as conn:
        parent = _operator_card(conn)
        child = kb.create_task(conn, title="child", parents=(parent,))
        assert kb.get_task(conn, child).session_id == kb.OPERATOR_HOME_SESSION


# --- CLI create -----------------------------------------------------------


def test_cli_create_refuses_implicit_unhomed(kanban_home, capsys):
    """RED before t_09fea045: this minted an undrivable ``unhomed`` card."""
    out = kc.run_slash("create 'cron card' --assignee human:apollo --json")
    assert "refused create" in out and "--home operator" in out
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_cli_create_home_operator(kanban_home):
    created = json.loads(kc.run_slash("create 'cron card' --home operator --json"))
    assert created["session_id"] == kb.OPERATOR_HOME_SESSION
    named = json.loads(kc.run_slash("create 'aegis card' --home operator:aegis --json"))
    assert named["session_id"] == "operator:aegis"


def test_cli_create_home_rejects_non_operator_and_session_combo(kanban_home):
    assert "expected 'operator'" in kc.run_slash("create 'x' --home apollo")
    assert "mutually exclusive" in kc.run_slash(
        f"create 'x' --home operator --session {APOLLO_SID}")
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_cli_create_explicit_session_or_env_needs_no_home(kanban_home, monkeypatch):
    explicit = json.loads(kc.run_slash(f"create 'x' --session {APOLLO_SID} --json"))
    assert explicit["session_id"] == APOLLO_SID
    # ``--session none`` is a deliberate, explicit opt-out: still allowed.
    none = json.loads(kc.run_slash("create 'y' --session none --json"))
    assert none["session_id"] is None
    monkeypatch.setenv("HERMES_SESSION_ID", APOLLO_SID)
    env = json.loads(kc.run_slash("create 'z' --json"))
    assert env["session_id"] == APOLLO_SID


def test_cli_create_under_homed_parent_needs_no_home(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="p", session_id=APOLLO_SID)
    child = json.loads(kc.run_slash(f"create 'c' --parent {parent} --json"))
    assert child["session_id"] == APOLLO_SID


def test_cli_create_in_worker_run_keeps_execution_lane(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        worker_card = kb.create_task(conn, title="w", session_id=None,
                                     session_explicit=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", worker_card)
    created = json.loads(kc.run_slash("create 'fanout' --json"))
    assert created["session_id"] is None  # unhomed, as before


def test_library_create_default_is_unchanged(kanban_home):
    """``require_home`` is a CLI policy; library callers keep the old default."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="lib")
        assert kb.get_task(conn, tid).unhomed
