"""Home-session card ownership: CLI stamping, --home, and the foreign-session
mutation guard (kanban_db.check_home_session / _home_session_guarded)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from tests.kanban_review_helpers import covered_request_changes

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
    # A PRE-EXISTING NULL row (created before birth-stamping); create_task
    # itself can no longer produce one.
    with kb.connect_closing() as conn:
        tid = _card(conn)
        conn.execute("UPDATE tasks SET session_id = NULL WHERE id = ?", (tid,))
        conn.commit()
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            assert kb.unblock_task(conn, tid)


def test_sessionless_create_is_born_unhomed_never_null(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cron card", assignee="worker-a")
        raw = conn.execute(
            "SELECT session_id, body FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert raw["session_id"] == kb.UNHOMED_SESSION
        assert raw["body"].startswith("origin: unhomed")
        task = kb.get_task(conn, tid)
        # Sentinel never leaks to notification/wake consumers.
        assert task.session_id is None and task.unhomed is True


def test_child_inherits_parent_home(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root", assignee="worker-a", session_id=HOME)
        kid = kb.create_task(conn, title="kid", assignee="worker-b", parents=(root,))
        assert kb.get_task(conn, kid).session_id == HOME


def test_explicit_session_stamps_origin_line_once(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", body="do it", session_id=HOME)
        body = kb.get_task(conn, tid).body
        assert body.splitlines()[0].startswith("origin: ")
        assert f"session {HOME}" in body.splitlines()[0]
        assert body.endswith("do it")
        pre = kb.create_task(conn, title="t2", body="origin: x\nbody", session_id=HOME)
        assert kb.get_task(conn, pre).body == "origin: x\nbody"


def test_unhomed_card_refused_for_chat_session_and_takeover_records_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cron card", assignee="worker-a")
        assert kb.block_task(conn, tid, reason="x")
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError, match="UNHOMED"):
                kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="adopting orphan"):
            assert kb.unblock_task(conn, tid)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
        assert "takeover" in kinds
        assert any("takeover:" in c and "adopting orphan" in c
                   for c in _comments(conn, tid))


def test_foreign_ok_allows_and_leaves_audit_comment(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="home session is gone"):
            assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status != "blocked"
        assert _comments(conn, tid) == [
            f"takeover: foreign-session action by {OTHER} (apollo): "
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
        legacy = _card(conn, blocked=False)
        conn.execute("UPDATE tasks SET session_id = NULL WHERE id = ?", (legacy,))
        conn.commit()
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
            b.startswith(f"takeover: foreign-session action by {OTHER} (apollo): home session closed")
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
        a = _card(conn)
        b = _card(conn)
        # Legacy pre-stamp NULL rows (create_task can no longer produce them).
        conn.execute("UPDATE tasks SET session_id = NULL WHERE id IN (?, ?)", (a, b))
        conn.commit()
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


# --- round 2: sessionless callers, slash path, every status writer ---------


def test_sessionless_actor_is_never_refused(kanban_home):
    """No chat-session identity (cron opener, plain shell) is session-vs-
    session undefined: allowed, exactly as without the guard."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(), profile="apollo"):
            assert kb.unblock_task(conn, tid)
        assert _comments(conn, tid) == []


def test_cli_sessionless_opener_shape_matches_base(kanban_home, capsys):
    """The live cron-opener shape (comment, then unblock) on a stamped,
    blocked card whose assignee is not the caller lands in ready."""
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME, assignee="daedalus")
    assert kc.kanban_command(kc.build_parser(_subparsers()).parse_args(
        ["comment", tid, "opener: waking"])) == 0
    rc = kc.kanban_command(kc.build_parser(_subparsers()).parse_args(
        ["unblock", tid]))
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "home-session guard skipped" in err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert "opener: waking" in _comments(conn, tid)


def _subparsers():
    import argparse

    return argparse.ArgumentParser().add_subparsers(dest="_top")


def _ready(conn, **kw):
    return _card(conn, blocked=False, **kw)


