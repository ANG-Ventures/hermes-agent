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



# ══ Phase 2 WIRING (hot path) — §5 bullets #1264 left open ══════════════════
#
# Drives the REAL try_activate_fallback / restore_primary_runtime /
# gateway pre-run decision + _announce_reinit_recovery against a temp
# HERMES_HOME (blackbox ledger + model-route-changes.log on disk).

import ast as _ast
import os as _os
import pathlib as _pathlib
import sqlite3 as _sqlite3
import time as _time
import types as _types

import agent.auxiliary_client as _ac
from agent import fallback_events as _fbe
from agent import fallback_wiring as _fw
from agent.agent_runtime_helpers import restore_primary_runtime as _restore
from agent.chat_completion_helpers import try_activate_fallback as _activate
from agent.error_classifier import FailoverReason as _FR

SID = "20260926_100000_aaaa"
REPO = _pathlib.Path(__file__).resolve().parents[2]
ELIG = fp.Eligibility(bound_seat="sub-vps-6", bound_eligible=True, model_eligible=True,
                      bound_expires_in_s=900.0, bound_idle_s=0.0, bound_expiry=None,
                      snapshot_age_s=1.0, instance_id="pid:1 port:18811")


class _Err(Exception):
    def __init__(self, msg, status, headers=None, body=None):
        super().__init__(msg)
        self.status_code = status
        self.response = _types.SimpleNamespace(headers=headers or {})
        self.body = body


FABLE_CAP = lambda: _Err("You've reached your Fable limit", 429,  # noqa: E731
                         body={"error": {"message": "Fable limit"}})
OPUS_CAP = lambda: _Err("You've reached your Opus limit", 429,  # noqa: E731
                        body={"error": {"message": "Opus limit"}})
CONN = lambda: _Err("Connection error.", None)  # noqa: E731


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Temp home with blackbox + announces on; resolver + eligibility faked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "blackbox:\n  enabled: true\n"
        "model:\n  announce_route_change: true\n  announce_recovery: true\n")
    _ac.clear_runtime_main()
    calls = {"resolve": [], "elig": 0, "gate": 0}

    def _resolve(provider, model, **kw):
        calls["resolve"].append((provider, model))
        return (_types.SimpleNamespace(api_key="fb-key", base_url="http://127.0.0.1:18811/v1",
                                       _custom_headers=None, default_headers=None,
                                       tag=f"{provider}/{model}"), model)

    monkeypatch.setattr(_ac, "resolve_provider_client", _resolve, raising=False)
    import agent.chat_completion_helpers as cch
    monkeypatch.setattr(cch, "get_model_context_length", lambda *a, **k: 1_000_000, raising=False)

    def _elig_fn(agent):
        def _get():
            calls["elig"] += 1
            return ELIG
        return _get

    monkeypatch.setattr(_fw, "_eligibility_fn", _elig_fn)
    import agent.quota_registry_gate as qrg
    real_gate = qrg.apply_quota_gate

    def _gate_spy(agent, *a, **k):
        calls["gate"] += 1
        return real_gate(agent, *a, **k)

    monkeypatch.setattr(qrg, "apply_quota_gate", _gate_spy)
    monkeypatch.setattr(qrg, "load_registry_snapshot", lambda *a, **k: {}, raising=False)
    yield tmp_path, calls
    _ac.clear_runtime_main()


def _wired_agent(sid=SID, provider=FABLE[0], model=FABLE[1]):
    from tests.agent.test_route_change_sink_e2e import _fake_agent

    base = "http://127.0.0.1:18811/v1"
    a = _fake_agent(model=model, provider=provider, base_url=base, api_mode="chat_completions")
    a.session_id = sid
    a._current_turn_id = f"{sid}:{sid}:t1"
    a.requested_provider = provider
    a._fallback_chain = [{"provider": OPUS[0], "model": OPUS[1]}]
    a._client_kwargs = {"api_key": "primary-key", "base_url": base}
    a._use_prompt_caching = False
    a._use_native_cache_layout = False
    a.request_overrides = {}
    a._primary_runtime = {
        "model": model, "provider": provider, "requested_provider": provider,
        "base_url": base, "api_mode": "chat_completions", "api_key": "primary-key",
        "request_overrides": {}, "client_kwargs": dict(a._client_kwargs),
        "use_prompt_caching": False, "use_native_cache_layout": False,
        "reasoning_echo_flag": False, "compressor_model": model, "compressor_base_url": base,
        "compressor_api_key": "", "compressor_provider": provider,
        "compressor_context_length": 1_000_000, "compressor_threshold_tokens": 0,
    }
    a._create_openai_client = lambda kw, **k: _types.SimpleNamespace(tag="primary", **{})
    a._try_activate_fallback = lambda *x, **k: _activate(a, *x, **k)
    a.client = _types.SimpleNamespace(tag="primary")
    return a


