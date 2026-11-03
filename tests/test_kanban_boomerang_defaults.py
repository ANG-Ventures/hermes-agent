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


def _board(tmp_path, monkeypatch, title="scratch"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="human:apollo")


def _runner(outcomes, sent):
    """Adapter whose send follows ``outcomes``: True -> receipt, False -> success=False, Exception -> raise."""
    from gateway.platforms.base import SendResult

    async def send(chat_id, text, metadata=None):
        o = outcomes.pop(0) if outcomes else True
        if isinstance(o, Exception):
            raise o
        if o:
            sent.append(text)
        return SendResult(success=bool(o), error=None if o else "injected")
    return SimpleNamespace(_kanban_route_adapter=lambda *a: SimpleNamespace(send=send))


@pytest.mark.parametrize("stale", [False, True])
def test_handback_shape(tmp_path, monkeypatch, stale):
    tid = _board(tmp_path, monkeypatch)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    entry = dict(task_id=tid, board=None, row="review_deadman", delegation_id="d1", route={})
    result = dict(summary="CI is pending\nnot a page", action="comment_why_waits")
    sent = []
    for _ in range(2):   # idempotent per delegation
        bw.enqueue_handback(entry, result, state=state, stale=stale)
        asyncio.run(bw.deliver_handbacks(_runner([], sent), state))
    assert sent == ([] if stale else [f"boomerang handled {tid} (review_deadman): CI is pending"])
    assert state.handbacks() == {}
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert len(comments) == 1 and comments[0].body.startswith("boomerang: review_deadman CI is pending")


@pytest.mark.parametrize("failure", ["raises", "success_false"])
def test_undelivered_line_stays_pending_and_the_tick_delivers_it_once(tmp_path, monkeypatch, failure):
    """Shape tests (1)+(2): no receipt -> the row stays pending; the next tick (reconcile) sends it;
    later ticks never resend."""
    tid = _board(tmp_path, monkeypatch)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    entry = dict(task_id=tid, board=None, row="review_deadman", delegation_id="d1", route={})
    bw.enqueue_handback(entry, dict(summary="CI is pending"), state=state)
    first = RuntimeError("adapter down") if failure == "raises" else False
    outcomes, sent = [first, True, True], []
    runner = _runner(outcomes, sent)
    assert asyncio.run(bw.deliver_handbacks(runner, state)) == 0
    pending = state.handbacks()["d1"]
    assert pending["parts"] == ["line"] and pending["attempts"] == 1
    with kb.connect_closing() as conn:
        assert not [e for e in kb.list_events(conn, tid) if e.kind == bw.DELIVERED_KIND]
    for _ in range(3):   # next ticks
        asyncio.run(bw.reconcile(runner, state=state))
    assert sent == [f"boomerang handled {tid} (review_deadman): CI is pending"]
    assert state.handbacks() == {}
    with kb.connect_closing() as conn:
        [ev] = [e for e in kb.list_events(conn, tid) if e.kind == bw.DELIVERED_KIND]
    assert ev.payload["delegation_id"] == "d1" and ev.payload["part"] == "line"


