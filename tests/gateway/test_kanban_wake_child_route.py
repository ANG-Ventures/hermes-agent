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
_REAL_READ = wcr.read_provider_routing
CATALOG = {"claude-alr": ("claude-opus-5-5", "claude-fable-5-2"),
           "claude-elr": ("claude-opus-5-5", "claude-fable-5-2"),
           "claude-dtlr": ("claude-opus-5-5", "claude-fable-5-2")}


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    from hermes_cli import model_registry

    monkeypatch.setattr(model_registry, "slot_view", lambda: dict(SLOTS))
    monkeypatch.setattr(model_registry, "catalog_for", lambda p, **_k: CATALOG.get(p, ()))
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: {})
    monkeypatch.setattr(wcr, "read_provider_routing", lambda: None)   # no provider_routing block


def _parent(provider="claude-elr", model="claude-fable-5-2", **extra):
    return SimpleNamespace(provider=provider, model=model, session_id="sid-parent", **extra)


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
def test_unknown_value_is_unusable(value):
    with pytest.raises(wcr.Unusable):
        wcr.resolve_route(value, "claude-alr", source="global", task_id="t_x")


# --- the card line takes a slot name only (Prism cdec138fffac) -------------------------------


@pytest.mark.parametrize("value", ["claude-dtlr/claude-opus-5-5", "claude-opus-5-5", "claude-alr/opus"])
def test_card_line_refuses_anything_but_a_slot(value):
    with pytest.raises(wcr.Unusable, match="slot name only"):
        wcr.resolve_route(value, "claude-elr", source="card")
    # the operator's config keys still take provider/model and bare ids
    assert wcr.resolve_route(value, "claude-elr", source="session")["model"] == "claude-opus-5-5"


# --- unreadable config values are reported, not skipped -------------------------------------------


@pytest.mark.parametrize("cfg", [_cfg(model=123), _cfg(sessions="opus"), _cfg(sessions={"home-1": ["opus"]})])
def test_unreadable_config_value_is_unusable(cfg):
    with pytest.raises(wcr.Unusable, match="unreadable"):
        value, source = wcr.requested(None, "home-1", cfg)
        wcr.resolve_route(value, "claude-elr", source=source)
    assert wcr.requested("opus", "home-1", cfg) == ("opus", "card")   # a card line still wins


# --- Prism bd8cd36ed8a8: a slot's target id is never split into provider/model ---------------------


def test_slot_target_with_a_namespace_stays_one_model_id(monkeypatch):
    from hermes_cli import model_registry

    monkeypatch.setattr(model_registry, "slot_view", lambda: {"opus": "anthropic/claude-opus-4.6"})
    monkeypatch.setattr(model_registry, "catalog_for",
                        lambda p, **_k: ("anthropic/claude-opus-4.6",) if p == "openrouter" else ())
    route = wcr.resolve_route("opus", "openrouter", source="card")
    assert (route["model"], route["provider"]) == ("anthropic/claude-opus-4.6", "openrouter")


# --- Prism 8b65f0d36f77: the session override is keyed by the RESOLVED home session id -------------


def test_session_override_found_by_the_resolved_home_session_id(monkeypatch):
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: _cfg(sessions={"sid-parent": "opus"}))
    route = ah._wake_route({"task_id": "t_1", "home_sid": "agent:main:discord:group:123"}, _parent(), {})
    assert (route["source"], route["model"]) == ("session", "claude-opus-5-5")


# --- Prism 3d5c62bd0009: a same-lane override never drops the parent's privacy/routing filters ------


def test_parent_filter_attribute_makes_any_model_override_unusable():
    parent = SimpleNamespace(**vars(_parent()), provider_data_collection="deny")
    for value, source in (("opus", "card"), ("claude-alr/opus", "global")):   # same lane or another
        with pytest.raises(wcr.Unusable, match=r"^provider routing filters are configured$"):
            wcr.resolve_route(value, "claude-elr", source=source, parent=parent)
    assert wcr.resolve_route("opus", "claude-elr", source="card", parent=_parent())["provider"] == "claude-elr"


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


_RAISE = object()