def _rows(home, kind=None):
    p = _os.path.join(str(home), "blackbox", "turns.db")
    if not _os.path.exists(p):
        return []
    con = _sqlite3.connect(p)
    con.row_factory = _sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("select * from fallback_events order by id")]
    except _sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [r for r in rows if kind is None or r["kind"] == kind]


def _route_lines(home):
    p = _pathlib.Path(home) / "state" / "model-route-changes.log"
    return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _fail(agent, err, reason=_FR.rate_limit):
    _fbe.stash_api_error(agent, err, err.status_code, {"message": str(err)})
    return _activate(agent, reason=reason)


def _state(agent):
    return fss.get(_fw.key_for(agent))


def _age_episode(agent, *, until_ago=300.0, fallback_idle=None, last_primary_ago=None):
    """Move the stored episode into the past (no clock patching)."""
    k = _fw.key_for(agent)
    st = fss.get(k)
    now = _time.time()
    st.until_epoch = now - until_ago
    if fallback_idle is not None:
        st.last_fallback_call_epoch = now - fallback_idle
        st.entered_at = now - fallback_idle - 600
    if last_primary_ago is not None:
        st.last_primary_call_epoch = now - last_primary_ago
    fss.default_store().put(k, st, now)
    return st


def test_wiring_conn_failover_arms_sticky_not_legacy_clock(wired):
    """§4.1 plumbing: conn skips apply_quota_gate AND the legacy arm; the
    sticky writer arms (one clock); the row carries sticky_until + rider."""
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, CONN(), reason=_FR.rate_limit) is True
    assert (a.provider, a.model) == OPUS
    assert calls["gate"] == 0
    assert (a._rate_limited_until or 0) <= _time.monotonic()
    st = _state(a)
    assert st.active and st.cls == "conn" and st.fallback_model == OPUS[1]
    [row] = _rows(home, "failover")
    assert row["trigger_class"] == "conn"
    assert row["sticky_until_epoch"] == pytest.approx(st.until_epoch)
    assert row["notice_text"].startswith("🔄 Model fallback")
    assert " — connection error " in row["notice_text"]
    assert any(" — connection error " in m for _k, m in a._announced)


def test_wiring_b1_fallback_429_does_not_rearm_primary(wired):
    """B1: a quota_seat on the same-provider fallback (bpr Opus after bpr
    Fable) does not re-arm or escalate the primary's episode."""
    home, _ = wired
    a = _wired_agent()
    a._fallback_chain = [{"provider": OPUS[0], "model": OPUS[1]},
                         {"provider": "openai-codex", "model": "gpt-5.5"}]
    assert _fail(a, CONN()) is True
    before = _state(a)
    # pool_pressure on the fallback: not a fallback_failed class -> chain walk
    assert _fail(a, _Err("pool at capacity", 503), reason=_FR.overloaded) is True
    assert (a.provider, a.model) == ("openai-codex", "gpt-5.5")
    after = _state(a)
    assert (after.cls, after.n, after.until_epoch) == (before.cls, before.n, before.until_epoch)


def test_wiring_content_policy_keeps_legacy_60s(wired):
    """B3: refusal reaches the legacy block unchanged (shared 60 s default)."""
    home, calls = wired
    a = _wired_agent()
    _fbe.stash_api_error(a, _Err("content_policy blocked", 400), 400, {"message": "content_policy"})
    assert _activate(a, reason=_FR.rate_limit) is True
    assert calls["gate"] == 1
    assert a._rate_limited_until > _time.monotonic()
    assert _state(a) is None


