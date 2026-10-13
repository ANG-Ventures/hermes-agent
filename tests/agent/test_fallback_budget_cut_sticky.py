"""A no-first-byte wall-budget cut arms the sticky fallback episode (t_3e4fa6ef).

2026-10-13 11:47-12:01 PT: alr held a fable turn 606 s with no byte, the budget cut
moved it to dtlr, dtlr answered, and the next turn restored alr in the same second
(no sticky state -> ``no active sticky state``). These drive the REAL
``try_activate_fallback`` / ``restore_primary_runtime`` / gateway rebuild decision
against a temp HERMES_HOME; only the provider resolver and the relay's
``/eligibility`` answer are faked (the shared ``wired`` fixture).
"""

from __future__ import annotations

import time

import pytest

from agent import fallback_policy as fp
from agent import fallback_sticky_store as fss
from agent import fallback_wiring as fw
from agent import fallback_events as fbe
from agent.error_classifier import FailoverReason
from agent.fallback_wall_budget import FallbackWallBudgetExceeded
from tests.agent.test_fallback_policy import (  # noqa: F401  (wired is a fixture)
    CODEX, CONN, ELIG, FABLE, OPUS, _activate, _restore, _rows, _state, _wired_agent, wired,
)

BUDGET = fp.NO_FIRST_BYTE_BUDGET_CLASS
NOT_ELIGIBLE = fp.Eligibility(bound_seat=None, bound_eligible=False, model_eligible=False,
                              bound_expires_in_s=None, bound_idle_s=None, bound_expiry=None,
                              snapshot_age_s=1.0, instance_id="pid:1 port:18811")


def _cut(agent, lane=FABLE[0]):
    exc = FallbackWallBudgetExceeded("cut", lane=lane, elapsed_s=606.0, pool_headers={})
    fbe.stash_api_error(agent, exc, None, None, elapsed_s=606.0)
    return _activate(agent, reason=FailoverReason.pool_stalled)


def _seed_primary_seat(agent, seat="sub-vps-6", ago=60.0):
    """The primary served from ``seat`` ``ago`` seconds before the episode."""
    k = fw.key_for(agent)
    fp.note_primary_success(fss.default_store(), k, time.time() - ago,
                            provider=FABLE[0], headers={"x-pool-served-by": seat})


def _after_until(agent):
    k = fw.key_for(agent)
    st = fss.get(k)
    st.until_epoch = time.time() - 30
    fss.default_store().put(k, st, time.time())


def test_budget_cut_arms_sticky_and_next_turn_stays_on_fallback(wired):
    """The incident: cut on alr -> fallback; the next turn must NOT restore alr."""
    home, calls = wired
    a = _wired_agent()
    assert _cut(a) is True
    assert (a.provider, a.model) == OPUS
    st = _state(a)
    assert st is not None and st.active and st.cls == BUDGET
    assert st.until_epoch - time.time() == pytest.approx(
        fp.compute_cooldown_s("conn", 0), abs=fp.compute_cooldown_s("conn", 0) * 0.11)
    [row] = _rows(home, "failover")
    # Ledger class and the loud label survive: not relabelled quota / conn.
    assert row["trigger_class"] == BUDGET
    assert row["sticky_until_epoch"] == pytest.approx(st.until_epoch)
    assert "no first byte" in row["notice_text"]
    assert calls["gate"] == 0  # not a quota event: no quota-registry gate
    assert _restore(a) is False  # same agent, next turn
    assert (a.provider, a.model) == OPUS
    [ref] = _rows(home, "restore_refused")[-1:]
    assert "until" in (ref.get("reason") or ref.get("err_head") or str(ref))


def test_after_cooldown_model_ineligible_primary_stays_on_fallback(wired, monkeypatch):
    """Model-aware gate: alr /eligibility?model=fable said eligible_count=0 at 12:09
    while /health said 25. Past ``until`` the warm-fallback copy is kept."""
    home, _ = wired
    a = _wired_agent()
    _seed_primary_seat(a)
    assert _cut(a) is True
    fp.note_fallback_success(fss.default_store(), fw.key_for(a), time.time(), a.session_id)
    _after_until(a)
    monkeypatch.setattr(fw, "_eligibility_fn", lambda agent: (lambda: NOT_ELIGIBLE))
    assert _restore(a) is False
    assert (a.provider, a.model) == OPUS


def test_after_cooldown_recovered_warm_primary_returns(wired):
    """Positive control: the primary's bound seat is eligible again (existing
    warm_seat contract) -> the turn boundary returns to the primary."""
    home, _ = wired
    a = _wired_agent()
    _seed_primary_seat(a, seat=ELIG.bound_seat)
    assert _cut(a) is True
    fp.note_fallback_success(fss.default_store(), fw.key_for(a), time.time(), a.session_id)
    _after_until(a)
    assert _restore(a) is True
    assert (a.provider, a.model) == FABLE
    [rec] = _rows(home, "recovery")
    assert rec["return_branch"] == "warm_seat" and rec["trigger_class"] == BUDGET
    assert fss.get(fw.key_for(a)).active is False