def _done(conn, **kw):
    tid = _ready(conn, **kw)
    assert kb.complete_task(conn, tid, result="x")
    return tid


def _review(conn, **kw):
    tid = _ready(conn, **kw)
    assert kb.request_review(conn, tid, summary="s", reviewer="argus", force=True)
    return tid


def _review_run(conn, **kw):
    """A card in an ACTIVE review run: request_changes' only legal state."""
    tid = _ready(conn, **kw)
    worker = kb.claim_task(conn, tid)
    assert worker is not None
    assert kb.request_review(conn, tid, reviewer="argus",
                             expected_run_id=worker.current_run_id)
    assert kb.claim_review_task(conn, tid) is not None
    return tid


def _triage(conn, **kw):
    return kb.create_task(conn, title="t", triage=True, session_id=HOME, **kw)


def _link(conn, tid):
    parent = kb.create_task(conn, title="p", assignee="x", session_id=OTHER)
    kb.link_tasks(conn, parent, tid)


# (setup, mutation) for every newly guarded status writer. Each mutation is
# first proven to CHANGE the card with no actor bound, so a refusal is real.
_ROUND2 = {
    "schedule": (_ready, lambda c, t: kb.schedule_task(c, t, reason="later")),
    "reopen": (_done, lambda c, t: kb.reopen_task(c, t, actor="apollo", reason="r")),
    "request-review": (_ready, lambda c, t: kb.request_review(
        c, t, summary="s", reviewer="argus", force=True)),
    "request-changes": (_review_run, lambda c, t: covered_request_changes(c, t, reason="fix")),
    "link": (_ready, _link),
    "specify": (_triage, lambda c, t: kb.specify_triage_task(c, t, body="spec")),
    "decompose": (_triage, lambda c, t: kb.decompose_triage_task(
        c, t, root_assignee="worker-a",
        children=[{"title": "c1", "assignee": "worker-b"}])),
}


def _state(conn, tid):
    t = kb.get_task(conn, tid)
    return (t.status, t.assignee, t.session_id)


@pytest.mark.parametrize("verb", sorted(_ROUND2))
def test_round2_writers_change_state_unguarded(kanban_home, verb):
    setup, mutate = _ROUND2[verb]
    with kb.connect_closing() as conn:
        tid = setup(conn)
        before = _state(conn, tid)
        mutate(conn, tid)
        assert _state(conn, tid) != before, f"{verb} is not a status writer here"


@pytest.mark.parametrize("verb", sorted(_ROUND2))
def test_round2_writers_refuse_foreign_session(kanban_home, verb):
    setup, mutate = _ROUND2[verb]
    with kb.connect_closing() as conn:
        tid = setup(conn)
        before = _state(conn, tid)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                mutate(conn, tid)
        assert f"refused {verb} on {tid}" in str(exc.value)
        assert _state(conn, tid) == before


@pytest.mark.parametrize("verb", sorted(_ROUND2))
def test_round2_writers_allow_home_and_override(kanban_home, verb):
    setup, mutate = _ROUND2[verb]
    with kb.connect_closing() as conn:
        mine = setup(conn)
        before = _state(conn, mine)
        with kb.mutation_actor(session_ids=(HOME,), profile="apollo"):
            mutate(conn, mine)
        assert _state(conn, mine) != before
        theirs = setup(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="home is gone"):
            mutate(conn, theirs)
        assert any(b.endswith(f"home is gone [{verb}]") for b in _comments(conn, theirs))


# --- gateway /kanban slash path ------------------------------------------


def _slash_runner(session_id):
    from gateway.run import GatewayRunner
    from types import SimpleNamespace

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:c1"
    # The runner's REAL async_session_store property wraps this raw store in
    # gateway.session.AsyncSessionStore, so the method the handler awaits is
    # the one the facade actually offloads. The raw lookup asserts it runs
    # OFF the event loop (a sync store call on the loop fails the test).
    import asyncio

    def _entry_for(key):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("raw session_store.entry_for ran on the event loop")
        if isinstance(session_id, BaseException):
            raise session_id
        return SimpleNamespace(session_id=session_id)

    runner.session_store = SimpleNamespace(entry_for=_entry_for)
    return runner