def test_surface_with_failed_resurface_is_retried_by_the_tick(tmp_path, monkeypatch):
    """Shape test (4): a surface verdict keeps its operator turn; a failed resurface leaves
    resurface_delivered False + a pending turn the tick retries; the turn lands exactly once.
    Round 4 (d): delivery is gated on deliver_turn's admission receipt, not on resurface()
    returning: a normal return with no admission (False) is NOT delivered (Prism 5160c71b78bc)."""
    tid = _board(tmp_path, monkeypatch, "surface")
    with kb.connect_closing() as conn:
        kb._append_event(conn, tid, "review_requested", {})
        kb._append_event(conn, tid, "review_deadman", {})
        event_id = kb.max_event_id(conn)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    entry = dict(key="claim", task_id=tid, board=None, row="review_deadman", kind="review_deadman",
                 event_id=event_id, delegation_id="d4", verdict_nonce="abcd", route={}, mode="fork")
    state.claim("claim", entry)
    from gateway import kanban_owner_wake as ow
    turns, receipts = [], [False, True, True]   # returns normally, but only the 2nd is admitted
    async def deliver_turn(runner, home, text, cards=None):
        ok = receipts.pop(0)
        if ok:
            turns.append(text)
        return ok
    async def parent(*args):
        return object(), "fork"
    home = SimpleNamespace(origin=SimpleNamespace(chat_id="42", platform="discord"))
    monkeypatch.setattr(ow, "resolve_owner", lambda *a: home)
    monkeypatch.setattr(ow, "deliver_turn", deliver_turn)
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "scratch")
    final = "<<<BOOMERANG-VERDICT nonce=abcd>>>\n" + json.dumps(
        {"action": "surface", "summary": "operator decision needed"}) + "\n<<<END-VERDICT>>>"
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    sent = []
    runner = _runner([], sent)
    runner.session_store = None
    asyncio.run(bw.run_entry(runner, entry, state))
    claim = state.get_claim("claim")
    assert claim["state"] == bw.STATE_TERMINAL and claim["resurface_delivered"] is False
    assert state.handbacks()["d4"]["parts"] == ["turn"]
    assert sent == [f"boomerang handled {tid} (review_deadman): operator decision needed"]
    for _ in range(3):
        asyncio.run(bw.reconcile(runner, state=state))
    assert len(turns) == 1 and turns[0].startswith(f"[boomerang-wake] child surfaced {tid}")
    assert "operator decision needed" in turns[0]
    assert state.get_claim("claim")["resurface_delivered"] is True
    assert state.handbacks() == {} and len(sent) == 1


@pytest.mark.parametrize("cli_auto_subscribe", [False, True])
def test_cli_create_boomerang_follows_cli_auto_subscribe(tmp_path, monkeypatch, cli_auto_subscribe):
    """Shape test (5), Prism 1aec29b63c57: scripts/cron drive the CLI, so the boomerang default
    applies to a CLI create only when kanban.cli_auto_subscribe is on, identity or not."""
    import re
    from pathlib import Path
    from hermes_cli import kanban as kc
    from gateway.session_context import reset_session_vars

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # an operator CLI, not a worker (a dispatched worker running this suite would otherwise count)
    import agent.delegation_context as dc
    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reset_session_vars()
    if cli_auto_subscribe:
        (home / "config.yaml").write_text("kanban:\n  cli_auto_subscribe: true\n")
    for k, v in {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "chat1", "HERMES_SESSION_CHAT_TYPE": "dm",
                 "HERMES_SESSION_USER_ID": "user1", "HERMES_SESSION_MESSAGE_ID": "462"}.items():
        monkeypatch.setenv(k, v)
    kb.init_db()
    out = kc.run_slash('--board default create "cli scratch" --assignee worker1')
    tid = re.search(r"Created\s+(t_[0-9a-f]+)\b", out).group(1)
    with kb.connect_closing() as conn:
        assert bool(kb.get_task(conn, tid).boomerang_wake) is cli_auto_subscribe
        assert bool(kbn.list_notify_subs(conn, tid)) is cli_auto_subscribe


@pytest.mark.parametrize("opt_out", [False, True, "false"])
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
        args["boomerang_wake"] = False if opt_out is True else opt_out  # "false": Prism a8e263caef71
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
    turns = []
    async def resurface(runner, e, text=None):
        turns.append(text)
        return True
    monkeypatch.setattr(bw, "resurface", resurface)
    sent = []
    asyncio.run(bw.run_entry(_runner([], sent), entry, state))
    assert sent == ([] if stale else [f"boomerang handled {tid} (review_deadman): operator decision needed"])
    assert len(turns) == (0 if stale else 1)
    with kb.connect_closing() as conn:
        assert any(c.body.startswith("boomerang: review_deadman ") for c in kb.list_comments(conn, tid))


