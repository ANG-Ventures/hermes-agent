"""t_1637dd3e: a card homed on the ``operator:<name>`` pseudo-session gets its boomerang turn.

Measured 2026-11-12: 5,229 cards are homed on ``operator:apollo``. ``resolve_owner`` returns None
for them, so ``run_entry`` failed the claim with ``home session not live here`` and the handback's
turn part was retried (later dead-lettered at 24 h) without ever reaching a session. The turn now
goes to the live session of the handback row's ``route`` chat; with none, it settles at once.
"""
from __future__ import annotations

import asyncio
import contextlib

from gateway import kanban_boomerang_wake as ah
from tests.gateway.test_kanban_boomerang_wake_backoff import T0, _comments, _row, _ticks, clock  # noqa: F401
from tests.gateway.test_kanban_boomerang_wake_dispatch import CHAT, _card, env  # noqa: F401 (fixture)

from hermes_cli import kanban_db as kb

REAL_RUN_ENTRY = ah.run_entry   # the env fixture swaps it for a recorder
OTHER_CHAT = "1499999999999999999"


def _operator_card() -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="card", assignee="worker", session_id="operator:apollo",
                              boomerang_wake=True)


def _operator_row(tid: str, chat: str = CHAT, **kw) -> dict:
    row = _row(tid, parts=[ah.PART_TURN], created_at=T0, home_sid="operator:apollo", turn="wake text", **kw)
    row["route"]["chat_id"] = chat
    row["action"] = "surface"
    return row


def test_operator_home_turn_goes_to_the_live_route_chat_session(env, clock, monkeypatch):  # noqa: F811
    tid = _operator_card()
    monkeypatch.setattr(ah, "_stale_refusal", lambda *_a, **_k: None)
    state = ah.BoomerangWakeState()
    state.put_handback("del-op", _operator_row(tid))
    asyncio.run(ah.deliver_handbacks(env["runner"], state))
    turns = env["adapter"].handled
    assert len(turns) == 1 and turns[0].text == "wake text"
    assert turns[0].metadata["gateway_session_key"] == env["group"].session_key
    assert state.handbacks() == {} and state.dead_letters() == {}


def test_operator_home_without_a_live_route_session_dead_letters_on_first_attempt(env, clock, monkeypatch):  # noqa: F811
    tid = _operator_card()
    monkeypatch.setattr(ah, "_stale_refusal", lambda *_a, **_k: None)
    logs: list = []
    monkeypatch.setattr(ah, "post_logs", logs.append)
    tries: list = []
    real = ah.resurface

    async def counting(runner, entry, text=None):
        tries.append(1)
        return await real(runner, entry, text)

    monkeypatch.setattr(ah, "resurface", counting)
    state = ah.BoomerangWakeState()
    state.put_handback("del-none", _operator_row(tid, chat=OTHER_CHAT))
    _ticks(env, clock, 3600)
    assert len(tries) == 1                                   # no retry
    assert state.handbacks() == {}
    dead = state.dead_letters()["del-none"]
    assert dead["parts"] == [ah.PART_TURN] and "no live home session" in dead["reasons"][ah.PART_TURN]
    assert len(logs) == 1 and "del-none" in logs[0]
    assert len([c for c in _comments(tid) if "dead-letter" in c]) == 1
    assert env["adapter"].handled == []


def test_resolve_parent_on_operator_home_uses_the_route_chat_session(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ah, "_db_parent", lambda _r, home: ("db-parent", home.session_key))
    entry = {"home_sid": "operator:apollo", "mode": "summary",
             "route": {"platform": "discord", "chat_id": CHAT, "session_id": "", "profile": None}}
    parent, mode = asyncio.run(ah.resolve_parent(env["runner"], entry, wait_s=0.0))
    assert parent == ("db-parent", env["group"].session_key) and mode == "summary"


def test_run_entry_on_operator_home_runs_the_child_instead_of_failing_the_claim(env, monkeypatch):  # noqa: F811
    ran: list = []
    failed: list = []

    async def scoped(_runner, entry, _state):
        ran.append(entry["key"])

    async def fail(_runner, _entry, reason, _state, *a, **k):
        failed.append(reason)

    @contextlib.asynccontextmanager
    async def scope(_source):
        yield

    monkeypatch.setattr(ah, "_run_entry_scoped", scoped)
    monkeypatch.setattr(ah, "_fail", fail)
    env["runner"]._async_profile_scope_for_source = scope
    state = ah.BoomerangWakeState()
    entry = {"key": "b|t|1", "delegation_id": "del-x", "task_id": "t", "home_sid": "operator:apollo",
             "route": {"platform": "discord", "chat_id": CHAT, "session_id": "", "profile": None}}
    asyncio.run(REAL_RUN_ENTRY(env["runner"], entry, state))
    assert ran == ["b|t|1"] and failed == []


def test_session_homed_card_is_unchanged(env, clock, monkeypatch):  # noqa: F811
    """A real home wins over the route chat; a dead real home still retries (not settled at once)."""
    tid = _card(env)
    monkeypatch.setattr(ah, "_stale_refusal", lambda *_a, **_k: None)
    state = ah.BoomerangWakeState()
    row = _operator_row(tid, chat=OTHER_CHAT)
    row["home_sid"] = env["group"].session_id
    state.put_handback("del-home", row)
    asyncio.run(ah.deliver_handbacks(env["runner"], state))
    assert len(env["adapter"].handled) == 1
    assert env["adapter"].handled[0].metadata["gateway_session_key"] == env["group"].session_key

    gone = _operator_row(tid)
    gone["home_sid"] = "20261019_999999_gone"
    state.put_handback("del-gone", gone)
    asyncio.run(ah.deliver_handbacks(env["runner"], state))
    assert "del-gone" in state.handbacks() and state.dead_letters() == {}
    assert len(env["adapter"].handled) == 1


def test_route_chat_session_requires_the_named_profile():
    """Prism 5674c71176df: an unset session profile is "default", not a wildcard for a named one."""
    from types import SimpleNamespace as NS

    def sess(name, profile):
        return NS(name=name, origin=NS(platform="discord", chat_id=CHAT, thread_id="", profile=profile))

    store = NS(list_sessions=lambda: [sess("unset", None), sess("work", "work")])
    route = {"platform": "discord", "chat_id": CHAT, "profile": "work"}
    assert ah._route_chat_session(store, route).name == "work"
    assert ah._route_chat_session(store, {**route, "profile": "default"}).name == "unset"
    assert ah._route_chat_session(store, {**route, "profile": None}).name == "unset"