def _slash_event(text):
    from types import SimpleNamespace

    return SimpleNamespace(text=text, source=SimpleNamespace(), message_id="1",
                           reply_to_message_id=None)


@pytest.mark.asyncio
async def test_gateway_slash_binds_invoking_session(kanban_home, monkeypatch):
    """In-process /kanban: env is another session's (process-global), the
    invoking session comes from the gateway explicitly."""
    from gateway.run import GatewayRunner

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", "poisoned-global-env")
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    home_runner = _slash_runner(HOME)
    out = await GatewayRunner._handle_kanban_command(
        home_runner, _slash_event("/kanban create 'mine' --assignee worker-a --json"))
    tid = json.loads(out)["id"]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id == HOME

    other_runner = _slash_runner(OTHER)
    out = await GatewayRunner._handle_kanban_command(
        other_runner, _slash_event(f"/kanban block {tid} 'nope'"))
    assert f"refused block on {tid}" in out and HOME in out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "blocked"

    out = await GatewayRunner._handle_kanban_command(
        home_runner, _slash_event(f"/kanban block {tid} 'mine to block'"))
    assert "refused" not in out, out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.asyncio
async def test_gateway_slash_session_resolution_failure_is_logged(
        kanban_home, monkeypatch, caplog):
    """A failed lookup degrades to sessionless -- but never silently."""
    import logging
    from gateway.run import GatewayRunner

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    runner = _slash_runner(RuntimeError("store offline"))
    with caplog.at_level(logging.WARNING):
        out = await GatewayRunner._handle_kanban_command(
            runner, _slash_event("/kanban create 'x' --json"))
    tid = json.loads(out)["id"]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id is None
    assert any("could not resolve invoking session" in r.getMessage()
               and "store offline" in r.getMessage() for r in caplog.records)


# --- claim: chat-reachable CLI verb, so guarded ---------------------------


def test_foreign_claim_is_refused(kanban_home):
    with kb.connect_closing() as conn:
        tid = _ready(conn)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                kb.claim_task(conn, tid)
        assert f"refused claim on {tid}" in str(exc.value)
        t = kb.get_task(conn, tid)
        assert (t.status, t.claim_lock) == ("ready", None)