@pytest.mark.parametrize("override_result, why", [
    ({"status": "timeout", "summary": "child timed out after 600s"}, "timeout: child timed out after 600s"),
    ({"status": "error", "error": "401 unauthorized"}, "error: 401 unauthorized"),
    ({"status": "interrupted", "interrupt_message": "watchdog"}, "interrupted: watchdog"),
    ({"status": "failed", "failure_reason": "content_policy_blocked", "summary": "refused"}, "refused: refused"),
    ({"status": "completed", "summary": ""}, "completed: empty final message"),
    ({"status": "failed", "error": "lane exhausted"}, "lane exhausted"),
    (_RAISE, "error: RuntimeError: delegate_task blew up"),
])
def test_any_non_success_override_reruns_on_the_parent(monkeypatch, override_result, why):
    """Ace msg 1573514581643370666: whatever goes wrong on the override, the wake runs on the parent."""
    from tools import delegate_tool

    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw.get("provider"):
            if override_result is _RAISE:
                raise RuntimeError("delegate_task blew up")
            return json.dumps({"results": [override_result]})
        return json.dumps({"results": [{"status": "completed", "summary": "ok"}]})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake)
    route = {**wcr.resolve_route("opus", "claude-elr"), "parent_mode": "fork"}
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        assert ah._run_child_delegate(_parent(), "goal", "summary") == ("completed", "ok")
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    first, second = calls
    assert first["provider"] == "claude-elr" and "provider" not in second and second["inherit_mode"] == "fork"
    assert route["fell_back"] == f"wake-model opus failed ({why}), ran on parent claude-elr/claude-fable-5-2"


def test_a_stop_on_an_overridden_child_is_not_rerun(monkeypatch):
    from gateway.run import _INTERRUPT_REASON_STOP
    from tools import delegate_tool

    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "interrupted", "interrupt_message": _INTERRUPT_REASON_STOP}]})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake)
    route = wcr.resolve_route("opus", "claude-elr")
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        assert ah._run_child_delegate(_parent(), "goal", "summary")[0] == "stopped"
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    assert len(calls) == 1 and "fell_back" not in route


# --- through dispatch -> run_entry --------------------------------------------------------------


def _run_capturing(env, entry, provider="claude-elr", reasoning=None):
    seen = []

    async def resolved():
        return _parent(provider, reasoning_config=reasoning), "fork"

    claim = {}

    def child(parent, goal, mode):
        seen.append((mode, wcr.CURRENT_ROUTE.get()))
        claim.update(ah.BoomerangWakeState().get_claim(entry["key"]))   # the running claim
        return "failed", "boom"

    env["monkeypatch"].setattr(ah, "parent_resolver", lambda *_a, **_k: resolved())
    env["monkeypatch"].setattr(ah, "run_child", child)
    env["runner"]._provider_routing = {}
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
    assert claim["wake_model_fallback"].startswith("wake-model opsu not usable (")


@pytest.mark.parametrize("body, why", [
    ("wake-model: opsu", "the card line takes a model-routing slot name only"),
    ("wake-model: claude-dtlr/claude-opus-5-5", "the card line takes a model-routing slot name only"),
])
def test_unusable_override_is_surfaced_on_the_wake_line(env, body, why):
    """Item 2: an override that cannot be used is SURFACED to the home, not only logged."""
    tid = _card(env, body=body)
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    calls = []

    async def resolved():
        return _parent(), "fork"

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "completed", "summary": "no verdict"}]})

    from tools import delegate_tool

    mp = env["monkeypatch"]
    env["runner"]._provider_routing = {}
    mp.setattr(ah, "parent_resolver", lambda *_a, **_k: resolved())
    mp.setattr(ah, "run_child", ah._run_child_delegate)
    mp.setattr(delegate_tool, "delegate_task", fake)
    asyncio.run(_REAL_RUN_ENTRY(env["runner"], entry, ah.BoomerangWakeState()))
    assert len(calls) == 1 and "provider" not in calls[0] and calls[0]["inherit_mode"] == "fork"
    label = body.split(": ", 1)[1]
    want = f"wake-model {label} not usable ({why}), ran on parent claude-elr/claude-fable-5-2"
    turns = [t.text for t in env["adapter"].handled]
    assert turns and all(want in t.split("\n")[0] for t in turns), turns


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
    env["runner"]._provider_routing = {}
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
    fell = ah.finding_text({**base, "wake_model_override": "claude-alr/claude-opus-5-5", "wake_model_fallback": note,
                            "wake_model_fallback_ran": True}, {"summary": "s"}, "child completed").split("\n")[0]
    assert f"(completed, summary, {note}," in fell
    eff = ah.finding_text({**base, "wake_effort": "medium"}, {"summary": "s"}, "child surfaced").split("\n")[0]
    assert "(completed, summary, effort medium," in eff