# --- round 4 (ruling t_e98f651e-handback-first-then-everything): pending row first, breadcrumb
# best-effort, delivered only on an admission receipt, reconcile re-drains after a restart. -----

def _surface_entry(tmp_path, monkeypatch, did, action="surface"):
    tid = _board(tmp_path, monkeypatch, "round4")
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))   # a deadman card is in review
        conn.commit()
        kb._append_event(conn, tid, "review_requested", {})
        kb._append_event(conn, tid, "review_deadman", {})
        event_id = kb.max_event_id(conn)
        status = kb.get_task(conn, tid).status
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    entry = dict(key="claim", task_id=tid, board=None, row="review_deadman", kind="review_deadman",
                 event_id=event_id, delegation_id=did, verdict_nonce="abcd", route={}, mode="fork",
                 status=status)
    state.claim("claim", entry)
    async def parent(*args):
        return object(), "fork"
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "scratch")
    body = {"action": action, "summary": "CI pending on main"}
    if action == "comment_why_waits":
        body["why_waits"] = {"pr_state": "open", "last_operator_action": "none", "flips_when": "CI green"}
    final = "<<<BOOMERANG-VERDICT nonce=abcd>>>\n" + json.dumps(body) + "\n<<<END-VERDICT>>>"
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    return tid, state, entry


def test_breadcrumb_failure_after_ok_verdict_keeps_the_pending_row(tmp_path, monkeypatch):
    """(a) Prism 9def48086ed1: kb.add_comment raises after an ok verdict -> the pending row exists
    and the next tick delivers the line."""
    tid, state, entry = _surface_entry(tmp_path, monkeypatch, "da", action="comment_why_waits")
    real_add = kb.add_comment
    def add_comment(conn, task_id, author, body, *a, **k):
        if body.startswith("boomerang: "):
            raise RuntimeError("board locked")
        return real_add(conn, task_id, author, body, *a, **k)
    monkeypatch.setattr(kb, "add_comment", add_comment)
    sent = []
    asyncio.run(bw.run_entry(_runner([False], sent), entry, state))   # first send: no receipt
    assert state.get_claim("claim")["state"] == bw.STATE_TERMINAL
    assert state.handbacks()["da"]["parts"] == ["line"] and sent == []
    asyncio.run(bw.reconcile(_runner([], sent), state=state))
    assert sent == [f"boomerang handled {tid} (review_deadman): CI pending on main"]
    assert state.handbacks() == {}


def test_exit_between_verdict_and_handback_is_delivered_by_reconcile(tmp_path, monkeypatch):
    """(b) Prism b967232e9f89: the gateway "exits" right after execute_verdict made the claim
    terminal; the pending row was committed in that same write, so reconcile() delivers it."""
    tid, state, entry = _surface_entry(tmp_path, monkeypatch, "db", action="comment_why_waits")

    class Exit(BaseException):
        pass

    def die(*a, **k):
        raise Exit()
    monkeypatch.setattr(bw, "_breadcrumb", die, raising=False)   # the first thing after execute_verdict
    monkeypatch.setattr(bw, "enqueue_handback", die)
    sent = []
    with pytest.raises(Exit):
        asyncio.run(bw.run_entry(_runner([], sent), entry, state))
    assert state.get_claim("claim")["state"] == bw.STATE_TERMINAL and sent == []
    restarted = bw.BoomerangWakeState(tmp_path / "claims.json")   # a fresh process reads the file
    asyncio.run(bw.reconcile(_runner([], sent), state=restarted))
    asyncio.run(bw.reconcile(_runner([], sent), state=restarted))
    assert sent == [f"boomerang handled {tid} (review_deadman): CI pending on main"]
    assert restarted.handbacks() == {}