def test_wiring_restore_refused_then_fallback_cold_return(wired):
    """restore_refused while sticky; after until, fallback_cold returns with a
    recovery row naming the branch, a route-change line and the rider."""
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    assert _restore(a) is False
    refused = _rows(home, "restore_refused")
    assert len(refused) == 1 and refused[0]["reason"].startswith("until")
    _age_episode(a, fallback_idle=61 * 60)
    assert _restore(a) is True
    assert (a.provider, a.model) == FABLE
    [rec] = _rows(home, "recovery")
    assert rec["return_branch"] == "fallback_cold" and rec["dwell_s"] is not None
    assert "fallback idle 61m" in rec["notice_text"]
    assert len(_route_lines(home)) == 2
    assert _state(a).active is False


def test_wiring_failover_fallback_failed_failover(wired):
    """failover -> fallback_failed -> failover within one turn: 2 failover
    rows + 1 recovery row, 3 route-change lines, both failovers announced;
    a second same-class fallback failure does not loop (anti-loop)."""
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, FABLE_CAP()) is True                     # Fable -> Opus
    assert (a.provider, a.model) == OPUS
    assert _fail(a, OPUS_CAP()) is True                      # Opus capped, Fable eligible
    assert (a.provider, a.model) == FABLE                    # returned, no chain walk
    assert calls["resolve"] == [OPUS]                        # chain was not walked
    assert _fail(a, FABLE_CAP()) is True                     # Fable fails again -> Opus
    assert (a.provider, a.model) == OPUS
    assert [r["kind"] for r in _rows(home)] == ["failover", "recovery", "failover"]
    assert _rows(home, "recovery")[0]["return_branch"] == "fallback_failed"
    assert len(_route_lines(home)) == 3
    fb_lines = [m for _k, m in a._announced if m.startswith("🔄 Model fallback")]
    assert len(fb_lines) == 2
    st = _state(a)
    assert st.ff_disabled_until == pytest.approx(st.until_epoch)
    # anti-loop: Opus capped again -> no second return, chain walk (exhausted)
    assert _fail(a, OPUS_CAP()) is False
    assert (a.provider, a.model) == OPUS
    assert len(_rows(home, "recovery")) == 1


def test_wiring_negative_fallback_capped_primary_ineligible_walks_chain(wired, monkeypatch):
    """I2: the sticky target is capped and the primary is NOT eligible -> the
    harness walks the chain."""
    home, _ = wired
    monkeypatch.setattr(_fw, "_eligibility_fn", lambda agent: (lambda: None))
    a = _wired_agent()
    a._fallback_chain = [{"provider": OPUS[0], "model": OPUS[1]},
                         {"provider": "openai-codex", "model": "gpt-5.5"}]
    assert _fail(a, FABLE_CAP()) is True
    assert _fail(a, OPUS_CAP()) is True
    assert (a.provider, a.model) == ("openai-codex", "gpt-5.5")
    assert _rows(home, "recovery") == []


def _fresh_rebuild(home, calls, state_before):
    b = _wired_agent()
    calls["gate"] = 0
    lines = len(_route_lines(home))
    resumes = len(_rows(home, "sticky_resume"))
    assert _fw.decide_rebuild_for_agent(b) == "resume"
    # the first outgoing request's client/route is the stored fallback
    assert (b.provider, b.model) == OPUS and calls["resolve"][-1] == OPUS
    assert b.client.tag == f"{OPUS[0]}/{OPUS[1]}"
    assert b._fallback_activated is True and b._fallback_index == 1
    assert (b._rate_limited_until or 0) == 0 and calls["gate"] == 0
    assert b._announced == []
    assert getattr(b, "_last_fallback_event", None) is None
    assert len(_route_lines(home)) == lines
    assert len(_rows(home, "sticky_resume")) == resumes + 1
    st = _state(b)
    assert (st.until_epoch, st.n, st.cls) == (state_before.until_epoch, state_before.n, state_before.cls)
    return b


