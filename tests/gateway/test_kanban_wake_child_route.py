"""t_54bbaca0: a kanban wake child runs on the PARENT's model by default (Ace msg
1573512367600308385); the operator can override it per card (``wake-model:``), per session and
globally, and an overridden wake names its model on the surfaced line."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as ah
from gateway import kanban_wake_child_route as wcr
from tests.gateway.test_kanban_boomerang_wake_dispatch import (  # noqa: F401  (env is a fixture)
    _REAL_RUN_ENTRY, _card, _claimed_entry, _worker_close, env,
)

SLOTS = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-2"}
CATALOG = {"claude-alr": ("claude-opus-5-5", "claude-fable-5-2"),
           "claude-elr": ("claude-opus-5-5", "claude-fable-5-2"),
           "claude-dtlr": ("claude-opus-5-5", "claude-fable-5-2")}


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    from hermes_cli import model_registry

    monkeypatch.setattr(model_registry, "slot_view", lambda: dict(SLOTS))
    monkeypatch.setattr(model_registry, "catalog_for", lambda p, **_k: CATALOG.get(p, ()))
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: {})


def _parent(provider="claude-elr", model="claude-fable-5-2"):
    return SimpleNamespace(provider=provider, model=model, session_id="sid-parent")


def _cfg(**wc):
    return {"boomerang": {"wake_child": wc}}


def _capture_delegate(monkeypatch):
    from tools import delegate_tool

    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "completed", "summary": "ok"}]})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake)
    return calls


# --- the default: unchanged ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fork", "summary"])
def test_default_unchanged_no_override_runs_the_parent_route(monkeypatch, mode):
    """No card line, no session or global key: the delegate call is byte-for-byte the old one
    (the parent's route, the resolver's inherit mode, no model or provider pin)."""
    assert wcr.requested(None, "home-1") == (None, None)
    assert ah._wake_route({"task_id": "t_1", "home_sid": "home-1"}, _parent()) is None
    calls = _capture_delegate(monkeypatch)
    assert ah._run_child_delegate(_parent(), "goal", mode) == ("completed", "ok")
    [kw] = calls
    assert set(kw) == {"goal", "inherit_context", "inherit_mode", "parent_agent"}
    assert kw["inherit_mode"] == mode and kw["inherit_context"] is True


def test_inherit_at_any_level_is_the_parent():
    assert wcr.resolve_route("inherit", "claude-alr") is None
    assert wcr.resolve_route("parent", "claude-alr") is None


# --- the card line -------------------------------------------------------------------------


@pytest.mark.parametrize("body, want", [
    ("operator-read: required\nwake-model: opus\n", "opus"),
    ("  Wake-Model:   claude-alr/claude-opus-5-5  ", "claude-alr/claude-opus-5-5"),
    ("wake-model: fable\nwake-model: opus", "opus"),      # the last line wins
    ("the wake-model: opus is inline prose", None),       # a whole line only
    ("wake-model:", None),
    (None, None),
])
def test_card_line(body, want):
    assert wcr.card_wake_model(body) == want


# --- precedence ------------------------------------------------------------------------------


def test_precedence_card_session_global():
    cfg = _cfg(model="fable", sessions={"home-1": "claude-dtlr/claude-opus-5-5"})
    assert wcr.requested("opus", "home-1", cfg) == ("opus", "card")
    assert wcr.requested(None, "home-1", cfg) == ("claude-dtlr/claude-opus-5-5", "session")
    assert wcr.requested(None, "home-2", cfg) == ("fable", "global")
    assert wcr.requested(None, "home-2", {}) == (None, None)


# --- resolution ------------------------------------------------------------------------------


def test_slot_keeps_the_parent_lane():
    route = wcr.resolve_route("opus", "claude-elr", source="card")
    assert (route["model"], route["provider"], route["source"]) == ("claude-opus-5-5", "claude-elr", "card")


def test_global_provider_names_the_lane():
    route = wcr.resolve_route("opus", "claude-elr", delegation_cfg=_cfg(provider="claude-alr"))
    assert route["provider"] == "claude-alr"


def test_provider_slash_model():
    route = wcr.resolve_route("claude-dtlr/claude-opus-5-5", "claude-elr")
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "claude-dtlr")


@pytest.mark.parametrize("value", ["opsu", "claude-alr/claude-nope-1", "nolane/claude-opus-5-5"])
def test_unknown_value_falls_back_to_the_parent_with_one_line(caplog, value):
    with caplog.at_level(logging.WARNING, logger=wcr.__name__):
        assert wcr.resolve_route(value, "claude-alr", source="card", task_id="t_x") is None
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "t_x" in lines[0] and repr(value) in lines[0] and "parent's model" in lines[0]


def test_override_runs_summary_pinned(monkeypatch):
    calls = _capture_delegate(monkeypatch)
    route = wcr.resolve_route("opus", "claude-elr")
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        ah._run_child_delegate(_parent(), "goal", "fork")
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    [kw] = calls
    assert (kw["inherit_mode"], kw["model"], kw["provider"]) == ("summary", "claude-opus-5-5", "claude-elr")
    assert kw["credentials_cfg"]["fallback_providers"] == []   # no chain of its own: failure -> the parent


# --- an override that fails: the parent's model and chain run the wake (Ace msg 1573514581643370666) ---
# The default's chain (item 4a) is tests/integration/test_boomerang_parent_routing_http.py::
# test_real_boomerang_fails_over_to_wire_sentinel: the real run_child walks the PARENT's chain.


def test_failed_override_reruns_on_the_parent_route(monkeypatch, caplog):
    from tools import delegate_tool

    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw.get("provider"):
            return json.dumps({"error": "provider claude-elr: 529 overloaded"})
        return json.dumps({"results": [{"status": "completed", "summary": "ok"}]})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake)
    route = {**wcr.resolve_route("opus", "claude-elr"), "parent_mode": "fork"}
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        with caplog.at_level(logging.WARNING):
            assert ah._run_child_delegate(_parent(), "goal", "summary") == ("completed", "ok")
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    first, second = calls
    assert first["provider"] == "claude-elr"
    assert set(second) == {"goal", "inherit_context", "inherit_mode", "parent_agent"}   # the parent's route + chain
    assert second["inherit_mode"] == "fork"
    assert route["fell_back"] == ("wake-model opus failed (provider claude-elr: 529 overloaded), "
                                  "ran on parent claude-elr/claude-fable-5-2")


# --- through dispatch -> run_entry --------------------------------------------------------------


def _run_capturing(env, entry, provider="claude-elr"):
    seen = []

    async def resolved():
        return _parent(provider), "fork"

    claim = {}

    def child(parent, goal, mode):
        seen.append((mode, wcr.CURRENT_ROUTE.get()))
        claim.update(ah.BoomerangWakeState().get_claim(entry["key"]))   # the running claim
        return "failed", "boom"

    env["monkeypatch"].setattr(ah, "parent_resolver", lambda *_a, **_k: resolved())
    env["monkeypatch"].setattr(ah, "run_child", child)
    asyncio.run(_REAL_RUN_ENTRY(env["runner"], entry, ah.BoomerangWakeState()))
    return seen, claim


def test_default_unchanged_through_dispatch(env):
    tid = _card(env, body="no override here")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    assert entry["wake_model"] is None
    seen, claim = _run_capturing(env, entry)
    assert seen == [("fork", None)]
    assert claim["inherit_mode"] == "fork" and not claim.get("wake_model_override")


def test_card_line_overrides_through_dispatch(env):
    tid = _card(env, body="do the thing\nwake-model: opus\n")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    assert entry["wake_model"] == "opus"
    seen, claim = _run_capturing(env, entry)
    [(mode, route)] = seen
    assert mode == "summary" and (route["model"], route["provider"]) == ("claude-opus-5-5", "claude-elr")
    assert claim["inherit_mode"] == "summary" and claim["wake_model_override"] == "claude-elr/claude-opus-5-5"


def test_unknown_card_value_still_runs_the_wake_on_the_parent(env):
    tid = _card(env, body="wake-model: opsu")
    _worker_close(tid)
    seen, claim = _run_capturing(env, _claimed_entry(env, tid))
    assert seen == [("fork", None)] and not claim.get("wake_model_override")


def test_failed_override_surfaces_the_fallback_line_through_run_entry(env):
    tid = _card(env, body="wake-model: opus")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    calls = []

    async def resolved():
        return _parent(), "fork"

    def fake(**kw):
        calls.append(kw)
        if kw.get("provider"):
            return json.dumps({"error": "lane down"})
        return json.dumps({"results": [{"status": "completed", "summary": "no verdict"}]})

    from tools import delegate_tool

    mp = env["monkeypatch"]
    mp.setattr(ah, "parent_resolver", lambda *_a, **_k: resolved())
    mp.setattr(ah, "run_child", ah._run_child_delegate)
    mp.setattr(delegate_tool, "delegate_task", fake)
    asyncio.run(_REAL_RUN_ENTRY(env["runner"], entry, ah.BoomerangWakeState()))
    assert len(calls) == 2 and calls[1]["inherit_mode"] == "fork"
    turns = [t.text for t in env["adapter"].handled]
    want = "wake-model opus failed (lane down), ran on parent claude-elr/claude-fable-5-2"
    assert turns and all(want in t.split("\n")[0] for t in turns), turns


def test_surfaced_line_names_an_overridden_model():
    base = {"task_id": "t_1", "row": "completed", "inherit_mode": "summary", "claimed_at": 0, "wake_text": "w"}
    plain = ah.finding_text(base, {"summary": "s"}, "child surfaced").split("\n")[0]
    assert "model" not in plain
    over = ah.finding_text({**base, "wake_model_override": "claude-alr/claude-opus-5-5"},
                           {"summary": "s"}, "child surfaced").split("\n")[0]
    assert "(completed, summary, model claude-alr/claude-opus-5-5," in over
    note = "wake-model opus failed (lane down), ran on parent claude-elr/claude-fable-5-2"
    fell = ah.finding_text({**base, "wake_model_override": "claude-alr/claude-opus-5-5", "wake_model_fallback": note},
                           {"summary": "s"}, "child completed").split("\n")[0]
    assert f"(completed, summary, {note}," in fell