# --- item 5: the effort override (Ace msgs 1573526187576401950, 1573526653924151358) ---------------


@pytest.mark.parametrize("body, want", [
    ("wake-effort: medium", "medium"), ("  Wake-Effort:  xhigh ", "xhigh"), ("the wake-effort: low inline", None),
])
def test_card_effort_line(body, want):
    assert wcr.card_wake_effort(body) == want


def test_effort_accepts_every_runtime_level_and_nothing_else():
    from hermes_constants import VALID_REASONING_EFFORTS

    for word in ("none", *VALID_REASONING_EFFORTS):
        assert wcr.resolve_effort(word.upper(), source="card") == word
    assert wcr.resolve_effort("inherit", source="card") is None
    for bad in ("hgih", "claude-alr/claude-opus-5-5", "medium; rm -rf"):
        with pytest.raises(wcr.Unusable, match="effort must be one of none"):
            wcr.resolve_effort(bad, source="card")


def test_effort_precedence_card_session_global():
    cfg = _cfg(effort="low", effort_sessions={"home-1": "high"})
    assert wcr.requested_effort("medium", "home-1", cfg) == ("medium", "card")
    assert wcr.requested_effort(None, ["sid-x", "home-1"], cfg) == ("high", "session")
    assert wcr.requested_effort(None, "home-2", cfg) == ("low", "global")
    assert wcr.requested_effort(None, "home-2", _cfg(model="opus")) == (None, None)


def test_default_effort_unchanged_no_reasoning_kwarg(monkeypatch):
    calls = _capture_delegate(monkeypatch)
    ah._run_child_delegate(_parent(), "goal", "fork")
    assert "reasoning_effort" not in calls[0]


def test_effort_alone_keeps_the_parent_route_and_fork(monkeypatch):
    """Effort without wake-model: parent model, lane and mode (a fork stays a fork), new effort."""
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: _cfg(effort="medium"))
    route = ah._wake_route({"task_id": "t_1", "home_sid": "home-1"}, _parent())
    assert route == {"effort": "medium", "label": "effort medium"}
    calls = _capture_delegate(monkeypatch)
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        assert ah._run_child_delegate(_parent(), "goal", "fork") == ("completed", "ok")
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    [kw] = calls
    assert kw == {"goal": "goal", "inherit_context": True, "parent_agent": kw["parent_agent"],
                  "inherit_mode": "fork", "reasoning_effort": "medium"}


def test_effort_with_model_rides_the_summary_pin(monkeypatch):
    route = {**wcr.resolve_route("opus", "claude-elr"), "effort": "high"}
    kw = wcr.delegate_kwargs(route, "fork")
    assert (kw["inherit_mode"], kw["model"], kw["reasoning_effort"]) == ("summary", "claude-opus-5-5", "high")


def test_unusable_effort_is_surfaced_and_the_model_override_still_applies(monkeypatch):
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: _cfg(effort="hgih"))
    route = ah._wake_route({"task_id": "t_1", "home_sid": "home-1", "wake_model": "opus"}, _parent(), {})
    assert route["model"] == "claude-opus-5-5" and "effort" not in route
    assert route["unusable"].startswith("wake-effort hgih not usable (effort must be one of none|")
    assert route["unusable"].endswith("), ran at the parent's effort")


def test_card_effort_through_dispatch_names_the_effort(env):
    """Review item 4: a changed effort cannot fork (the request fingerprint rebuilds it as a
    summary), so the wake runs and REPORTS summary; the parent's own effort keeps the fork."""
    tid = _card(env, body="wake-effort: medium")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    assert entry["wake_effort"] == "medium"
    seen, claim = _run_capturing(env, entry)
    [(mode, route)] = seen
    assert mode == "summary" and route["effort"] == "medium" and not route.get("model")
    assert route["parent_mode"] == "fork"   # a fallback still runs the parent's fork
    assert claim["wake_effort"] == "medium" and claim["inherit_mode"] == "summary"
    line = ah.finding_text(claim, {"summary": "s"}, "child surfaced").split("\n")[0]
    assert ", summary, effort medium," in line


def test_card_effort_equal_to_the_parent_keeps_the_fork(env):
    tid = _card(env, body="wake-effort: high")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    seen, claim = _run_capturing(env, entry, reasoning={"enabled": True, "effort": "high"})
    [(mode, route)] = seen
    assert mode == "fork" and claim["inherit_mode"] == "fork"