def test_cli_foreign_claim_is_refused(kanban_home, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        tid = _ready(conn)
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    rc = kc.kanban_command(kc.build_parser(_subparsers()).parse_args(
        ["claim", tid]))
    assert rc == 1
    assert f"refused claim on {tid}" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_cli_foreign_requeue_refused_then_override(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _ready(conn)
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    out = kc.run_slash(f"requeue {tid} 'run now'")
    assert f"refused requeue on {tid}" in out and HOME in out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "requeued"]
        assert _comments(conn, tid) == []
    kc.run_slash(f"requeue {tid} 'run now' --foreign-ok 'recovery'")
    with kb.connect_closing() as conn:
        assert [e for e in kb.list_events(conn, tid) if e.kind == "requeued"]
        assert _comments(conn, tid) == [
            f"takeover: foreign-session action by {OTHER} (apollo): recovery [requeue]"
        ]


def test_cli_assignee_can_requeue_foreign_home(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _ready(conn)
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "worker-a")
    assert "Requeued" in kc.run_slash(f"requeue {tid} 'assigned work'")
    with kb.connect_closing() as conn:
        assert [e for e in kb.list_events(conn, tid) if e.kind == "requeued"]
        assert _comments(conn, tid) == []


def test_claim_allowed_for_home_session_and_assignee(kanban_home):
    with kb.connect_closing() as conn:
        mine = _ready(conn)
        with kb.mutation_actor(session_ids=(HOME,), profile="apollo"):
            assert kb.claim_task(conn, mine) is not None
        assert kb.get_task(conn, mine).status == "running"
        theirs = _ready(conn, assignee="worker-a")
        with kb.mutation_actor(session_ids=(OTHER,), profile="worker-a"):
            assert kb.claim_task(conn, theirs) is not None
        assert kb.get_task(conn, theirs).status == "running"


def test_dispatcher_tick_still_claims_stamped_card(kanban_home, monkeypatch):
    """A real dispatch_once tick claims a card stamped by some chat session:
    the dispatcher binds no actor, so the claim guard is inert there."""
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    spawned = []
    with kb.connect_closing() as conn:
        tid = _ready(conn, session_id=OTHER)
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert kb.get_task(conn, tid).status == "running"
    assert spawned == [tid]


def test_run_slash_session_does_not_leak(kanban_home):
    kc.run_slash("list", session_id=HOME)
    assert kc._SLASH_SESSION_ID.get() is None


# --- contract: ONE choke point -------------------------------------------

import ast as _ast
import re as _re

# Writers of tasks.status/assignee/priority/session_id or dispatch-intent
# events that are NOT guarded, each with the reason it is execution lane.
EXECUTION_LANE = {
    "_migrate_add_optional_columns": "schema migration at connect time",
    "backfill_unhomed": (
        "callers: `kanban home-lint --backfill` (operator/cron). Writes ONLY "
        "rows whose session_id IS NULL/empty (WHERE-clause re-checked in the "
        "UPDATE), i.e. cards with no home to trespass on; never reassigns a "
        "homed card"
    ),
    # Reasons are derived from the CALLERS, not the function's intent. The
    # chat-reachable ones (CLI verb / tool / dashboard) say why a foreign
    # session still cannot change another card's status or ownership.
    "recompute_ready": (
        "callers: CLI list (kanban.py), kanban_list tool, dashboard, "
        "dispatcher. Board-wide todo->ready cascade with no task argument and "
        "no per-card choice: it only promotes cards whose parents are ALL "
        "done, the same result the next dispatcher tick produces"),
    "claim_review_task": (
        "callers: dispatcher only (no CLI verb, tool or slash path)"),
    "heartbeat_claim": (
        "callers: kanban_heartbeat tool + worker auto-heartbeat. Writes only "
        "claim_expires (never status/assignee/priority; the regex hit is the "
        "WHERE status='running'), and only when claim_lock == the caller's "
        "own lock, so a foreign process matches no row"),
    "heartbeat_worker": (
        "callers: CLI heartbeat, kanban_heartbeat tool, auto-heartbeat. Writes "
        "only last_heartbeat_at + a heartbeat event on an already-running "
        "card (the regex hit is the WHERE status='running'); it cannot "
        "change status, assignee, priority or the claim"),
    "release_stale_claims": "reaper",
    "invalidate_descendants_for_parent_reopen": "cascade of a (guarded) reopen",
    "_release_claim_for_workspace_refusal": "dispatcher spawn refusal",
    "_refuse_reclaim_unproven_death": "reaper",
    "_defer_reclaim_for_live_worker": "reaper",
    "enforce_max_runtime": "reaper",
    "detect_progress_stalls": "reaper",
    "detect_stale_running": "reaper",
    "reconcile_orphaned_running": "reaper",
    "detect_crashed_workers": "reaper",
    "_record_task_failure": "dispatcher failure accounting",
    "_dispatch_once_locked": "the dispatcher",
}

_STATIC_WRITE = _re.compile(
    r"UPDATE\s+tasks\s+SET[^;]*?\b(status|assignee|priority|session_id)\s*=",
    _re.S | _re.I)
_DYNAMIC_WRITE = _re.compile(r"[\"'](status|assignee|priority|session_id)\s*=\s*\?")


def _module_src(mod):
    return Path(mod.__file__).read_text(encoding="utf-8")


def _writers():
    src = _module_src(kb)
    out = {}
    for node in _ast.parse(src).body:
        if not isinstance(node, _ast.FunctionDef):
            continue
        seg = _ast.get_source_segment(src, node) or ""
        dispatch_intent = any(
            isinstance(call, _ast.Call)
            and isinstance(call.func, _ast.Name)
            and call.func.id == "_append_event"
            and len(call.args) > 2
            and isinstance(call.args[2], _ast.Constant)
            and call.args[2].value in kb._RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS
            for call in _ast.walk(node)
        )
        if dispatch_intent or _STATIC_WRITE.search(seg) or (
            "UPDATE tasks" in seg and _DYNAMIC_WRITE.search(seg)
        ):
            out[node.name] = node
    return out


def _is_guarded(node):
    return any(
        isinstance(d, _ast.Call) and getattr(d.func, "id", "") == "_home_session_guarded"
        for d in node.decorator_list
    )


def test_every_status_writer_goes_through_the_guard():
    writers = _writers()
    assert len(writers) > 10, "writer scan found nothing: the scanner is broken"
    unrouted = sorted(
        n for n, node in writers.items()
        if not _is_guarded(node) and n not in EXECUTION_LANE
    )
    assert not unrouted, (
        f"status/assignee/priority/session_id or dispatch-intent writers without "
        f"@_home_session_guarded: {unrouted}. Guard them, or add them to "
        f"EXECUTION_LANE with the reason no chat surface can reach them."
    )
    stale = sorted(set(EXECUTION_LANE) - set(writers))
    assert not stale, f"EXECUTION_LANE lists non-writers: {stale}"
    both = sorted(n for n in EXECUTION_LANE if _is_guarded(writers[n]))
    assert not both, f"guarded AND listed as execution lane: {both}"


def test_every_cli_verb_reaching_a_guarded_writer_binds_the_actor():
    """An unbound actor makes the guard inert, so a verb that reaches a
    guarded writer but is missing from _HOME_GUARDED_ACTIONS bypasses it."""
    guarded = {n for n, node in _writers().items() if _is_guarded(node)}
    guarded |= {
        name for name, fn in vars(kb).items()
        if callable(fn) and getattr(fn, "__home_session_action__", None)
    }
    src = _module_src(kc)
    tree = _ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, _ast.FunctionDef)}
    table = _re.search(r"handlers = \{(.*?)\}", src, _re.S).group(1)
    verbs = dict(_re.findall(r'"([\w-]+)":\s*(_cmd_\w+)', table))

    def reach(fn, seen):
        hits = set()
        for node in _ast.walk(fn):
            if not isinstance(node, _ast.Call):
                continue
            f = node.func
            if isinstance(f, _ast.Attribute) and getattr(f.value, "id", "") == "kb":
                hits.add(f.attr)
            elif isinstance(f, _ast.Name) and f.id in funcs and f.id not in seen:
                seen.add(f.id)
                hits |= reach(funcs[f.id], seen)
        return hits

    missing = sorted(
        verb for verb, cmd in verbs.items()
        if reach(funcs[cmd], {cmd}) & guarded and verb not in kc._HOME_GUARDED_ACTIONS
    )
    assert not missing, f"CLI verbs reaching guarded writers unbound: {missing}"