def test_wiring_resume_after_three_evictions_and_restart(wired):
    """resume_sticky_fallback (§4.2 normative): evict 3x -> each rebuilt agent's
    first call goes to the fallback; until/n_c/_rate_limited_until unchanged,
    apply_quota_gate not called, one sticky_resume row per rebuild, zero
    route-change lines, no announce; gateway restart (map empty, DB row) ->
    same; a later restore returns to the snapshotted primary (B1'c)."""
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    st0 = _state(a)
    for _ in range(3):
        b = _fresh_rebuild(home, calls, st0)
    fss.default_store().evict_memory()                      # gateway restart
    b = _fresh_rebuild(home, calls, st0)
    assert len(_rows(home, "sticky_resume")) == 4
    _age_episode(b, fallback_idle=61 * 60)
    assert _restore(b) is True
    assert (b.provider, b.model) == FABLE and b.client.tag == "primary"


def test_wiring_resume_chain_head_when_entry_removed(wired):
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    b = _wired_agent()
    b._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.5"}]
    assert _fw.decide_rebuild_for_agent(b) == "resume"
    assert (b.provider, b.model) == ("openai-codex", "gpt-5.5")
    [row] = _rows(home, "sticky_resume")
    assert row["reason"] == "chain_head"


def test_wiring_b1a_fallback_failed_return_then_evict_stays_on_primary(wired):
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, FABLE_CAP()) is True
    assert _fail(a, OPUS_CAP()) is True                      # fallback_failed return
    assert _state(a).active is False
    lines = len(_route_lines(home))
    b = _wired_agent()
    assert _fw.decide_rebuild_for_agent(b) == "primary"
    assert (b.provider, b.model) == FABLE
    assert _rows(home, "sticky_resume") == [] and len(_route_lines(home)) == lines


def _runner_env(home, last_served):
    import threading as _th
    from datetime import datetime, timezone

    import gateway.run as gateway_run
    from gateway.session import SessionEntry, SessionStore

    store = object.__new__(SessionStore)
    store._entries = {}
    store.sessions_dir = home
    store._lock = _th.RLock()
    store._loaded = True
    store._record_gateway_session_peer = lambda *a, **k: None
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    now = datetime.now(timezone.utc)
    e = SessionEntry(session_key="agent:main:telegram:c1:c1", session_id=SID,
                     created_at=now, updated_at=now)
    e.last_served_identity = {"provider": last_served[0], "model": last_served[1]}
    store._entries[e.session_key] = e
    return runner, e.session_key


def _prerun(runner, key, agent):
    """The gateway pre-run site, in order (gateway/run.py, fresh agent)."""
    _fw.decide_rebuild_for_agent(agent)
    runner._announce_reinit_recovery(agent=agent, session_key=key,
                                     applied_provider=agent.provider, applied_model=agent.model)
    _fw.flush_unconsumed_recovery_row(agent)


def test_wiring_g2_one_decision_one_notice_per_rebuild(wired):
    """G2: gate true at rebuild -> exactly one recovery row, one route-change
    line and one Model recovery notice on the bound sink; gate false ->
    sticky_resume row, zero route-change lines, no recovery notice."""
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    base_lines = len(_route_lines(home))
    runner, key = _runner_env(home, OPUS)
    # gate false (inside until) -> resume, silent
    b = _wired_agent()
    _prerun(runner, key, b)
    assert (b.provider, b.model) == OPUS
    assert b._announced == [] and len(_route_lines(home)) == base_lines
    assert _rows(home, "recovery") == [] and len(_rows(home, "sticky_resume")) == 1
    # B1'b: until + 5 min, fallback warm (10 min), binding gone -> stays
    _age_episode(a, until_ago=300, fallback_idle=600, last_primary_ago=60 * 60)
    c = _wired_agent()
    _prerun(runner, key, c)
    assert (c.provider, c.model) == OPUS and c._announced == []
    assert "warm_seat" in _rows(home, "restore_refused")[-1]["reason"]
    # until + 5 min with warm_seat true -> return: 1 row, 1 line, 1 notice
    _age_episode(a, until_ago=300, fallback_idle=600, last_primary_ago=10 * 60)
    st = _state(a)
    st.last_primary_seat = "sub-vps-6"
    fss.default_store().put(_fw.key_for(a), st, _time.time())
    d = _wired_agent()
    _prerun(runner, key, d)
    assert (d.provider, d.model) == FABLE
    [rec] = _rows(home, "recovery")
    assert rec["return_branch"] == "warm_seat" and rec["seat"] == "sub-vps-6"
    assert len(_route_lines(home)) == base_lines + 1
    notices = [m for _k, m in d._announced if m.startswith("🔄 Model recovery")]
    assert len(notices) == 1 and "primary eligible on sub-vps-6" in notices[0]
    assert rec["notice_text"] == notices[0]