# --- the delegate_task kwarg reaches the child build, fork path included ---------------------------


@pytest.mark.parametrize("mode", ["fork", "summary"])
def test_reasoning_override_replaces_the_child_effort_on_every_path(monkeypatch, mode):
    from tools import delegate_tool as dt
    from tools import delegate_tool_fork as fork

    seen = {}

    class Stop(Exception):
        pass

    def fake_agent(**kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    monkeypatch.setattr(fork, "fork_eligible", lambda *a, **k: (True, ""))
    parent = SimpleNamespace(provider="claude-elr", model="claude-fable-5-2", base_url="http://x", api_key="k",
                             api_mode="anthropic_messages", reasoning_config={"enabled": True, "effort": "high"},
                             session_id="sid-parent", _delegate_depth=0, _fallback_chain=[], request_overrides={},
                             _client_kwargs={}, _session_messages=[], enabled_toolsets=["file"])
    with pytest.raises(Exception):
        dt._build_child_agent(task_index=0, goal="g", context=None, toolsets=None, model=None, max_iterations=3,
                              task_count=1, parent_agent=parent, inherit_context=True, inherit_mode=mode,
                              reasoning_override={"enabled": True, "effort": "medium"})
    assert seen, "the child build never reached AIAgent"
    assert seen["reasoning_config"] == {"enabled": True, "effort": "medium"}


def test_changed_effort_fork_is_rebuilt_as_summary_by_the_fingerprint_check(monkeypatch):
    """Review item 4: past the constructor. The fork child carries the new effort, the request
    fingerprint differs on reasoning_config only, and the build is redone as a summary fold (the
    wake reports that mode; test_card_effort_through_dispatch_names_the_effort)."""
    from tools import delegate_tool as dt
    from tools import delegate_tool_fork as fork

    parent = SimpleNamespace(provider="claude-elr", model="claude-fable-5-2", base_url="http://x", api_key="k",
                             api_mode="anthropic_messages", reasoning_config={"enabled": True, "effort": "high"},
                             session_id="sid-parent", _delegate_depth=0, _fallback_chain=[], request_overrides={},
                             _client_kwargs={}, _session_messages=[], enabled_toolsets=["file"],
                             _cached_system_prompt="SYS", _cached_system_prompt_static="SYS", tools=[],
                             _cache_ttl=None, _use_prompt_caching=True, _use_native_cache_layout=True,
                             max_tokens=None, ephemeral_system_prompt=None, prefill_messages=None)
    built, ineligible = [], []

    class Stop(Exception):
        pass

    def fake_agent(**kw):
        built.append(kw)
        if len(built) > 1:
            raise Stop
        child = SimpleNamespace(**{**vars(parent), "reasoning_config": kw["reasoning_config"],
                                   "session_id": "sid-child"})
        child.close = lambda *a, **k: None
        return child

    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    monkeypatch.setattr(fork, "fork_eligible", lambda *a, **k: (True, ""))
    monkeypatch.setattr(fork, "log_ineligible", lambda reason, *a, **k: ineligible.append(reason))
    with pytest.raises(Stop):
        dt._build_child_agent(task_index=0, goal="g", context=None, toolsets=None, model=None, max_iterations=3,
                              task_count=1, parent_agent=parent, inherit_context=True, inherit_mode="fork",
                              reasoning_override={"enabled": True, "effort": "medium"})
    assert ineligible == ["same_request_params:reasoning_config"]
    assert len(built) == 2 and all(kw["reasoning_config"] == {"enabled": True, "effort": "medium"} for kw in built)


def test_delegate_task_refuses_an_unknown_effort():
    from tools import delegate_tool as dt

    out = json.loads(dt.delegate_task(goal="g", reasoning_effort="hgih", parent_agent=_parent()))
    assert "not a known effort" in out["error"]


# --- second-vendor read of 502098b3 (card comment 442351) ------------------------------------------


def _run_entry_with(env, body, fake, *, parent=None, routing=None, draining=False):
    """A real _run_entry_scoped with ``fake`` as delegate_task; returns (entry, home turns)."""
    from tools import delegate_tool

    tid = _card(env, body=body)
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    p = parent or _parent()

    async def resolved():
        return p, "fork"

    mp = env["monkeypatch"]
    env["runner"]._provider_routing = {} if routing is None else routing
    if draining:
        env["runner"]._draining = True
    mp.setattr(ah, "parent_resolver", lambda *_a, **_k: resolved())
    mp.setattr(ah, "run_child", ah._run_child_delegate)
    mp.setattr(delegate_tool, "delegate_task", fake)
    asyncio.run(_REAL_RUN_ENTRY(env["runner"], entry, ah.BoomerangWakeState()))
    return entry, [t.text for t in env["adapter"].handled]


def _parent_only(calls):
    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "completed", "summary": "no verdict"}]})
    return fake


