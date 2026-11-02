"""Default subscriptions and quiet, single-line boomerang handbacks."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as bw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn


@pytest.mark.parametrize("configured,explicit,worker,expected", [
    (None, None, False, True), (False, None, False, False),
    (True, False, False, False), (True, None, True, None),
    (False, True, True, True),
])
def test_create_boomerang_default(tmp_path, monkeypatch, configured, explicit, worker, expected):
    from tools import kanban_tools as kt
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["auto_subscribe_boomerang"] is True
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {} if configured is None else {"kanban": {"auto_subscribe_boomerang": configured}}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    assert kt._auto_boomerang(explicit, worker=worker, subscribed=True) is expected
    if explicit is None:
        assert kt._auto_boomerang(None, worker=worker, subscribed=False) is None


def test_create_cli_opt_out():
    import argparse
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["kanban", "create", "c", "--assignee", "peer", "--no-boomerang-wake"]).boomerang_wake is False
    assert parser.parse_args(["kanban", "create", "c", "--assignee", "peer"]).boomerang_wake is None


@pytest.mark.parametrize("surface,stale", [(False, False), (True, False), (True, True)])
def test_handback_shape(tmp_path, monkeypatch, surface, stale):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch", assignee="human:apollo")
    entry = dict(task_id=tid, board=None, row="review_deadman", delegation_id="d1", route={})
    sent = []
    async def send(chat_id, text, metadata=None):
        sent.append(text)
    runner = SimpleNamespace(_kanban_route_adapter=lambda *a: SimpleNamespace(send=send))
    result = dict(summary="CI is pending\nnot a page", action="surface" if surface else "comment_why_waits")
    assert asyncio.run(bw.report_handled(runner, entry, result, stale=stale)) is (not stale)
    assert asyncio.run(bw.report_handled(runner, entry, result, stale=stale)) is False
    assert sent == ([] if stale else [f"boomerang handled {tid} (review_deadman): CI is pending"])
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert len(comments) == 1
    assert comments[0].body.startswith("boomerang: review_deadman CI is pending")


@pytest.mark.parametrize("opt_out", [False, True])
def test_gateway_create_persists_level(tmp_path, monkeypatch, opt_out):
    from tools import kanban_tools as kt
    from gateway import session_context as sc
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(sc, "get_session_env", lambda k, d="": {
        "HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "test-chat",
        "HERMES_SESSION_CHAT_TYPE": "group"}.get(k, d))
    monkeypatch.setattr(kt, "_persisted_session_id", lambda value: "home")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    args = dict(board="default", title="gateway scratch", assignee="peer")
    if opt_out:
        args["boomerang_wake"] = False
    created = json.loads(kt._handle_create(args))
    assert created["ok"], created
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, created["task_id"]).boomerang_wake is (not opt_out)
        assert kbn.list_notify_subs(conn, created["task_id"])[0]["delivery_mode"] == "notify+wake"


@pytest.mark.parametrize("owner", [False, True])
def test_deadman_owner_comment_since_review(tmp_path, monkeypatch, owner):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="deadman", assignee="human:apollo")
        kb._append_event(conn, tid, "review_requested", {})
        kb.add_comment(conn, tid, "human:apollo" if owner else "worker", "CI pending")
        kb._append_event(conn, tid, "review_deadman", {})
        event_id = kb.max_event_id(conn)
    assert bw.deadman_owner_acted(dict(task_id=tid, row="review_deadman", event_id=event_id)) is owner


@pytest.mark.parametrize("stale", [False, True])
def test_run_entry_deadman_passive_delivery(tmp_path, monkeypatch, stale):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch live deadman", assignee="human:apollo")
        kb._append_event(conn, tid, "review_requested", {})
        if stale:
            kb.add_comment(conn, tid, "human:apollo", "already landed")
        kb._append_event(conn, tid, "review_deadman", {})
        event_id = kb.max_event_id(conn)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    entry = dict(key="claim", task_id=tid, board=None, row="review_deadman", kind="review_deadman",
                 event_id=event_id, delegation_id="d2", verdict_nonce="abcd", route={}, mode="fork")
    state.claim("claim", entry)
    async def parent(*args):
        return object(), "fork"
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "scratch deadman")
    final = '<<<BOOMERANG-VERDICT nonce=abcd>>>\n' + json.dumps({
        "action": "surface", "summary": "operator decision needed"}) + '\n<<<END-VERDICT>>>'
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    sent = []
    async def send(chat_id, text, metadata=None):
        sent.append(text)
    runner = SimpleNamespace(_kanban_route_adapter=lambda *a: SimpleNamespace(send=send))
    asyncio.run(bw.run_entry(runner, entry, state))
    assert sent == ([] if stale else [f"boomerang handled {tid} (review_deadman): operator decision needed"])
    with kb.connect_closing() as conn:
        assert any(c.body.startswith("boomerang: review_deadman ") for c in kb.list_comments(conn, tid))