def test_wiring_db_only_restart_fallback_cold_and_compaction(wired):
    """pass-10 G1: map empty, DB row -> fallback_cold at 61 min idle; a rotated
    session id -> compaction. Dwell in the notice comes from the DB row."""
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    _age_episode(a, until_ago=60, fallback_idle=61 * 60)
    fss.default_store().evict_memory()
    runner, key = _runner_env(home, OPUS)
    b = _wired_agent()
    _prerun(runner, key, b)
    assert (b.provider, b.model) == FABLE
    assert _rows(home, "recovery")[-1]["return_branch"] == "fallback_cold"
    assert "after " in _rows(home, "recovery")[-1]["notice_text"]


def test_wiring_i3_turn_completes_with_db_unwritable(wired):
    """I3: blackbox dir unwritable -> failover, restore gate and rebuild all
    complete; nothing raises; the in-process map still holds the episode."""
    home, _ = wired
    (home / "blackbox").write_text("not a dir")               # mkdir/connect fails
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    assert _state(a).active is True
    assert _restore(a) is False                                 # refused (until), no raise
    b = _wired_agent()
    assert _fw.decide_rebuild_for_agent(b) == "resume"
    _fw.note_success(b, {"x-pool-served-by": "sub-vps-6"})


def test_wiring_store_unreadable_starts_on_primary(wired, monkeypatch):
    """RC-4: map miss + DB read raises -> start on primary, counted."""
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    st = fss.default_store()
    st.evict_memory()
    monkeypatch.setattr(StickyStore, "_read_db", lambda self, key: (_ for _ in ()).throw(OSError("locked")))
    b = _wired_agent()
    assert _fw.decide_rebuild_for_agent(b) == "store_unreadable"
    assert (b.provider, b.model) == FABLE and st.store_unreadable_count >= 1


def test_wiring_cheap_form_chain_refresh_makes_no_eligibility_call(wired):
    """RC-3: the gateway chain-refresh gate uses probe=False (no I/O) and
    keeps the chain while the sticky gate refuses."""
    import gateway.run as gateway_run

    home, calls = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    calls["elig"] = 0
    old = list(a._fallback_chain)
    gateway_run.GatewayRunner._apply_fallback_chain_to_agent(a, [{"provider": "x", "model": "y"}])
    assert a._fallback_chain == old and calls["elig"] == 0


def test_wiring_primary_success_records_seat_and_survives_rebuild(wired):
    """last_primary_call_epoch / last_primary_seat come from x-pool-served-by
    on success and survive an agent rebuild (lineage-root store)."""
    from agent.chat_completion_helpers import _record_successful_api_call

    home, _ = wired
    a = _wired_agent()
    resp = _types.SimpleNamespace(usage=None, pool_headers={"x-pool-served-by": "sub-vps-6"})
    _record_successful_api_call(a, resp)
    fss.default_store().evict_memory()
    st = _state(_wired_agent())
    assert st.last_primary_seat == "sub-vps-6" and st.last_primary_call_epoch


def test_wiring_sticky_policy_false_purges_and_disables(wired):
    """§7 rollback: fallback.sticky_policy=false purges the table and the
    failover keeps today's behaviour (no sticky arm)."""
    home, _ = wired
    a = _wired_agent()
    assert _fail(a, CONN()) is True
    (home / "config.yaml").write_text("blackbox:\n  enabled: true\nfallback:\n  sticky_policy: false\n")
    assert _fw.sticky_policy_enabled() is False
    con = _sqlite3.connect(str(home / "blackbox" / "turns.db"))
    assert con.execute("select count(*) from fallback_sticky").fetchone()[0] == 0
    con.close()
    b = _wired_agent()
    assert _fw.decide_rebuild_for_agent(b) == "primary"
    c = _wired_agent(sid="20260926_100000_bbbb")
    assert _fail(c, CONN()) is True
    assert _state(c) is None


