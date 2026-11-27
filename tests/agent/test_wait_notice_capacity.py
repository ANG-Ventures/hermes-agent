"""The wait notice tells the truth when the relay has no capacity, and the harness
falls back early instead of sitting out the 600 s budget (Ace 2026-11-13 20:50 PT).

Replays ``⏳ 12 sessions waiting on claude-dtlr/claude-fable-5-2 (oldest 2429s); this
turn 120s, cut at 600s``: Fable weekly allowance used up on 23 of 25 seats, the dtlr
relay retrying drained seats, every turn cut at 600 s and then "recovered" back to Fable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import fallback_wall_budget as wb
from agent.fork_ext import relay_capacity as rc
from agent.fork_ext import relay_headers as rh

FABLE = "claude-fable-5-2"
DTLR = "http://127.0.0.1:18816/v1"
RESET = "2026-11-15T18:59:59Z"          # Sun 10:59 PT
CHAIN = [{"provider": "claude-btpr", "model": FABLE},
         {"provider": "claude-alr", "model": "claude-opus-5-5"}]


def _relay(monkeypatch, *, exhausted=23, of=25, eligible=2, inflight=None, model=FABLE):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if url.endswith("/health"):
            return {"fable_allowance": {"exhausted": exhausted, "of": of, "earliest_reset": RESET}}
        if "/eligibility?" in url:
            return {"model": model, "eligible_count": eligible, "model_eligible": eligible > 0}
        return None

    monkeypatch.setattr(rc, "fetch_json", fetch)
    monkeypatch.setattr(rh, "fetch_relay_stage", lambda base, key, **kw: inflight)
    monkeypatch.setattr(wb, "_knobs", lambda: (600.0, 120.0))
    monkeypatch.setattr(wb, "_dedupe_on", lambda: True)
    monkeypatch.setattr(wb, "post_logs", lambda text: None)
    return calls


def _agent(session="s1", provider="claude-dtlr", model=FABLE, chain=CHAIN):
    return SimpleNamespace(provider=provider, model=model, base_url=DTLR, session_id=session,
                           _fallback_chain=list(chain), _fallback_index=0, _wall_budget=None)


RETRYING = {"stage": "upstream_ttfb", "seat": "sub-vps-3", "since_ms": 4000,
            "attempt_ms": 4000, "request_ms": 62000}


def test_shared_notice_names_the_exhausted_allowance_not_a_queue(monkeypatch):
    _relay(monkeypatch, inflight=RETRYING)
    agents = [_agent(f"s{i}") for i in range(12)]
    wb.arm(agents[0], now=-2309.0)
    for a in agents[1:]:
        wb.arm(a, now=0.0)
    for a in agents:
        wb.note_quiet(a, 300, now=0.0)
    text = wb.notice_due(agents[-1], now=120.0)
    print(text)
    assert text == ("⏳ 12 sessions waiting on claude-dtlr/claude-fable-5-2 (oldest 2429s): "
                    "relay retrying seats (Fable allowance used up on 23/25 seats, 2 eligible, "
                    "resets Sun 10:59); this turn 120s, cut at 600s")


def test_solo_notice_names_the_allowance(monkeypatch):
    _relay(monkeypatch, inflight=RETRYING)
    a = _agent()
    wb.arm(a, now=0.0)
    text = wb.notice_due(a, now=120.0)
    print(text)
    assert text == ("⏳ waiting on claude-dtlr/claude-fable-5-2: relay retrying seats (Fable "
                    "allowance used up on 23/25 seats, 2 eligible, resets Sun 10:59); 120s "
                    "elapsed; cut to next lane at 600s")


def test_solo_notice_without_retry_evidence_still_names_the_allowance(monkeypatch):
    _relay(monkeypatch, inflight=None)
    a = _agent()
    wb.arm(a, now=0.0)
    assert wb.notice_due(a, now=120.0) == (
        "⏳ waiting on claude-dtlr/claude-fable-5-2: Fable allowance used up on 23/25 seats, "
        "2 eligible, resets Sun 10:59; 120s elapsed; cut to next lane at 600s")


def test_normal_queue_keeps_todays_wording(monkeypatch):
    _relay(monkeypatch, exhausted=0, eligible=20,
           inflight={"stage": "queued", "seat": "sub-vps-7", "since_ms": 95000})
    agents = [_agent("s1"), _agent("s2")]
    for a in agents:
        wb.arm(a, now=0.0)
        wb.note_quiet(a, 300, now=0.0)
    shared = wb.notice_due(agents[-1], now=120.0)
    print(shared)
    assert shared == "⏳ 2 sessions waiting on claude-dtlr/claude-fable-5-2 (oldest 120s); this turn 120s, cut at 600s"
    wb.clear_registry()
    solo = _agent("s3")
    wb.arm(solo, now=0.0)
    text = wb.notice_due(solo, now=120.0)
    print(text)
    assert text == "⏳ queued on sub-vps-7 for 95s: 120s elapsed; cut at 600s"


def test_unknown_capacity_is_never_read_as_no_capacity(monkeypatch):
    _relay(monkeypatch, inflight=None)
    monkeypatch.setattr(rc, "fetch_json", lambda url, timeout: None)
    a = _agent()
    wb.arm(a, now=0.0)
    assert not wb.should_cut_attempt(a, now=30.0)
    assert "allowance" not in (wb.notice_due(a, now=120.0) or "")


@pytest.mark.parametrize("exhausted,eligible", [(25, 0), (23, 0), (25, 3)])
def test_exhausted_model_cuts_early_once_and_names_the_quota(monkeypatch, exhausted, eligible):
    _relay(monkeypatch, exhausted=exhausted, eligible=eligible)
    a = _agent()
    wb.arm(a, now=0.0)
    assert not wb.should_cut_attempt(a, now=5.0), "a fast first byte never probes"
    assert wb.should_cut_attempt(a, now=12.0)
    exc = wb.cut_exception(a, FABLE, lane="claude-dtlr", elapsed_s=12.0, pool_headers={})
    assert isinstance(exc, wb.RelayModelExhausted)
    assert isinstance(exc, wb.FallbackWallBudgetExceeded)   # same fall-back-now verdict
    assert exc.relay_headers == {"x-pool-fable-allowance": f"{exhausted}/25; earliest_reset={RESET}"}
    # once per lane: the same lane is not cut again; the budget cut still fires at 600 s
    assert not wb.should_cut_attempt(a, now=30.0)
    assert wb.should_cut_attempt(a, now=600.0)
    assert type(wb.cut_exception(a, FABLE)) is wb.FallbackWallBudgetExceeded


def test_exhausted_model_with_nothing_ahead_is_not_cut(monkeypatch):
    _relay(monkeypatch, exhausted=25, eligible=0)
    a = _agent(chain=[])
    wb.arm(a, now=0.0)
    assert not wb.should_cut_attempt(a, now=12.0)
    assert wb.notice_due(a, now=120.0) == (
        "⏳ waiting on claude-dtlr/claude-fable-5-2: Fable allowance used up on all 25 seats, "
        "resets Sun 10:59; 120s elapsed; nothing left in the fallback chain")


def test_non_relay_lane_never_probes(monkeypatch):
    calls = _relay(monkeypatch, exhausted=25, eligible=0)
    a = _agent(provider="cpa")
    wb.arm(a, now=0.0)
    assert not wb.should_cut_attempt(a, now=30.0)
    assert calls == []


def test_capacity_probe_is_shared_and_cached(monkeypatch):
    calls = _relay(monkeypatch, exhausted=23, eligible=2)
    for i in range(12):
        assert rc.model_capacity(DTLR, FABLE, now=100.0 + i)["exhausted"] == 23
    assert len(calls) == 2   # one /health + one /eligibility for 12 readers
    rc.model_capacity(DTLR, FABLE, now=100.0 + rc.CACHE_TTL_S + 1)
    assert len(calls) == 4


def test_non_fable_model_reads_eligibility_only(monkeypatch):
    calls = _relay(monkeypatch, eligible=0, model="claude-opus-5-5")
    cap = rc.model_capacity(DTLR, "claude-opus-5-5")
    assert cap["eligible"] == 0 and cap["of"] is None
    assert rc.exhausted(cap)
    assert rc.capacity_phrase(cap) == "no seat can serve claude-opus-5-5"
    assert not any(u.endswith("/health") for u in calls)


def test_non_loopback_relay_is_never_probed(monkeypatch):
    calls = _relay(monkeypatch)
    assert rc.model_capacity("https://api.example.com/v1", FABLE) is None
    assert calls == []


def test_early_cut_classifies_as_relay_quota_and_renders_the_fable_banner(monkeypatch):
    """The cut is stashed like a relay refusal: ``quota_model``, relay-sourced, with the
    allowance header, so the fallback banner says ``Fable weekly limit`` + the counts."""
    from agent import fallback_events as fbe
    from agent.fallback_policy import format_cause_rider, head_label_override

    exc = wb.RelayModelExhausted("relay has no capacity", capacity={
        "model": FABLE, "exhausted": 25, "of": 25, "earliest_reset": RESET, "eligible": 0},
        lane="claude-dtlr", elapsed_s=12.0, pool_headers={})
    agent = SimpleNamespace(provider="claude-dtlr", model=FABLE, session_id="s1",
                            _current_turn_id="s1:1")
    fbe.stash_api_error(agent, exc, None)
    assert fbe.pending_trigger_class(agent) == "quota_model"
    row = fbe.build_row(agent, "failover", from_provider="claude-dtlr", from_model=FABLE,
                        to_provider="claude-btpr", to_model=FABLE, reason="rate_limit")
    assert (row["trigger_class"], row["class_source"]) == ("quota_model", "relay_probe")
    assert row["fable_allowance"]["exhausted"] == 25
    assert head_label_override(row) == "Fable weekly limit"
    assert "Fable weekly allowance (7d_oi) exhausted on 25/25 seats" in format_cause_rider(row)


def test_error_classifier_falls_back_now(monkeypatch):
    from agent.error_classifier import classify_api_error

    exc = wb.RelayModelExhausted("relay has no capacity", capacity={"eligible": 0})
    c = classify_api_error(exc, provider="claude-dtlr", model=FABLE)
    assert c.should_fallback and not c.retryable


def test_relay_capacity_json_shape_matches_the_live_relay(monkeypatch):
    """The fields read here are the ones claude-pool emits (measured 2026-11-13 on :18816)."""
    health = json.loads('{"fable_allowance": {"exhausted": 17, "of": 26, '
                        '"earliest_reset": "2026-11-15T18:59:59Z"}}')
    elig = json.loads('{"model": "claude-fable-5-2", "eligible": ["sub-vps-11", "sub-vps-23"], '
                      '"eligible_count": 2, "model_eligible": true}')
    monkeypatch.setattr(rc, "fetch_json",
                        lambda url, timeout: health if url.endswith("/health") else elig)
    cap = rc.model_capacity(DTLR, FABLE)
    assert cap == {"model": FABLE, "exhausted": 17, "of": 26,
                   "earliest_reset": "2026-11-15T18:59:59Z", "eligible": 2}
    assert rc.capacity_phrase(cap) == "Fable allowance used up on 17/26 seats, 2 eligible, resets Sun 10:59"


# -- no "Model recovery" back to a model the relay still reports exhausted ---------

def _restoring_agent():
    return SimpleNamespace(provider="claude-alr", model="claude-opus-5-5",
                           base_url="http://127.0.0.1:18810/v1", session_id="s1",
                           _primary_runtime={"provider": "claude-dtlr", "model": FABLE,
                                             "base_url": DTLR},
                           _rate_limited_until=0, _current_turn_id="s1:2")


@pytest.mark.parametrize("exhausted,eligible", [(25, 0), (23, 0), (25, 2)])
def test_turn_start_restore_holds_while_relay_reports_model_exhausted(monkeypatch, exhausted, eligible):
    from agent import fallback_wiring as fw

    _relay(monkeypatch, exhausted=exhausted, eligible=eligible)
    d = fw.restore_allowed(_restoring_agent(), probe=True)
    assert d.allowed is False
    assert d.reason.startswith("relay: no capacity for claude-fable-5-2 (Fable allowance used up")


@pytest.mark.parametrize("case", ["capacity", "unknown"])
def test_turn_start_restore_unchanged_when_relay_has_capacity_or_is_silent(monkeypatch, case):
    from agent import fallback_wiring as fw

    _relay(monkeypatch, exhausted=23, eligible=2)
    if case == "unknown":
        monkeypatch.setattr(rc, "fetch_json", lambda url, timeout: None)
    monkeypatch.setattr(fw, "sticky_policy_enabled", lambda: False)
    assert fw.restore_allowed(_restoring_agent(), probe=True).allowed is True
    # the cheap (no-I/O) form never probes
    monkeypatch.setattr(rc, "fetch_json", lambda url, timeout: pytest.fail("probed"))
    rc.clear_cache()
    assert fw.restore_allowed(_restoring_agent(), probe=False).allowed is True


def _alr_primary_agent():
    """A primary on an /eligibility relay lane (claude-alr), currently on its fallback."""
    return SimpleNamespace(provider="claude-btpr", model=FABLE,
                           base_url="http://127.0.0.1:18820/v1", session_id="s1",
                           _primary_runtime={"provider": "claude-alr", "model": FABLE,
                                             "base_url": DTLR},
                           _rate_limited_until=0, _current_turn_id="s1:2")


def test_eligibility_fn_returns_the_relays_own_answer_untouched(monkeypatch):
    """Prism P2 (8020d4ad): the capacity hold lives in ``_primary_capacity_hold`` only;
    ``/eligibility`` answers are no longer rewritten from the capacity probe."""
    from agent import fallback_policy as fp
    from agent import fallback_wiring as fw

    _relay(monkeypatch, exhausted=25, eligible=0)
    raw = fp.parse_eligibility({"instance_id": "6:18816", "bound_eligible": True,
                                "model_eligible": True, "snapshot_age_s": 1})
    monkeypatch.setattr(fp, "fetch_eligibility", lambda *a, **k: raw)
    assert not hasattr(fw, "_with_relay_capacity")
    assert fw._eligibility_fn(_alr_primary_agent())() is raw


@pytest.mark.parametrize("exhausted,eligible,returns", [(25, 0, False), (23, 2, True)])
def test_fallback_failed_never_returns_to_an_exhausted_primary(monkeypatch, exhausted, eligible, returns):
    """The fallback fails with a quota class and /eligibility says the primary seat is
    fine: the return to the primary is still held while the relay reports the primary
    model exhausted (the gate the removed overlay used to cover)."""
    from agent import fallback_policy as fp
    from agent import fallback_wiring as fw
    from agent import agent_runtime_helpers as arh

    _relay(monkeypatch, exhausted=exhausted, eligible=eligible)
    eligible_answer = fp.parse_eligibility({"instance_id": "6:18816", "bound_eligible": True,
                                            "model_eligible": True, "snapshot_age_s": 1})
    monkeypatch.setattr(fp, "fetch_eligibility", lambda *a, **k: eligible_answer)
    st = SimpleNamespace(active=True, ff_disabled_until=0.0, primary_provider="claude-alr")
    monkeypatch.setattr(fw, "sticky_policy_enabled", lambda: True)
    monkeypatch.setattr(fw, "failure_class", lambda agent, reason: ("quota_seat", None))
    monkeypatch.setattr(fw, "on_fallback", lambda agent: True)
    monkeypatch.setattr(fw, "key_for", lambda agent: SimpleNamespace(primary_provider="claude-alr"))
    monkeypatch.setattr(fw, "_state", lambda key: st)
    restored = []
    monkeypatch.setattr(arh, "restore_primary_runtime", lambda agent, **k: restored.append(k) or True)
    plan = fw.pre_failover(_alr_primary_agent(), "rate_limit")
    assert plan.returned is returns
    assert bool(restored) is returns


def test_budget_cut_episode_holds_on_an_ineligible_relay_answer():
    """A 600 s cut (``no_first_byte_budget``) on a relay lane is held like a quota
    episode while the relay says no seat can serve the model."""
    from agent import fallback_policy as fp

    T0 = 1_800_000_000.0
    st = fp.StickyState(
        primary_provider="claude-alr", primary_model=FABLE,
        fallback_provider="claude-btpr", fallback_model="claude-opus-5-5",
        cls=fp.NO_FIRST_BYTE_BUDGET_CLASS, until_epoch=T0 - 1, active=True,
        entered_at=T0 - 7200, last_fallback_call_epoch=T0 - 7200,
        last_compaction_epoch=None, last_primary_call_epoch=T0 - 7300,
        last_primary_seat="sub-vps-3")
    no = fp.parse_eligibility({"instance_id": "6:18816", "bound_eligible": False,
                               "model_eligible": False, "snapshot_age_s": 1})
    yes = fp.parse_eligibility({"instance_id": "6:18816", "bound_eligible": False,
                                "model_eligible": True, "snapshot_age_s": 1})
    held = fp.restore_allowed(st, T0, probe=True, primary_provider="claude-alr",
                              eligibility=lambda: no)
    assert (held.allowed, held.reason) == (False, fp.RELAY_MODEL_INELIGIBLE_REASON)
    back = fp.restore_allowed(st, T0, probe=True, primary_provider="claude-alr",
                              eligibility=lambda: yes)
    assert back.allowed is True and back.branch == "fallback_cold"


def test_rebuilt_session_resumes_its_fallback_while_primary_is_exhausted():
    """The re-init leg (cache evicted): a dtlr/Fable primary with a budget-cut episode
    resumes the fallback instead of starting on Fable, while the relay says exhausted."""
    from agent import fallback_policy as fp
    from agent import fallback_sticky_store as fss

    T0 = 1_800_000_000.0
    store = fss.StickyStore()
    key = fss.StickyKey.build("lineage-1", "claude-dtlr", FABLE)
    st = fp.StickyState(
        primary_provider="claude-dtlr", primary_model=FABLE,
        fallback_provider="claude-btpr", fallback_model=FABLE,
        cls=fp.NO_FIRST_BYTE_BUDGET_CLASS, until_epoch=T0 - 1, active=True,
        entered_at=T0 - 60, last_fallback_call_epoch=T0 - 60,
        last_compaction_epoch=None, last_primary_call_epoch=T0 - 700,
        last_primary_seat=None)
    from unittest import mock
    with mock.patch.object(fp._store_mod, "get", lambda k, s: st):
        held = fp.decide_rebuild(store, key, T0, live_session_id="s1",
                                 capacity_hold=lambda: "relay: no capacity for claude-fable-5-2")
        assert held.action == "resume" and held.decision.reason.startswith("relay: no capacity")
        with mock.patch.object(fp, "record_return", lambda *a, **k: st):
            free = fp.decide_rebuild(store, key, T0, live_session_id="s1", capacity_hold=lambda: None)
        assert free.action == "return"


# -- Prism 8020d4ad: redirect escape + probe stampede ----------------------------

def _serve(handler_cls):
    import http.server
    import threading

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_never_follows_a_redirect_off_the_checked_origin(monkeypatch):
    """Prism P1: /health 302 -> another origin must not be requested. The repro used
    ``Location: http://169.254.169.254/latest/meta-data/``; a second loopback server
    stands in for it so the test can see whether the opener went there."""
    import http.server

    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = json.dumps({"fable_allowance": {"exhausted": 25, "of": 25}, "model": FABLE,
                               "eligible_count": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    target = _serve(Target)
    elsewhere = f"http://127.0.0.1:{target.server_port}/latest/meta-data/"

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", elsewhere)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    relay = _serve(Redirector)
    try:
        monkeypatch.setattr(rc, "fetch_json", rc._http_get_json)
        monkeypatch.setattr(rc, "PROBE_TIMEOUT_S", 2.0)
        cap = rc.model_capacity(f"http://127.0.0.1:{relay.server_port}/v1", FABLE)
        assert hits == []          # the redirect target was never requested
        assert cap is None         # a redirect is unknown capacity, never "exhausted"
        assert rc._http_get_json(f"http://127.0.0.1:{relay.server_port}/health", 2.0) is None
        assert hits == []
    finally:
        relay.shutdown()
        target.shutdown()


def test_concurrent_waiters_share_one_probe(monkeypatch):
    """Prism P2: N sessions asking at once while the cache is cold cause one probe
    (one /health + one /eligibility), and all of them get its answer."""
    import threading
    import time as _t

    calls = []
    gate = threading.Event()

    def slow_fetch(url, timeout):
        calls.append(url)
        gate.wait(5)
        if url.endswith("/health"):
            return {"fable_allowance": {"exhausted": 25, "of": 25, "earliest_reset": RESET}}
        return {"model": FABLE, "eligible_count": 0, "model_eligible": False}

    monkeypatch.setattr(rc, "fetch_json", slow_fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(rc.model_capacity(DTLR, FABLE)))
               for _ in range(8)]
    for t in threads:
        t.start()
    deadline = _t.monotonic() + 5
    while not calls and _t.monotonic() < deadline:
        _t.sleep(0.01)
    _t.sleep(0.3)                  # every thread has reached model_capacity by now
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 2, calls
    assert len(results) == 8 and all(r == results[0] for r in results)
    assert rc.exhausted(results[0])


def test_inflight_probe_that_raises_releases_waiters(monkeypatch):
    def boom(url, timeout):
        raise RuntimeError("seam failure")

    monkeypatch.setattr(rc, "fetch_json", boom)
    with pytest.raises(RuntimeError):
        rc.model_capacity(DTLR, FABLE)
    assert rc._INFLIGHT == {}
    assert rc.model_capacity(DTLR, FABLE) is None   # cached unknown, no stuck waiter