# card comment 443064 item 1: a MODEL override is not usable whenever ANY provider routing filter
# is configured anywhere, or the routing config cannot be read. No lane comparison, no aliases.

_FILTERED = "provider routing filters are configured"


def _surfaced(turns, want):
    assert turns and all(want in t.split("\n")[0] for t in turns), turns


@pytest.mark.parametrize("routing", [
    {"only": ["Anthropic"]},                                   # flat filter
    {"data_collection": "deny"},
    {"models": {"claude-opus-5-5": {"only": ["Anthropic"]}}},  # per-model filter only
])
def test_any_configured_filter_makes_the_model_override_unusable(env, routing):
    env["monkeypatch"].setattr(wcr, "read_provider_routing", lambda: routing)
    calls = []
    _, turns = _run_entry_with(env, "wake-model: opus", _parent_only(calls))
    assert len(calls) == 1 and "provider" not in calls[0] and calls[0]["inherit_mode"] == "fork"
    _surfaced(turns, f"wake-model opus not usable ({_FILTERED}), ran on parent claude-elr/claude-fable-5-2")


def test_filter_in_the_gateway_memory_copy_is_enough(env):
    calls = []
    _, turns = _run_entry_with(env, "wake-model: opus", _parent_only(calls), routing={"sort": "price"})
    assert len(calls) == 1 and "provider" not in calls[0]
    _surfaced(turns, f"wake-model opus not usable ({_FILTERED})")


def test_unreadable_routing_config_makes_the_model_override_unusable(env):
    def broken():
        raise OSError("permission denied")

    env["monkeypatch"].setattr(wcr, "read_provider_routing", broken)
    calls = []
    _, turns = _run_entry_with(env, "wake-model: opus", _parent_only(calls))
    assert len(calls) == 1 and "provider" not in calls[0]
    _surfaced(turns, "wake-model opus not usable (provider routing could not be read), ran on parent")


def test_alias_spelling_is_refused_by_the_rule_alone(monkeypatch):
    from hermes_cli import model_registry

    monkeypatch.setattr(model_registry, "catalog_for", lambda p, **_k: ("claude-opus-5-5",) if p == "or" else ())
    monkeypatch.setattr(wcr, "read_provider_routing", lambda: {"data_collection": "deny"})
    with pytest.raises(wcr.Unusable, match=f"^{_FILTERED}$"):
        wcr.resolve_route("or/claude-opus-5-5", "openrouter", source="global", parent=_parent("openrouter"))
    monkeypatch.setattr(wcr, "read_provider_routing", lambda: None)
    assert wcr.resolve_route("or/claude-opus-5-5", "openrouter", source="global")["provider"] == "or"


@pytest.mark.parametrize("routing", [None, {}, {"only": [], "models": {}}])
def test_no_routing_filter_accepts_the_override(monkeypatch, routing):
    monkeypatch.setattr(wcr, "read_provider_routing", lambda: routing)
    assert wcr.resolve_route("opus", "claude-elr", source="card", parent=_parent())["model"] == "claude-opus-5-5"


def test_effort_override_ignores_routing_filters(monkeypatch):
    monkeypatch.setattr(wcr, "read_provider_routing", lambda: {"only": ["Anthropic"]})
    route = ah._wake_route({"task_id": "t_1", "home_sid": "h", "wake_effort": "low"}, _parent(), {"only": ["x"]})
    assert route == {"effort": "low", "label": "effort low"}