def test_rebuild_resumes_fallback_and_model_switch_wins(wired):
    """Agent re-init (cache eviction / restart) inside the episode resumes the
    fallback; an explicit /model (user_route) still starts on the primary."""
    home, _ = wired
    a = _wired_agent()
    assert _cut(a) is True
    b = _wired_agent()
    assert fw.decide_rebuild_for_agent(b) == "resume"
    assert (b.provider, b.model) == OPUS
    c = _wired_agent()
    assert fw.decide_rebuild_for_agent(c, user_route=True) == "primary"
    assert fw.on_fallback(c) is False
    assert fss.get(fw.key_for(c)).active is False
    d = _wired_agent()  # /new-style fresh lineage: nothing to resume
    d.session_id = "20260926_100000_ffff"
    d._current_turn_id = f"{d.session_id}:{d.session_id}:t1"
    assert fw.decide_rebuild_for_agent(d) == "primary"


def test_failing_fallback_still_walks_the_chain(wired):
    """Never trap the user: a sticky budget episode does not stop the chain walk
    when the fallback itself fails (budget cut or conn)."""
    home, _ = wired
    a = _wired_agent()
    a._fallback_chain = [{"provider": OPUS[0], "model": OPUS[1]},
                         {"provider": CODEX[0], "model": CODEX[1]}]
    assert _cut(a) is True
    before = _state(a)
    assert _cut(a, lane=OPUS[0]) is True
    assert (a.provider, a.model) == CODEX
    after = _state(a)  # B1: the fallback's failure does not re-arm the primary
    assert (after.cls, after.n, after.until_epoch) == (before.cls, before.n, before.until_epoch)


def _same_model_agent():
    a = _wired_agent(provider="claude-alr", model="claude-fable-5-1")
    a._fallback_chain = [
        {"provider": "claude-dtlr", "model": a.model},
        {"provider": "claude-btpr", "model": a.model},
        {"provider": "claude-bpr", "model": a.model},
    ]
    return a


def test_same_model_alr_dtlr_resume_and_chain_walk(wired, monkeypatch):
    home, _ = wired
    a = _same_model_agent()
    assert _cut(a, lane="claude-alr")
    assert (a.provider, a.model) == ("claude-dtlr", "claude-fable-5-1")
    state = _state(a)
    assert state and state.active and state.cls == BUDGET
    assert not _restore(a)
    fp.note_fallback_success(fss.default_store(), fw.key_for(a), time.time(), a.session_id)
    _after_until(a)
    monkeypatch.setattr(fw, "_eligibility_fn", lambda agent: (lambda: NOT_ELIGIBLE))
    assert not _restore(a)
    rebuilt = _same_model_agent()
    assert fw.decide_rebuild_for_agent(rebuilt) == "resume"
    assert (rebuilt.provider, rebuilt.model) == ("claude-dtlr", "claude-fable-5-1")
    before = _state(rebuilt)
    before_until = before.until_epoch
    assert _cut(rebuilt, lane="claude-dtlr")
    assert (rebuilt.provider, rebuilt.model) == ("claude-btpr", "claude-fable-5-1")
    assert _state(rebuilt).until_epoch == before_until
    assert not _rows(home, "recovery")


def test_same_model_user_route_and_new_lineage_win(wired):
    a = _same_model_agent()
    assert _cut(a, lane="claude-alr")
    fresh = _same_model_agent()
    fresh.session_id = "20261013_120000_fresh"
    fresh._current_turn_id = f"{fresh.session_id}:{fresh.session_id}:t1"
    assert fw.decide_rebuild_for_agent(fresh) == "primary"
    explicit = _same_model_agent()
    assert fw.decide_rebuild_for_agent(explicit, user_route=True) == "primary"
    assert (explicit.provider, explicit.model) == ("claude-alr", "claude-fable-5-1")
    assert not fss.get(fw.key_for(explicit)).active


def test_unseated_primary_returns_after_cooldown(tmp_path):
    """A primary with no seat signal (not relay, not pin) is not held on the
    fallback forever: like conn, it returns on the transient branch after until."""
    store = fss.StickyStore(db_path=tmp_path / "t.db")
    route = ("openrouter", "moonshotai/kimi-k3")
    key = fss.StickyKey.build("root-sid", *route)
    t0 = 1_790_000_000.0
    st = fp.arm_sticky(store, key, cls=BUDGET, now=t0, failing=route, fallback=OPUS, jitter=1.0)
    fp.note_fallback_success(store, key, st.until_epoch + 50, "sid-a")
    st = store.get(key)
    assert not fp.restore_allowed(st, st.until_epoch - 1, probe=True,
                                  primary_provider="openrouter").allowed
    d = fp.restore_allowed(st, st.until_epoch + 60, probe=True, primary_provider="openrouter")
    assert d.allowed and d.branch == fp.TRANSIENT_BRANCH
    assert "no first byte" in fp.format_recovery_rider(
        {"return_branch": d.branch, "trigger_class": BUDGET, "dwell_s": 60, "dwell_turns": 1})