# ── AST guard (RC-5): one reader of _sticky / _rate_limited_until ─────────

_READ_ALLOW = {
    ("agent/chat_completion_helpers.py", "try_activate_fallback"),  # the writer (its own max())
    ("agent/fallback_wiring.py", "restore_allowed"),                # the one restore predicate
    ("agent/fallback_policy.py", "restore_allowed"),
    ("agent/fallback_sticky_store.py", "get"),                     # the store accessor
}
_GUARDED_ATTRS = ("_rate_limited_until", "_sticky")


def _sticky_reads(path: _pathlib.Path, rel: str):
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def visit(node, fn):
        for child in _ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)) else fn
            hit = None
            if (isinstance(child, _ast.Attribute) and child.attr in _GUARDED_ATTRS
                    and isinstance(child.ctx, _ast.Load)):
                hit = child
            elif (isinstance(child, _ast.Call) and isinstance(child.func, _ast.Name)
                  and child.func.id in ("getattr", "hasattr") and len(child.args) >= 2
                  and isinstance(child.args[1], _ast.Constant)
                  and child.args[1].value in _GUARDED_ATTRS):
                hit = child
            if hit is not None and (rel, name) not in _READ_ALLOW:
                out.append(f"{rel}:{hit.lineno} in {name}")
            visit(child, name)

    visit(tree, None)
    return out


def _guard_scope():
    for sub in ("agent", "gateway"):
        for p in sorted((REPO / sub).rglob("*.py")):
            yield p, p.relative_to(REPO).as_posix()
    yield REPO / "run_agent.py", "run_agent.py"


def test_ast_guard_single_reader_of_cooldown_state():
    bad = [v for p, rel in _guard_scope() for v in _sticky_reads(p, rel)]
    assert bad == [], "direct read of _rate_limited_until/_sticky outside the accessor: " + ", ".join(bad)


def test_ast_guard_single_reader_self_test_flags_planted_read(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("def peek(agent):\n    return getattr(agent, '_rate_limited_until', 0)\n"
                       "def peek2(agent):\n    return agent._sticky\n")
    assert len(_sticky_reads(planted, "agent/planted.py")) == 2



def test_replay_script_old_flaps_new_bounded(tmp_path):
    """§5 replay: a flapping quota_model session (4 return/re-failover pairs
    inside the cooldown) is > 2 legs under the old policy and <= 2 under the
    new one; exit 0 against the target."""
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("replay_fp", REPO / "scripts" / "replay-fallback-policy.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t0 = _time.time() - 3600
    rows = [{"ts": t0, "session_id": "s", "kind": "failover", "trigger_class": "quota_model"}]
    for i in range(4):
        rows.append({"ts": t0 + 60 * (2 * i + 1), "session_id": "s", "kind": "recovery", "trigger_class": None})
        rows.append({"ts": t0 + 60 * (2 * i + 2), "session_id": "s", "kind": "failover", "trigger_class": "quota_model"})
    res = mod.replay(rows)
    assert res["quota_model_events"] == 1
    assert res["max_legs_per_quota_event_old"] == 9
    assert res["max_legs_per_quota_event"] <= 2
    db = tmp_path / "turns.db"
    con = _sqlite3.connect(str(db))
    con.execute("create table fallback_events (id integer primary key, ts real, session_id text,"
                " kind text, trigger_class text, from_provider text, from_model text,"
                " to_provider text, to_model text)")
    con.executemany("insert into fallback_events (ts, session_id, kind, trigger_class)"
                    " values (:ts, :session_id, :kind, :trigger_class)", rows)
    con.commit()
    con.close()
    assert mod.main(["--ledger", "48h", "--db", str(db)]) == 0
    assert mod.main(["--ledger", "48h", "--db", str(db), "--target", "0"]) == 1
