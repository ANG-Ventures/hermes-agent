"""A rebuilt session starts on its primary unless the primary really failed
and its fallback is really warm (t_1737b2b7).

Regression for session 20261031_172840_87d0796c, 2026-11-01 00:16:50 PDT: the
first turn after a rebuild activated claude-dtlr/claude-fable-5-1 with no
request to the primary claude-alr/claude-fable-5-1. The episode's cooldown had
expired at 00:08:19; ``fallback_cold`` refused the return because the stored
fallback (dtlr/fable) looked warm, but every recent "fallback" success had been
served by the chain end (alr/opus). The dtlr call that followed read 7% cache.
The same lineage also gained rows keyed primary_provider=claude-dtlr, written by
review forks built on the parent's live fallback route.
"""

from __future__ import annotations

import time

import pytest

from agent import fallback_sticky_store as fss
from agent import fallback_wiring as _fw
from agent.agent_runtime_helpers import restore_primary_runtime as _restore
from tests.agent.test_fallback_policy import (  # noqa: F401 — `wired` is a fixture
    FABLE_CAP,
    _Err,
    _FR,
    _fail,
    _rows,
    _wired_agent,
    wired,
)

PRIMARY = ("claude-alr", "claude-fable-5-1")
STORED_FB = ("claude-dtlr", "claude-fable-5-1")
CHAIN_END = ("claude-alr", "claude-opus-5-5")
CHAIN = [{"provider": STORED_FB[0], "model": STORED_FB[1]},
         {"provider": CHAIN_END[0], "model": CHAIN_END[1]}]


def _session_agent():
    a = _wired_agent(provider=PRIMARY[0], model=PRIMARY[1])
    a._fallback_chain = [dict(e) for e in CHAIN]
    return a


def _incident_episode(home):
    """Primary capped -> stored fallback dtlr -> dtlr pool-full -> chain end
    alr/opus serves; then the cooldown expires (until 8 min ago, failure 82 min
    ago) while alr/opus keeps answering."""
    a = _session_agent()
    assert _fail(a, FABLE_CAP()) is True
    assert (a.provider, a.model) == STORED_FB
    assert _fail(a, _Err("pool at capacity", 503), reason=_FR.overloaded) is True
    assert (a.provider, a.model) == CHAIN_END
    key = _fw.key_for(a)
    st = fss.get(key)
    assert st.active and (st.fallback_provider, st.fallback_model) == STORED_FB
    now = time.time()
    st.until_epoch = now - 8 * 60
    st.entered_at = st.last_failure_epoch = now - 82 * 60
    st.last_fallback_call_epoch = None
    st.last_primary_call_epoch = None
    fss.default_store().put(key, st, now)
    for _ in range(3):  # alr/opus answers the live turns (and review forks)
        _fw.note_success(a, {})
    return a


def test_rebuild_after_cooldown_starts_on_primary_and_first_turn_stays(wired):
    home, _calls = wired
    _incident_episode(home)
    resumes = len(_rows(home, "sticky_resume"))
    failovers = len(_rows(home, "failover"))

    b = _session_agent()                                   # context rebuild
    assert _fw.decide_rebuild_for_agent(b) == "return"
    assert (b.provider, b.model) == PRIMARY
    assert b._fallback_activated is not True
    assert len(_rows(home, "sticky_resume")) == resumes
    assert b._sticky_recovery_row["return_branch"] == "fallback_cold"

    # First turn: turn-start restore, then the fake primary answers.
    _restore(b)
    _fw.note_success(b, {})
    assert (b.provider, b.model) == PRIMARY and b._fallback_activated is not True
    assert len(_rows(home, "failover")) == failovers
    assert fss.get(_fw.key_for(b)).active is False


def test_stored_fallback_success_still_warms_it(wired):
    """The warm-seat design stays: calls on the STORED fallback keep it warm."""
    home, _ = wired
    a = _session_agent()
    assert _fail(a, FABLE_CAP()) is True
    key = _fw.key_for(a)
    st = fss.get(key)
    st.until_epoch = time.time() - 60
    fss.default_store().put(key, st, time.time())
    _fw.note_success(a, {})                                 # served by dtlr/fable
    assert fss.get(key).last_fallback_call_epoch == pytest.approx(time.time(), abs=5)
    b = _session_agent()
    assert _fw.decide_rebuild_for_agent(b) == "resume"
    assert (b.provider, b.model) == STORED_FB


def test_resume_without_recorded_primary_failure_starts_on_primary(wired):
    home, _ = wired
    a = _session_agent()
    assert _fail(a, FABLE_CAP()) is True
    key = _fw.key_for(a)
    st = fss.get(key)
    st.last_failure_epoch = None
    fss.default_store().put(key, st, time.time())
    b = _session_agent()
    assert _fw.decide_rebuild_for_agent(b) == "primary"
    assert (b.provider, b.model) == PRIMARY and b._fallback_activated is not True


# ── (b) the store never records a fallback route as a primary ────────────

def _fork_on(route, sid):
    f = _wired_agent(sid=sid, provider=route[0], model=route[1])
    f._fallback_chain = []
    f._persist_disabled = True
    return f


def test_review_fork_on_parent_fallback_writes_no_primary_row(wired):
    home, _ = wired
    a = _session_agent()
    assert _fail(a, FABLE_CAP()) is True                   # parent on dtlr/fable
    fork = _fork_on(STORED_FB, a.session_id)
    _fw.note_success(fork, {})
    keys = fss.default_store().keys_for_lineage(_fw.key_for(a).lineage_root)
    assert {(k.primary_provider, k.primary_model) for k in keys} == {PRIMARY}


def test_agent_keyed_on_active_episode_fallback_writes_no_row(wired):
    home, _ = wired
    a = _session_agent()
    assert _fail(a, FABLE_CAP()) is True
    rebuilt_on_fb = _wired_agent(sid=a.session_id, provider=STORED_FB[0], model=STORED_FB[1])
    _fw.note_success(rebuilt_on_fb, {})
    assert fss.get(_fw.key_for(rebuilt_on_fb)) is None


def test_fork_on_real_primary_updates_existing_row(wired):
    home, _ = wired
    a = _session_agent()
    _fw.note_success(a, {})
    fork = _fork_on(PRIMARY, a.session_id)
    _fw._last_primary_note.clear()
    _fw.note_success(fork, {"x-pool-served-by": "sub-vps-25"})
    assert fss.get(_fw.key_for(a)).last_primary_seat == "sub-vps-25"


def test_user_primary_that_is_also_a_chain_entry_still_records(wired):
    """A same-provider /model switch keeps its own route in the configured
    chain; with no active episode naming it as a fallback it is a primary."""
    home, _ = wired
    a = _wired_agent(provider=CHAIN_END[0], model=CHAIN_END[1])
    a._fallback_chain = [dict(e) for e in CHAIN]
    _fw.note_success(a, {})
    assert fss.get(_fw.key_for(a)) is not None