def test_read_provider_routing_tells_an_error_from_an_absent_block(monkeypatch, tmp_path):
    from gateway import run as gw_run

    monkeypatch.setattr(wcr, "read_provider_routing", _REAL_READ)
    monkeypatch.setattr(gw_run, "_gateway_config_home", lambda: tmp_path)
    assert _REAL_READ() is None                                     # no config.yaml at all
    (tmp_path / "config.yaml").write_text("model: x\n")
    assert _REAL_READ() is None                                     # no provider_routing block
    assert wcr.resolve_route("opus", "claude-elr", source="card")["model"] == "claude-opus-5-5"
    (tmp_path / "config.yaml").write_text("provider_routing:\n  models: {m: {only: [a]}}\n")
    assert _REAL_READ() == {"models": {"m": {"only": ["a"]}}}
    (tmp_path / "config.yaml").write_text("provider_routing: [unclosed\n")
    with pytest.raises(Exception):
        _REAL_READ()
    with pytest.raises(wcr.Unusable, match="^provider routing could not be read$"):
        wcr.resolve_route("opus", "claude-elr", source="card")


# item 2: a shutdown or a cancelled claim never starts a parent-route child


def test_restart_interrupt_of_an_override_is_not_rerun(env):
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART

    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "interrupted",
                                        "interrupt_message": _INTERRUPT_REASON_GATEWAY_RESTART}]})

    entry, turns = _run_entry_with(env, "wake-model: opus", fake, draining=True)
    assert len(calls) == 1 and calls[0]["provider"] == "claude-elr"
    claim = ah.BoomerangWakeState().get_claim(entry["key"])
    assert claim["interrupted_by_shutdown"] is True and claim["state"] not in (ah.STATE_TERMINAL,)
    assert not claim.get("wake_model_fallback") and turns == []


def test_restart_message_alone_is_not_rerun(monkeypatch):
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART
    from tools import delegate_tool

    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"status": "interrupted",
                                        "interrupt_message": _INTERRUPT_REASON_GATEWAY_RESTART}]})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake)
    route = wcr.resolve_route("opus", "claude-elr")
    tok = wcr.CURRENT_ROUTE.set(route)
    try:
        assert ah._run_child_delegate(_parent(), "goal", "summary") == ("interrupted", _INTERRUPT_REASON_GATEWAY_RESTART)
    finally:
        wcr.CURRENT_ROUTE.reset(tok)
    assert len(calls) == 1 and "fell_back" not in route


def test_budget_cancelled_claim_is_not_rerun(env):
    """The tick cancels a claim past its budget and interrupts the child: no parent rerun."""
    calls = []

    def fake(**kw):
        calls.append(kw)
        [key] = [k for k, c in ah.BoomerangWakeState()._claims_of(ah.BoomerangWakeState()._read()).items()
                 if c.get("state") == ah.STATE_RUNNING]
        ah.BoomerangWakeState().update_claim(key, state=ah.STATE_CANCELLED)
        return json.dumps({"results": [{"status": "interrupted", "interrupt_message": "cancelled"}]})

    entry, turns = _run_entry_with(env, "wake-model: opus", fake)
    assert len(calls) == 1 and calls[0]["provider"] == "claude-elr"
    claim = ah.BoomerangWakeState().get_claim(entry["key"])
    assert claim["state"] == ah.STATE_CANCELLED and not claim.get("wake_model_fallback")


# item 3: the fallback note is on the claim BEFORE the parent dispatch, so a raise there keeps it


def test_fallback_note_survives_a_failed_parent_dispatch(env):
    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw.get("provider"):
            return json.dumps({"error": "lane down"})
        raise RuntimeError("parent lane exploded")

    entry, turns = _run_entry_with(env, "wake-model: opus", fake)
    assert len(calls) == 2 and "provider" not in calls[1]
    claim = ah.BoomerangWakeState().get_claim(entry["key"])
    assert claim["reason"] == "child failed: dispatch failed: parent lane exploded"
    note = "wake-model opus failed (lane down), ran on parent claude-elr/claude-fable-5-2"
    want = f"{note}; child failed: dispatch failed: parent lane exploded"
    assert turns and all(want in t.split("\n")[0] for t in turns), turns


# item 5: a wake_child section that is not a mapping is surfaced, not ignored


@pytest.mark.parametrize("section", [["opus"], "opus", 7])
def test_malformed_wake_child_section_is_unusable(monkeypatch, section):
    monkeypatch.setattr(wcr, "_delegation_cfg", lambda: {"boomerang": {"wake_child": section}})
    route = ah._wake_route({"task_id": "t_1", "home_sid": "home-1"}, _parent(), {})
    kind = type(section).__name__
    assert route == {"unusable": (
        f"wake-model config not usable (unreadable wake_child section {kind}), ran on parent "
        f"claude-elr/claude-fable-5-2; wake-effort config not usable (unreadable wake_child section {kind}), "
        "ran at the parent's effort")}