def test_executing_claim_with_board_receipt_owes_its_handback_after_restart(tmp_path, monkeypatch):
    """(b, reconcile leg) a claim found EXECUTING with its receipt on the board is closed WITH a
    pending handback, delivered once."""
    tid, state, entry = _surface_entry(tmp_path, monkeypatch, "dr")
    state.update_claim("claim", state=bw.STATE_EXECUTING, action="comment_why_waits")
    with kb.connect_closing() as conn:
        kb._append_event(conn, tid, bw.RECEIPT_KIND, {"operator_kind": "machine", "via": bw.VIA,
                                                      "delegation_id": "dr", "action": "comment_why_waits"})
    sent = []
    for _ in range(2):
        asyncio.run(bw.reconcile(_runner([], sent), state=state))
    assert state.get_claim("claim")["state"] == bw.STATE_TERMINAL
    assert sent == [f"boomerang handled {tid} (review_deadman): comment_why_waits"]


def _busy_home(monkeypatch, runner):
    """A real WireAdapter + runner FIFO; the home session is mid-turn (busy) until ``release``."""
    from evals.heartbeat_idle_wire import WireAdapter
    from gateway import kanban_owner_wake as ow
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, build_session_key

    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    gw = object.__new__(GatewayRunner)
    gw._running_agents = {}
    gw.adapters = {adapter.platform: adapter}
    gw._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=adapter.platform, chat_id="42", user_id="42", chat_type="dm")
    key = build_session_key(source)
    home = SimpleNamespace(origin=source, session_key=key, session_id="home-sid")
    monkeypatch.setattr(ow, "resolve_owner", lambda *a: home)
    runner._adapter_for_source = lambda s: adapter
    runner.session_store = None
    release, started, received = asyncio.Event(), asyncio.Event(), []

    async def handler(event):
        received.append(event.text)
        started.set()
        await release.wait()
        if key not in adapter._pending_messages:
            nxt = gw._promote_queued_event(key, adapter, None)
            if nxt is not None:
                adapter._pending_messages[key] = nxt
    adapter.set_message_handler(handler)
    return gw, adapter, source, key, release, started, received, MessageEvent


def test_busy_home_at_queue_cap_leaves_the_turn_pending_until_the_queue_drains(tmp_path, monkeypatch):
    """(c) Prism 5160c71b78bc: the home session is at _BUSY_QUEUE_MAX_PENDING, so the adapter does
    not admit the surfaced turn; the row stays pending and the next tick after the drain delivers it."""
    tid, state, entry = _surface_entry(tmp_path, monkeypatch, "dc")
    sent = []
    runner = _runner([], sent)

    async def go():
        gw, adapter, source, key, release, started, received, MessageEvent = _busy_home(monkeypatch, runner)
        await adapter.handle_message(MessageEvent(text="human-active", source=source))
        await asyncio.wait_for(started.wait(), 2)
        await adapter.handle_message(MessageEvent(text="human-pending", source=source))
        adapter.set_busy_session_handler(gw._handle_active_session_busy_message)
        gw._BUSY_QUEUE_MAX_PENDING = 1
        await bw.run_entry(runner, entry, state)
        assert state.get_claim("claim")["resurface_delivered"] is False
        assert state.handbacks()["dc"]["parts"] == ["turn"]          # line sent, turn not admitted
        await bw.reconcile(runner, state=state)                       # still at cap: still pending
        assert state.handbacks()["dc"]["parts"] == ["turn"]
        release.set()
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
        await bw.reconcile(runner, state=state)                       # drained: admitted
        await bw.reconcile(runner, state=state)
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
        return received

    received = asyncio.run(go())
    turns = [t for t in received if t.startswith(f"[boomerang-wake] child surfaced {tid}")]
    assert received[:2] == ["human-active", "human-pending"] and len(turns) == 1
    assert state.get_claim("claim")["resurface_delivered"] is True and state.handbacks() == {}
    assert sent == [f"boomerang handled {tid} (review_deadman): CI pending on main"]
