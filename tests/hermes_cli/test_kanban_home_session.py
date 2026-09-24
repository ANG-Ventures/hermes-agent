"""Home-session card ownership: CLI stamping, --home, and the foreign-session
mutation guard (kanban_db.check_home_session / _home_session_guarded)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

HOME = "20260922_000000_home"
OTHER = "20260922_111111_other"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatched worker's own env would trip the execution-lane exemptions.
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    kb.init_db()
    return home


def _card(conn, *, session_id=HOME, assignee="worker-a", blocked=True):
    tid = kb.create_task(conn, title="card", assignee=assignee, session_id=session_id)
    if blocked:
        assert kb.block_task(conn, tid, reason="needs input")
    return tid


def _comments(conn, tid):
    return [c.body for c in kb.list_comments(conn, tid)]


# --- guard: db layer ------------------------------------------------------


def test_no_actor_is_execution_lane_and_never_guarded(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert kb.unblock_task(conn, tid)  # dispatcher/library path: allowed


def test_foreign_session_mutation_is_refused(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                kb.unblock_task(conn, tid)
        msg = str(exc.value)
        assert HOME in msg
        assert f"comment instead: hermes kanban comment {tid}" in msg
        assert kb.get_task(conn, tid).status == "blocked"
        assert _comments(conn, tid) == []


@pytest.mark.parametrize("mutate", [
    lambda c, t: kb.complete_task(c, t, result="x"),
    lambda c, t: kb.archive_task(c, t),
    lambda c, t: kb.assign_task(c, t, "someone-else"),
    lambda c, t: kb.set_task_model(c, t, "some-model"),
    lambda c, t: kb.set_task_session(c, t, "hijack"),
    lambda c, t: kb.triage_resolve_task(c, t, to="todo", reason="r"),
])
def test_every_guarded_mutator_refuses_foreign(kanban_home, mutate):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        before = kb.get_task(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError):
                mutate(conn, tid)
        after = kb.get_task(conn, tid)
        assert (after.status, after.assignee, after.session_id) == (
            before.status, before.assignee, before.session_id)


def test_home_session_is_allowed(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(HOME,), profile="apollo"):
            assert kb.unblock_task(conn, tid)
        assert _comments(conn, tid) == []


def test_assignee_is_exempt_wherever_card_was_born(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee="worker-a")
        with kb.mutation_actor(session_ids=(OTHER,), profile="worker-a"):
            assert kb.unblock_task(conn, tid)


def test_dispatched_worker_owns_its_own_card(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="argus"):
            assert kb.unblock_task(conn, tid)


def test_compaction_lineage_keeps_ownership(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_caller_session_lineage",
                        lambda sid: (HOME, sid) if sid == OTHER else ())
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            assert kb.unblock_task(conn, tid)


def test_unstamped_legacy_card_is_allowed(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=None)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            assert kb.unblock_task(conn, tid)


def test_foreign_ok_allows_and_leaves_audit_comment(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="home session is gone"):
            assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status != "blocked"
        assert _comments(conn, tid) == [
            f"foreign-session action by {OTHER} (apollo): "
            "home session is gone [unblock]"
        ]


def test_foreign_ok_on_failed_mutation_leaves_no_comment(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, blocked=False)  # not blocked -> unblock returns False
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="why"):
            assert not kb.unblock_task(conn, tid)
        assert _comments(conn, tid) == []


def test_batch_set_model_refuses_whole_batch(kanban_home):
    with kb.connect_closing() as conn:
        mine = _card(conn, session_id=OTHER, blocked=False)
        foreign = _card(conn, blocked=False)
        writes = [kb.BatchRouteWrite(task_id=t, touch_model=True, model="m-x")
                  for t in (mine, foreign)]
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.apply_batch_route_writes(conn, writes)
        assert kb.get_task(conn, mine).model_override is None


# --- CLI ------------------------------------------------------------------


def test_cli_create_stamps_env_session_by_default(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    created = json.loads(kc.run_slash("create 'x' --assignee a --json"))
    assert created["session_id"] == HOME
    explicit = json.loads(kc.run_slash(f"create 'y' --session {OTHER} --json"))
    assert explicit["session_id"] == OTHER
    unstamped = json.loads(kc.run_slash("create 'z' --session none --json"))
    assert unstamped["session_id"] is None


def test_cli_create_without_env_stays_unstamped(kanban_home):
    created = json.loads(kc.run_slash("create 'x' --json"))
    assert created["session_id"] is None


def test_cli_list_home(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        mine = _card(conn, session_id=HOME, blocked=False)
        theirs = _card(conn, session_id=OTHER, blocked=False)
    assert "HERMES_SESSION_ID" in kc.run_slash("list --home")
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    ids = {t["id"] for t in json.loads(kc.run_slash("list --home --json"))}
    assert mine in ids and theirs not in ids


def test_cli_show_prints_home(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    with kb.connect_closing() as conn:
        mine = _card(conn, session_id=HOME, blocked=False)
        theirs = _card(conn, session_id=OTHER, blocked=False)
        legacy = _card(conn, session_id=None, blocked=False)
    assert "home:      this-session" in kc.run_slash(f"show {mine}")
    out = kc.run_slash(f"show {theirs}")
    assert f"session:   {OTHER}" in out
    assert f"home:      other ({OTHER})" in out
    assert "home:      unstamped" in kc.run_slash(f"show {legacy}")


def test_cli_update_restamps_session(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME, blocked=False)
    kc.run_slash(f"update {tid} --session {OTHER}")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id == OTHER


def test_cli_foreign_complete_refused_then_override(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME, blocked=False)
    out = kc.run_slash(f"complete {tid} --result done")
    assert "refused complete" in out and HOME in out
    assert "comment instead" in out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"
    kc.run_slash(f"complete {tid} --result done --foreign-ok 'home session closed'")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
        assert any(
            b.startswith(f"foreign-session action by {OTHER} (apollo): home session closed")
            for b in _comments(conn, tid)
        )


def test_cli_comment_on_foreign_card_is_unaffected(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME, blocked=False)
    kc.run_slash(f"comment {tid} 'fyi from another session'")
    with kb.connect_closing() as conn:
        assert "fyi from another session" in _comments(conn, tid)


def test_cli_legacy_card_warns_once(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    with kb.connect_closing() as conn:
        a = _card(conn, session_id=None)
        b = _card(conn, session_id=None)
    out = kc.run_slash(f"unblock {a} {b}")
    assert out.count("has no home session") == 1


# --- tool surface -----------------------------------------------------------


def test_tool_unblock_guard_and_foreign_ok(kanban_home, monkeypatch):
    from tools import kanban_tools as kt
    from tools.registry import registry

    monkeypatch.setattr(kt, "_require_orchestrator_tool", lambda name: None)
    monkeypatch.setattr(kt, "_current_session_id", lambda: OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    handler = registry.get_entry("kanban_unblock").handler
    assert "foreign_ok" in kt.KANBAN_UNBLOCK_SCHEMA["parameters"]["properties"]
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME)

    refused = json.loads(handler({"task_id": tid}))
    assert "error" in refused and HOME in refused["error"]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"

    ok = json.loads(handler({"task_id": tid, "foreign_ok": "home is dead"}))
    assert ok.get("ok") or ok.get("success") or ok.get("task_id") == tid
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "blocked"
        assert any("home is dead [unblock]" in b for b in _comments(conn, tid))