def test_absent_wake_child_section_is_the_default():
    for cfg in ({}, {"boomerang": {}}, {"boomerang": {"wake_child": None}}):
        assert wcr.requested(None, "home-1", cfg) == (None, None)


# card comment 443064 item 2: the STATE_RUNNING write and the gate's note write are compare-and-set
# against a cancel; a claim cancelled in between starts no child.


def test_cancel_between_route_lookup_and_running_write_starts_no_child(env):
    real = ah._wake_route

    def cancelling(entry, parent, routing=None):
        ah.BoomerangWakeState().update_claim(entry["key"], state=ah.STATE_CANCELLED)
        return real(entry, parent, routing)

    env["monkeypatch"].setattr(ah, "_wake_route", cancelling)
    calls = []
    entry, turns = _run_entry_with(env, "wake-model: opus", _parent_only(calls))
    assert calls == [] and turns == []
    assert ah.BoomerangWakeState().get_claim(entry["key"])["state"] == ah.STATE_CANCELLED


def test_cancel_after_the_gate_read_starts_no_parent_child(env):
    """The cancel lands after a read of RUNNING: the note write itself must refuse (no rerun)."""
    tid = _card(env, body="wake-model: opus")
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    state = ah.BoomerangWakeState()
    state.update_claim(entry["key"], state=ah.STATE_RUNNING)
    stale = state.get_claim(entry["key"])
    state.update_claim(entry["key"], state=ah.STATE_CANCELLED)
    real_get = ah.BoomerangWakeState.get_claim
    env["monkeypatch"].setattr(ah.BoomerangWakeState, "get_claim", lambda self, k: dict(stale))
    gate = ah._FallBackGate(env["runner"], state, entry["key"], None, "fork")
    assert gate("wake-model opus failed (x), ran on parent p/m") is False
    env["monkeypatch"].setattr(ah.BoomerangWakeState, "get_claim", real_get)
    claim = ah.BoomerangWakeState().get_claim(entry["key"])
    assert claim["state"] == ah.STATE_CANCELLED and not claim.get("wake_model_fallback")


# item 3: a failed note write does not block the parent rerun, and the home still hears the reason


def test_failed_note_write_still_reruns_on_the_parent(env):
    real = ah.BoomerangWakeState.update_claim

    def flaky(self, key, **kw):
        if "wake_model_fallback" in kw and kw.get("wake_model_fallback_ran"):
            raise OSError("disk full")
        return real(self, key, **kw)

    env["monkeypatch"].setattr(ah.BoomerangWakeState, "update_claim", flaky)
    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw.get("provider"):
            return json.dumps({"error": "lane down"})
        return json.dumps({"results": [{"status": "failed", "error": "parent also down"}]})

    entry, turns = _run_entry_with(env, "wake-model: opus", fake)
    assert len(calls) == 2 and "provider" not in calls[1]
    _surfaced(turns, "wake-model opus failed (lane down), ran on parent claude-elr/claude-fable-5-2")


# item 4: card text echoed to the home line is a safe charset; exception text is cut


@pytest.mark.parametrize("body, shown", [
    ("wake-model: <@everyone>", "<invalid value>"),
    ("wake-model: " + "a" * 65, "<invalid value>"),
    ("wake-model: opsu", "opsu"),
])
def test_card_value_echo_is_restricted(env, body, shown):
    calls = []
    _, turns = _run_entry_with(env, body, _parent_only(calls))
    _surfaced(turns, f"wake-model {shown} not usable (")
    assert "@everyone" not in turns[0]
    effort = ah._wake_route({"task_id": "t_1", "home_sid": "h", "wake_effort": "<@here>"}, _parent())
    assert effort["unusable"].startswith("wake-effort <invalid value> not usable (")


def test_exception_text_on_the_wake_line_is_cut(monkeypatch):
    from hermes_cli import model_registry

    def boom(*_a, **_k):
        raise RuntimeError("x" * 500)

    monkeypatch.setattr(model_registry, "resolve", boom)
    route = ah._wake_route({"task_id": "t_1", "home_sid": "h", "wake_model": "opus"}, _parent())
    why = route["unusable"].split(" not usable (", 1)[1].rsplit("), ran on parent", 1)[0]
    assert len(why) <= 160 and why.startswith("not resolvable: xxx")