def test_every_tool_reaching_a_guarded_writer_binds_the_actor():
    from tools import kanban_tools as kt

    guarded = {
        name for name, fn in vars(kb).items()
        if callable(fn) and getattr(fn, "__home_session_action__", None)
    }
    src = _module_src(kt)
    tree = _ast.parse(src)
    handlers = {n.name: n for n in tree.body if isinstance(n, _ast.FunctionDef)}
    wrapped = set(_re.findall(r"handler=_with_mutation_actor\((\w+)\)", src))
    registered = set(_re.findall(r"handler=(?:_with_mutation_actor\()?(\w+)", src))
    missing = []
    for name in sorted(registered):
        node = handlers.get(name)
        if node is None:
            continue
        calls = {
            c.func.attr for c in _ast.walk(node)
            if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute)
        }
        if calls & guarded and name not in wrapped:
            missing.append(name)
    assert not missing, f"tool handlers reaching guarded writers unbound: {missing}"


# --- t_4de62657: birth stamping, session-first list, --takeover, home-lint ---


def test_cli_create_stamps_session_and_origin_line(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_NAME", "#sub-vps-n")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "1550")
    out = kc.run_slash("create 'cli card' --body 'do the thing' --json")
    tid = json.loads(out[out.index("{"):])["id"]
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.session_id == HOME
    first = t.body.splitlines()[0]
    assert first.startswith("origin: discord #sub-vps-n (1550) \u00b7 session " + HOME)


def test_cli_create_without_session_is_unhomed(kanban_home):
    out = kc.run_slash("create 'cron card' --json")
    tid = json.loads(out[out.index("{"):])["id"]
    with kb.connect_closing() as conn:
        raw = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert raw["session_id"] == kb.UNHOMED_SESSION


def test_cli_takeover_flag_is_the_override(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME)
    assert "refused unblock" in kc.run_slash(f"unblock {tid}")
    kc.run_slash(f"unblock {tid} --takeover 'home chat is dead'")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "blocked"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "takeover"]
        assert len(ev) == 1 and ev[0].payload["reason"] == "home chat is dead"
        assert ev[0].payload["home"] == HOME
        assert ev[0].payload["by_sessions"] == [OTHER]


