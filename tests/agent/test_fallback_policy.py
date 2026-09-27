"""Same-family fallback spec rev12, Phase 2 (§4.1-4.3) — policy core.

Every test names the §5 Phase 2 bullet it covers. Bullets that need the
hot-path wiring (outgoing request after a resume, route-change log lines,
announce delivery) are out of this module's scope; see the PR body.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from agent import fallback_policy as fp
from agent import fallback_sticky_store as fss
from agent.fallback_sticky_store import StickyKey, StickyState, StickyStore, StoreUnreadable

UTC = dt.timezone.utc
T0 = 1_790_000_000.0
FABLE = ("claude-bpr", "claude-fable-5-1")
OPUS = ("claude-bpr", "claude-opus-5-5")


@pytest.fixture
def store(tmp_path):
    return StickyStore(db_path=tmp_path / "turns.db")


@pytest.fixture
def key():
    return StickyKey.build("root-sid", *FABLE)


def _arm(store, key, cls="conn", now=T0, **kw):
    kw.setdefault("jitter", 1.0)
    return fp.arm_sticky(store, key, cls=cls, now=now, failing=FABLE, fallback=OPUS, **kw)


# ── fake relay AffinityMap (idle TTL refreshed on hit + hard TTL) ─────────

class FakeAffinityMap:
    """Mirrors the relay binding life the spec measured: idle TTL 1800 s
    refreshed on a hit, hard TTL 21600 s from bind; key ``sid|fable`` under
    fable_reserve_mode=enforce, bare ``sid`` under off."""

    def __init__(self, mode="enforce", idle_ttl=1800.0, hard_ttl=21600.0):
        self.mode, self.idle_ttl, self.hard_ttl = mode, idle_ttl, hard_ttl
        self.bind_at, self.hit_at, self.seat = {}, {}, {}

    def key(self, sid, fable):
        return f"{sid}|fable" if (fable and self.mode == "enforce") else sid

    def serve(self, sid, fable, seat, now, rebind=False):
        k = self.key(sid, fable)
        if k not in self.seat or rebind:
            self.seat[k], self.bind_at[k] = seat, now
        self.hit_at[k] = now

    def eligibility(self, sid, now, eligible=True):
        k = self.key(sid, True)
        if k not in self.seat:
            return {"instance_id": "1:18811", "bound_seat": None, "bound_eligible": False,
                    "model_eligible": eligible, "snapshot_age_s": 1}
        idle_left = self.idle_ttl - (now - self.hit_at[k])
        hard_left = self.hard_ttl - (now - self.bind_at[k])
        left = min(idle_left, hard_left)
        if left <= 0:
            return {"instance_id": "1:18811", "bound_seat": None, "bound_eligible": False,
                    "model_eligible": eligible, "snapshot_age_s": 1}
        return {"instance_id": "1:18811", "bound_seat": self.seat[k], "bound_eligible": eligible,
                "model_eligible": eligible, "bound_expires_in_s": left, "snapshot_age_s": 1}


def _elig(obj):
    parsed = fp.parse_eligibility(obj)
    return lambda: parsed


def _sticky_on_fallback(store, key, *, primary_seat="sub-vps-6", last_primary=None,
                        cls="conn", now=T0):
    fp.note_primary_success(store, key, last_primary if last_primary is not None else now - 60,
                            provider="claude-bpr", headers={"x-pool-served-by": primary_seat})
    _arm(store, key, cls=cls, now=now)
    fp.note_fallback_success(store, key, now + 1, "sid-a")
    return store.get(key)


# ── class -> cooldown table, doubling, B3 ─────────────────────────────────

def test_class_cooldown_table():
    assert fp.compute_cooldown_s("conn", 0) == 120
    assert fp.compute_cooldown_s("pool_pressure", 0, retry_after_s=7) == 7
    assert fp.compute_cooldown_s("pool_pressure", 0) == 2
    assert fp.compute_cooldown_s("quota_seat", 0, retry_after_s=30) == 60
    assert fp.compute_cooldown_s("quota_seat", 0, retry_after_s=300) == 300
    assert fp.compute_cooldown_s("quota_model", 0) == 6 * 3600
    for legacy in ("rate_upstream", "refusal", "auth", "unclassified"):
        assert fp.compute_cooldown_s(legacy, 0) is None
    assert set(fp.TRIGGER_CLASSES) >= set(fp.COOLDOWN_TABLE)


@pytest.mark.parametrize("cls,kw,ceiling", [
    ("conn", {}, 1800), ("pool_pressure", {"retry_after_s": 5}, 300),
    ("quota_seat", {"retry_after_s": 60}, 1800), ("quota_model", {}, 24 * 3600),
])
def test_doubling_reaches_per_class_ceiling(cls, kw, ceiling):
    seq = [fp.compute_cooldown_s(cls, n, **kw) for n in range(12)]
    assert seq[1] == 2 * seq[0]
    assert seq == sorted(seq)
    assert seq[-1] == ceiling and max(seq) == ceiling


def test_jitter_bounds():
    assert fp.default_jitter(lambda: 0.0) == 1.0
    assert fp.default_jitter(lambda: 0.999) < 1.1


def test_content_policy_keeps_shared_60s_B3():
    assert "refusal" not in fp.STICKY_CLASSES
    assert fp.compute_cooldown_s("refusal", 5) is None  # legacy formula, shared default
    assert fp.LEGACY_DEFAULT_COOLDOWN_S == 60.0
    assert not fp.skips_quota_gate("refusal")


def test_quota_gate_gated_on_class():
    for cls in ("quota_seat", "conn", "pool_pressure"):
        assert fp.skips_quota_gate(cls)
    for cls in ("quota_model", "rate_upstream", "unclassified"):
        assert not fp.skips_quota_gate(cls)


# ── B1: same-provider fallback failure does not touch the primary clock ──

def test_429_on_same_provider_fallback_does_not_rearm_primary_B1(store, key):
    st = _arm(store, key, cls="quota_seat", retry_after_s=60)
    got = fp.arm_sticky(store, key, cls="quota_seat", now=T0 + 30, failing=OPUS,
                        fallback=OPUS, jitter=1.0)
    assert got is None
    after = store.get(key)
    assert (after.until_epoch, after.n) == (st.until_epoch, st.n)
    assert not fp.should_arm(OPUS, FABLE) and fp.should_arm(FABLE, FABLE)


# ── until is epoch, survives rebuild; restore_refused while sticky ───────

def test_until_is_epoch_and_survives_rebuild(tmp_path, key):
    s1 = StickyStore(db_path=tmp_path / "turns.db")
    st = fp.arm_sticky(s1, key, cls="conn", now=T0, failing=FABLE, fallback=OPUS, jitter=1.0)
    assert st.until_epoch == T0 + 120  # wall-clock epoch, not monotonic
    s2 = StickyStore(db_path=tmp_path / "turns.db")  # fresh process: empty map
    assert s2.get(key).until_epoch == T0 + 120


def test_restore_refused_while_sticky(store, key):
    st = _sticky_on_fallback(store, key)
    d = fp.restore_allowed(st, T0 + 60, probe=True, primary_provider="claude-bpr",
                           eligibility=_elig({"instance_id": "x", "bound_seat": "sub-vps-6",
                                              "bound_eligible": True, "model_eligible": True}))
    assert not d.allowed and d.reason.startswith("until")


def test_stated_reset_used_exactly_never_doubled():
    for n in (0, 1, 5):
        assert fp.compute_cooldown_s("quota_model", n, stated_reset_s=5000) == 5000
    assert fp.compute_cooldown_s("quota_model", 3, stated_reset_s=10 * 86400) == 6 * 3600
    assert fp.compute_cooldown_s("quota_model", 3, stated_reset_s=10 * 86400,
                                 window="7d") == 7 * 86400
    ctx = {"reset_at": T0 + 4000}
    assert fp.stated_reset_from_context(ctx, T0) == 4000


# ── §4.3 table ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["enforce", "off"])
@pytest.mark.parametrize("minutes,expect", [(20, True), (28.5, True), (31, False)])
def test_warm_seat_idle_ttl_real_binding_life(store, key, mode, minutes, expect):
    amap = FakeAffinityMap(mode=mode)
    last_fable = T0
    amap.serve("sid", True, "sub-vps-6", last_fable)
    fp.note_primary_success(store, key, last_fable, provider="claude-bpr",
                            headers={"x-pool-served-by": "sub-vps-6"})
    st = _arm(store, key, cls="conn", now=last_fable + 1)
    now = last_fable + minutes * 60
    d = fp.restore_allowed(st, now, probe=True, primary_provider="claude-bpr",
                           live_session_id=None,
                           eligibility=_elig(amap.eligibility("sid", now)))
    assert d.allowed is expect
    assert (d.branch == "warm_seat") is expect


def test_warm_seat_hard_ttl_with_idle_left_stays(store, key):
    amap = FakeAffinityMap(hard_ttl=3000)
    amap.serve("sid", True, "sub-vps-6", T0 - 2900)   # bound long ago
    amap.serve("sid", True, "sub-vps-6", T0)          # refreshed: idle left 1800
    fp.note_primary_success(store, key, T0, provider="claude-bpr",
                            headers={"x-pool-served-by": "sub-vps-6"})
    st = _arm(store, key, now=T0 + 1)
    now = T0 + 130  # past until; hard TTL remaining < 0 now
    d = fp.restore_allowed(st, now, probe=True, primary_provider="claude-bpr",
                           eligibility=_elig(amap.eligibility("sid", now)))
    assert not d.allowed


def test_warm_seat_hard_ttl_under_60s_stays(store, key):
    st = _sticky_on_fallback(store, key)
    elig = {"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
            "model_eligible": True, "bound_expires_in_s": 45}
    d = fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                           eligibility=_elig(elig))
    assert not d.allowed and "bound_expires_in_s" in d.reason


def test_off_mode_post_cap_rebind_during_opus_stretch_stays(store, key):
    amap = FakeAffinityMap(mode="off")
    amap.serve("sid", True, "sub-vps-6", T0)
    fp.note_primary_success(store, key, T0, provider="claude-bpr",
                            headers={"x-pool-served-by": "sub-vps-6"})
    st = _arm(store, key, now=T0 + 1)
    amap.serve("sid", False, "sub-vps-2", T0 + 100, rebind=True)  # Opus stretch shares `sid`
    now = T0 + 300
    d = fp.restore_allowed(st, now, probe=True, primary_provider="claude-bpr",
                           eligibility=_elig(amap.eligibility("sid", now)))
    assert not d.allowed and "!= last_primary_seat" in d.reason
    # enforce: the Opus stretch never touches `sid|fable`
    amap2 = FakeAffinityMap(mode="enforce")
    amap2.serve("sid", True, "sub-vps-6", T0)
    amap2.serve("sid", False, "sub-vps-2", T0 + 100, rebind=True)
    assert fp.restore_allowed(st, now, probe=True, primary_provider="claude-bpr",
                              eligibility=_elig(amap2.eligibility("sid", now))).branch == "warm_seat"


def test_warm_seat_bound_expiry_none_uses_seat_equality(store, key):
    st = _sticky_on_fallback(store, key)
    elig = {"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
            "model_eligible": True, "bound_expires_in_s": None, "bound_expiry": "none"}
    assert fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                              eligibility=_elig(elig)).branch == "warm_seat"


def test_warm_seat_needs_bound_eligible_and_age_under_55(store, key):
    st = _sticky_on_fallback(store, key, last_primary=T0 - 56 * 60)
    ok = {"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
          "model_eligible": True}
    assert not fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                                  eligibility=_elig(ok)).allowed
    st = _sticky_on_fallback(store, key, last_primary=T0 - 60)
    capped = dict(ok, bound_eligible=False)
    assert not fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                                  eligibility=_elig(capped)).allowed


def test_unknown_seat_never_matches(store, key):
    fp.note_primary_success(store, key, T0, provider="claude-bpr",
                            headers={"x-pool-served-by": "unknown"})
    assert store.get(key).last_primary_seat is None


def test_warm_rank_enforce_widens_to_warm_eligible(store, key):
    st = _sticky_on_fallback(store, key)
    elig = {"instance_id": "x", "bound_seat": "sub-vps-1", "bound_eligible": True,
            "model_eligible": True, "warm_eligible": True, "warm_rank_effective": "enforce"}
    assert fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                              eligibility=_elig(elig)).branch == "warm_seat"
    shadow = dict(elig, warm_rank_effective="shadow")
    assert not fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                                  eligibility=_elig(shadow)).allowed


def test_fallback_cold_at_61_min(store, key):
    _sticky_on_fallback(store, key)
    now = T0 + 1 + 61 * 60
    d = fp.restore_allowed(store.get(key), now, probe=False)
    assert d.allowed and d.branch == "fallback_cold"
    assert not fp.restore_allowed(store.get(key), T0 + 1 + 59 * 60, probe=False).allowed


def test_compaction_branch(store, key):
    _sticky_on_fallback(store, key)
    d = fp.restore_allowed(store.get(key), T0 + 300, probe=False, live_session_id="sid-b")
    assert d.allowed and d.branch == "compaction"
    assert not fp.restore_allowed(store.get(key), T0 + 300, probe=False,
                                  live_session_id="sid-a").allowed


def test_nothing_but_fallback_failed_returns_before_until(store, key):
    st = _sticky_on_fallback(store, key, cls="quota_model")
    elig = _elig({"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
                  "model_eligible": True})
    now = T0 + 3 * 3600  # fallback idle > 60 min, session rotated, seat warm-ish: still < until
    d = fp.restore_allowed(st, now, probe=True, live_session_id="sid-b",
                           primary_provider="claude-bpr", eligibility=elig)
    assert not d.allowed
    ff = fp.fallback_failed_allowed(st, "quota_seat", now, primary_provider="claude-bpr",
                                    eligibility=elig)
    assert ff.allowed and ff.branch == "fallback_failed"


def test_fallback_failed_returns_instead_of_chain_walk(store, key):
    st = _sticky_on_fallback(store, key, cls="quota_seat")
    elig = _elig({"instance_id": "x", "bound_seat": None, "bound_eligible": False,
                  "model_eligible": True})
    assert fp.fallback_failed_allowed(st, "quota_seat", T0 + 10, primary_provider="claude-bpr",
                                      eligibility=elig).allowed


def test_second_fallback_failed_rearms_until_and_does_not_loop(store, key):
    _sticky_on_fallback(store, key, cls="quota_seat")
    elig = _elig({"instance_id": "x", "model_eligible": True, "bound_eligible": True})
    d = fp.fallback_failed_allowed(store.get(key), "quota_seat", T0 + 10,
                                   primary_provider="claude-bpr", eligibility=elig)
    assert d.allowed
    fp.record_return(store, key, T0 + 10, d.branch)
    n_before = store.get(key).n
    st = _arm(store, key, cls="quota_seat", now=T0 + 20)          # primary fails again
    assert st.n == n_before + 1 and st.ff_disabled_until == st.until_epoch
    again = fp.fallback_failed_allowed(st, "quota_seat", T0 + 30, primary_provider="claude-bpr",
                                       eligibility=elig)
    assert not again.allowed and "anti-loop" in again.reason


def test_stale_or_unreachable_eligibility_only_cold_or_compaction(store, key):
    st = _sticky_on_fallback(store, key)
    stale = _elig({"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
                   "model_eligible": True, "snapshot_age_s": 301})
    down = lambda: None  # noqa: E731
    for e in (stale, down):
        assert not fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpr",
                                      eligibility=e).allowed
        assert not fp.fallback_failed_allowed(st, "quota_seat", T0 + 200,
                                              primary_provider="claude-bpr", eligibility=e).allowed
        assert fp.restore_allowed(st, T0 + 200, probe=True, live_session_id="sid-b",
                                  primary_provider="claude-bpr", eligibility=e).branch == "compaction"
    assert fp.parse_eligibility({"bound_seat": "x"}) is None  # no instance_id -> fail closed


def test_fetch_eligibility_url_and_fail_closed():
    url = fp.eligibility_url("http://127.0.0.1:18811/v1", model="claude-fable-5-1",
                             user="u-sess:abc")
    assert url.startswith("http://127.0.0.1:18811/eligibility?")
    assert "user=u-sess%3Aabc" in url and "model=claude-fable-5-1" in url

    def boom(*a, **k):
        raise OSError("refused")
    assert fp.fetch_eligibility("http://127.0.0.1:1/", model="m", session="s", opener=boom) is None


# ── negatives (I2) ────────────────────────────────────────────────────────

def test_sticky_target_capped_and_primary_ineligible_walks_chain(store, key):
    st = _sticky_on_fallback(store, key, cls="quota_seat")
    no = _elig({"instance_id": "x", "bound_eligible": False, "model_eligible": False})
    assert not fp.fallback_failed_allowed(st, "quota_seat", T0 + 10,
                                          primary_provider="claude-bpr", eligibility=no).allowed


def test_pool_pressure_on_shared_relay_walks_chain(store, key):
    st = _sticky_on_fallback(store, key)
    yes = _elig({"instance_id": "x", "bound_eligible": True, "model_eligible": True})
    for cls in ("pool_pressure", "conn"):
        assert not fp.fallback_failed_allowed(st, cls, T0 + 10, primary_provider="claude-bpr",
                                              eligibility=yes).allowed


# ── lineage-root key, auth mark, seat state ───────────────────────────────

class _Agent:
    def __init__(self, sid, db=None):
        self.session_id, self._session_db = sid, db


class _LineageDB:
    def get_compression_lineage(self, sid):
        return ["root-sid", "sid-a", "sid-b"] if sid in ("root-sid", "sid-a", "sid-b") else [sid]


def test_sticky_survives_compaction_that_rotates_session_id(store):
    db = _LineageDB()
    k1 = StickyKey.build(fss.lineage_root_for_agent(_Agent("sid-a", db)), *FABLE)
    fp.arm_sticky(store, k1, cls="conn", now=T0, failing=FABLE, fallback=OPUS, jitter=1.0)
    k2 = StickyKey.build(fss.lineage_root_for_agent(_Agent("sid-b", db)), *FABLE)
    assert k1 == k2 and store.get(k2).active
    assert fss.lineage_root_for_agent(_Agent("lonely")) == "lonely"


def test_subagent_with_other_primary_does_not_share(store):
    fp.arm_sticky(store, StickyKey.build("r", *FABLE), cls="conn", now=T0,
                  failing=FABLE, fallback=OPUS, jitter=1.0)
    assert store.get(StickyKey.build("r", *OPUS)) is None


def test_auth_mark_per_token_fingerprint(store):
    fp1, fp2 = fp.token_fingerprint("tok-one"), fp.token_fingerprint("tok-two")
    assert len(fp1) == 12 and fp1 != fp2 and "tok" not in fp1
    store.mark_auth(fp1, T0)
    assert store.auth_marked(fp1, T0 + 10)
    assert not store.auth_marked(fp2, T0 + 10)            # re-authed token not benched
    assert not store.auth_marked(fp1, T0 + 24 * 3600 + 1)  # 24h TTL


def test_last_primary_seat_from_served_by_survives_rebuild_and_compaction(tmp_path):
    db = _LineageDB()
    s1 = StickyStore(db_path=tmp_path / "t.db")
    k = StickyKey.build(fss.lineage_root_for_agent(_Agent("sid-a", db)), *FABLE)
    fp.note_primary_success(s1, k, T0, provider="claude-bpr",
                            headers={"X-Pool-Served-By": "sub-vps-9"})
    s2 = StickyStore(db_path=tmp_path / "t.db")
    k2 = StickyKey.build(fss.lineage_root_for_agent(_Agent("sid-b", db)), *FABLE)
    got = s2.get(k2)
    assert (got.last_primary_seat, got.last_primary_call_epoch) == ("sub-vps-9", T0)
    assert fp.seat_from_response("claude-bpx-16", {}) == "claude-bpx-16"


def test_concurrent_sessions_no_cross_write(store):
    keys = [StickyKey.build(f"root-{i}", *FABLE) for i in range(8)]

    def run(i):
        for j in range(50):
            fp.note_primary_success(store, keys[i], T0 + j, provider="claude-bpr",
                                    headers={"x-pool-served-by": f"seat-{i}"})
    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [store.get(k).last_primary_seat for k in keys] == [f"seat-{i}" for i in range(8)]


# ── direct pins ───────────────────────────────────────────────────────────

def test_direct_pin_weekly_fable_limit_is_quota_model_7d():
    text = "Claude Fable weekly limit reached · resets Sep 29, 5pm"
    cls, window = fp.lane_class("quota_seat", "claude-bpx-16", text)
    assert (cls, window) == ("quota_model", "7d")
    assert fp.lane_class("quota_seat", "claude-bpr", text)[0] == "quota_seat"
    now = dt.datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp()
    reset = fp.parse_stated_reset_s(text, now, tz=UTC)
    assert reset == pytest.approx((3 * 24 + 5) * 3600)
    assert fp.compute_cooldown_s(cls, 0, stated_reset_s=reset, window=window) == reset
    assert fp.parse_stated_reset_s("limit reached", now) is None
    assert fp.parse_stated_reset_s("resets in 2h 30m", now) == 9000


def test_direct_pin_warm_seat_no_relay_call(tmp_path):
    store = StickyStore(db_path=tmp_path / "t.db")
    k = StickyKey.build("r", "claude-bpx-16", "claude-fable-5-1")
    fp.note_primary_success(store, k, T0, provider="claude-bpx-16")
    st = fp.arm_sticky(store, k, cls="conn", now=T0 + 1,
                       failing=("claude-bpx-16", "claude-fable-5-1"),
                       fallback=("claude-bpx-16", "claude-opus-5-5"), jitter=1.0)

    def no_relay():
        raise AssertionError("direct pin must not call /eligibility")
    d = fp.restore_allowed(st, T0 + 200, probe=True, primary_provider="claude-bpx-16",
                           eligibility=no_relay, direct_pin_benched=lambda: False)
    assert d.branch == "warm_seat"


def test_direct_pin_fallback_failed_RC_A_RC_6(tmp_path):
    store = StickyStore(db_path=tmp_path / "t.db")
    k = StickyKey.build("r", "claude-bpx-16", "claude-fable-5-1")
    st = fp.arm_sticky(store, k, cls="conn", now=T0,
                       failing=("claude-bpx-16", "claude-fable-5-1"),
                       fallback=("claude-bpx-16", "claude-opus-5-5"), jitter=1.0)

    def no_relay():
        raise AssertionError("no relay call on a direct pin")
    ok = fp.fallback_failed_allowed(st, "quota_seat", T0 + 10, primary_provider="claude-bpx-16",
                                    eligibility=no_relay, direct_pin_benched=lambda: False)
    assert ok.allowed
    benched = fp.fallback_failed_allowed(st, "quota_seat", T0 + 10,
                                         primary_provider="claude-bpx-16",
                                         eligibility=no_relay, direct_pin_benched=lambda: True)
    assert not benched.allowed  # -> chain walk
    # the real bench predicate: an active sticky on the pin counts as benched
    assert fp.direct_pin_benched(store, k, T0 + 10)
    tfp = fp.token_fingerprint("tok")
    store.mark_auth(tfp, T0)
    assert fp.direct_pin_benched(store, k, T0 + 10_000, token_fp=tfp)
    assert fp.direct_pin_benched(store, k, T0 + 10_000, registry_exhausted=lambda: True)


# ── rebuild decision / resume_sticky_fallback inputs ──────────────────────

def test_resume_decision_on_three_evictions_leaves_state_untouched(tmp_path, key):
    store = StickyStore(db_path=tmp_path / "t.db")
    _sticky_on_fallback(store, key, cls="quota_seat")
    before = store.get(key)
    for i in range(3):
        store.evict_memory()
        r = fp.decide_rebuild(store, key, T0 + 20 + i, live_session_id="sid-a",
                              eligibility=lambda: None)
        assert r.action == "resume"
    after = store.get(key)
    assert (after.until_epoch, after.n, after.active) == (before.until_epoch, before.n, True)


def test_resume_after_gateway_restart_db_only(tmp_path, key):
    s1 = StickyStore(db_path=tmp_path / "t.db")
    _sticky_on_fallback(s1, key)
    s2 = StickyStore(db_path=tmp_path / "t.db")
    assert fp.decide_rebuild(s2, key, T0 + 30, live_session_id="sid-a").action == "resume"


def test_resume_target_chain_changed():
    st = StickyState(fallback_provider="claude-bpr", fallback_model="claude-opus-5-5")
    chain = [{"provider": "openai-codex", "model": "gpt"},
             {"provider": "claude-bpr", "model": "claude-opus-5-5"}]
    assert fp.resume_target_index(chain, st) == (1, False)
    assert fp.resume_target_index(chain[:1], st) == (0, True)


def test_B1a_fallback_failed_return_then_evict_stays_on_primary(store, key):
    _sticky_on_fallback(store, key, cls="quota_seat")
    fp.record_return(store, key, T0 + 10, "fallback_failed")
    store.evict_memory()
    r = fp.decide_rebuild(store, key, T0 + 20, live_session_id="sid-a")
    assert r.action == "primary" and r.decision is None


def test_B1b_rebuild_after_until_fallback_warm_binding_expired_stays(store, key):
    st = _sticky_on_fallback(store, key, last_primary=T0 - 40 * 60)
    until = store.get(key).until_epoch
    now = until + 5 * 60
    fp.note_fallback_success(store, key, now - 10 * 60, "sid-a")
    expired = {"instance_id": "x", "bound_seat": None, "bound_eligible": False,
               "model_eligible": True}
    r = fp.decide_rebuild(store, key, now, live_session_id="sid-a", eligibility=_elig(expired))
    assert r.action == "resume" and "warm_seat" in r.decision.reason
    warm = {"instance_id": "x", "bound_seat": "sub-vps-6", "bound_eligible": True,
            "model_eligible": True}
    fp.note_primary_success(store, key, now - 20 * 60, provider="claude-bpr",
                            headers={"x-pool-served-by": "sub-vps-6"})
    r = fp.decide_rebuild(store, key, now, live_session_id="sid-a", eligibility=_elig(warm))
    assert r.action == "return" and r.decision.branch == "warm_seat"
    assert store.get(key).active is False


def test_db_only_restart_fallback_cold_compaction_and_dwell(tmp_path, key):
    s1 = StickyStore(db_path=tmp_path / "t.db")
    _sticky_on_fallback(s1, key)
    for _ in range(4):
        fp.note_fallback_success(s1, key, T0 + 60, "sid-a")
    now = T0 + 60 + 61 * 60
    s2 = StickyStore(db_path=tmp_path / "t.db")
    r = fp.decide_rebuild(s2, key, now, live_session_id="sid-a", eligibility=lambda: None)
    assert r.action == "return" and r.decision.branch == "fallback_cold"
    row = fp.recovery_row(r.state, r.decision, now)
    assert row["dwell_turns"] == 5 and row["dwell_s"] == pytest.approx(now - T0)
    s3 = StickyStore(db_path=tmp_path / "t3.db")
    _sticky_on_fallback(s3, key)
    s3.evict_memory()
    r = fp.decide_rebuild(s3, key, T0 + 600, live_session_id="sid-z", eligibility=lambda: None)
    assert r.decision.branch == "compaction"


def test_anti_loop_across_eviction_RC_2(tmp_path, key):
    store = StickyStore(db_path=tmp_path / "t.db")
    _sticky_on_fallback(store, key, cls="quota_seat")
    fp.record_return(store, key, T0 + 10, "fallback_failed")
    _arm(store, key, cls="quota_seat", now=T0 + 20)
    store.evict_memory()
    st = StickyStore(db_path=tmp_path / "t.db").get(key)
    elig = _elig({"instance_id": "x", "model_eligible": True, "bound_eligible": True})
    assert not fp.fallback_failed_allowed(st, "quota_seat", T0 + 30,
                                          primary_provider="claude-bpr", eligibility=elig).allowed


def test_cheap_form_makes_no_eligibility_call_RC_3(store, key):
    st = _sticky_on_fallback(store, key)
    calls = []

    def fetch():
        calls.append(1)
        return None
    fp.restore_allowed(st, T0 + 500, probe=False, eligibility=fetch)
    assert calls == []
    turn = fp.TurnEligibility(fetch)
    fp.restore_allowed(st, T0 + 500, probe=True, primary_provider="claude-bpr",
                       eligibility=lambda: turn.get("turn-1"))
    fp.fallback_failed_allowed(st, "quota_seat", T0 + 500, primary_provider="claude-bpr",
                               eligibility=lambda: turn.get("turn-1"))
    assert turn.calls == 1 and len(calls) == 1
    turn.get("turn-2")
    assert len(calls) == 2


def test_store_unreadable_RC_4(tmp_path, key, caplog):
    bad = tmp_path / "dir-not-db"
    bad.mkdir()
    store = StickyStore(db_path=bad)  # sqlite cannot open a directory
    with caplog.at_level("WARNING"):
        r = fp.decide_rebuild(store, key, T0, live_session_id="s")
    assert r.action == "store_unreadable" and store.store_unreadable_count == 1
    assert "root-sid" in caplog.text
    with pytest.raises(StoreUnreadable):
        store.get(key)
    # I3: writes never raise
    store.put(key, StickyState(), T0)
    store.mark_auth("abc", T0)


def test_sticky_policy_off_ignores_store(store, key):
    st = _sticky_on_fallback(store, key)
    assert fp.restore_allowed(st, T0 + 5, sticky_policy=False).allowed
    assert fp.decide_rebuild(store, key, T0 + 5, live_session_id="s",
                             sticky_policy=False).action == "primary"


def test_purge_logs_then_deletes(store, key, caplog):
    _arm(store, key)
    with caplog.at_level("INFO"):
        rows = store.purge()
    assert rows and rows[0][3] == "conn" and "fallback_sticky purge" in caplog.text
    assert store.get(key) is None


def test_lru_bound(tmp_path):
    s = StickyStore(db_path=tmp_path / "t.db", max_entries=3)
    for i in range(5):
        s.put(StickyKey.build(f"r{i}", *FABLE), StickyState(), T0)
    assert len(s._map) == 3


def test_n_c_hysteresis():
    prev = StickyState(cls="quota_seat", n=2, active=False, returned_at=T0)
    assert fp.next_n(prev, "quota_seat", T0 + 1800) == 3   # repeat does not fall back to 60 s
    assert fp.next_n(prev, "quota_seat", T0 + 3601) == 0
    assert fp.next_n(prev, "conn", T0 + 10) == 0


def test_n_c_resets_after_30_min_primary_service(store, key):
    _sticky_on_fallback(store, key, cls="quota_seat")
    fp.record_return(store, key, T0 + 100, "warm_seat")
    fp.note_primary_success(store, key, T0 + 100 + 1799, provider="claude-bpr")
    assert store.get(key).n == 0  # first arm had n=0; unchanged under 30 min
    st = _arm(store, key, cls="quota_seat", now=T0 + 2000)
    assert st.n == 1
    fp.record_return(store, key, T0 + 2100, "warm_seat")
    fp.note_primary_success(store, key, T0 + 2100 + 1800, provider="claude-bpr")
    assert store.get(key).n == 0


# ── §4.8 recovery rider (D6 branch / seat / dwell) ────────────────────────

def test_recovery_notice_renders_d6_branch_seat_dwell(store, key):
    fp.note_primary_success(store, key, T0 - 12 * 60 + 60, provider="claude-bpr",
                            headers={"x-pool-served-by": "sub-vps-6"})
    _arm(store, key, cls="conn", now=T0 - 18 * 60)
    for _ in range(7):
        fp.note_fallback_success(store, key, T0 - 60, "sid-a")
    st = store.get(key)
    d = fp.Decision(True, "warm_seat", "", None)
    row = fp.recovery_row(st, d, T0 + 60)
    assert fp.format_recovery_rider(row) == (
        "primary eligible on sub-vps-6, last call 12m ago (expected warm), "
        "after 19m / 7 turns on Opus")