def test_cli_comment_on_foreign_card_always_allowed(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    with kb.connect_closing() as conn:
        tid = _card(conn, session_id=HOME)
    kc.run_slash(f"comment {tid} 'fyi from another chat'")
    with kb.connect_closing() as conn:
        assert "fyi from another chat" in _comments(conn, tid)


def test_cli_list_groups_this_session_first(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        mine = kb.create_task(conn, title="mine", assignee="w", session_id=HOME)
        theirs = kb.create_task(conn, title="theirs", assignee="w", session_id=OTHER)
        orphan = kb.create_task(conn, title="orphan", assignee="w")
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    out = kc.run_slash("list")
    this_at, other_at = out.index("THIS SESSION (1)"), out.index("OTHER SESSIONS (2)")
    assert this_at < out.index(mine) < other_at
    assert out.index(theirs) > other_at and out.index(orphan) > other_at
    assert f"  {theirs} \u00b7 ready \u00b7 theirs" in out
    assert f"  {orphan} \u00b7 ready \u00b7 orphan [unhomed]" in out
    flat = kc.run_slash("list --all")
    assert "THIS SESSION" not in flat and mine in flat and theirs in flat
    only = kc.run_slash("list --this-session")
    assert mine in only and theirs not in only and orphan not in only


def test_cli_list_without_session_is_flat(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w", session_id=HOME)
    out = kc.run_slash("list")
    assert tid in out and "THIS SESSION" not in out


def test_home_lint_silent_when_green_and_backfill(kanban_home, capsys):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="w", session_id=HOME)
        b = kb.create_task(conn, title="b", assignee="w", session_id=HOME)
        done = kb.create_task(conn, title="d", assignee="w", session_id=HOME)
        conn.execute("UPDATE tasks SET session_id = NULL WHERE id IN (?, ?)", (b, done))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (done,))
        conn.commit()
    args = argparse.Namespace(backfill=False, dry_run=False, json=False)
    assert kc._cmd_home_lint(args) == 1
    out = capsys.readouterr().out
    assert b in out and a not in out and done not in out
    assert kc._cmd_home_lint(argparse.Namespace(backfill=True, dry_run=True, json=False)) == 0
    with kb.connect_closing() as conn:
        assert kb.find_homeless_open_tasks(conn) == [b]
    capsys.readouterr()
    assert kc._cmd_home_lint(argparse.Namespace(backfill=True, dry_run=False, json=False)) == 0
    with kb.connect_closing() as conn:
        raw = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (b,)).fetchone()
        assert raw["session_id"] == kb.UNHOMED_SESSION
        assert any(c.startswith("origin: unknown (pre-stamp or cron)") for c in _comments(conn, b))
        assert kb.backfill_unhomed(conn) == []  # idempotent
    capsys.readouterr()
    assert kc._cmd_home_lint(argparse.Namespace(backfill=False, dry_run=False, json=False)) == 0
    assert capsys.readouterr().out == ""  # silent when green
