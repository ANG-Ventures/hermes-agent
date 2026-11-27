"""t_d780729d: a Claude relay lane keeps no per-lane quota verdict.

Every Claude lane reaches the same subscriptions through a relay that knows
each seat's state. A 429 seen on one lane name must not leave that lane (or a
sibling name of the same pool) believing something the relay does not, and a
stored "exhausted" must never outlive a stated reset or a short cache.
"""

from __future__ import annotations

import json
import time

import pytest

from agent import fallback_policy as fp
from agent.fallback_sticky_store import StickyKey, StickyStore
from agent.relay_lanes import is_relay_backed, is_relay_pinned, is_relay_pooled

T0 = 1_790_000_000.0


# ── which names are relay-backed ──────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "claude-apr", "claude-alr", "claude-alrs", "claude-alrf", "claude-dalrs",
    "claude-dalrf", "claude-bpr", "claude-btpr", "claude-dtlr", "claude-dtlrs",
    "claude-dtlrf", "claude-bhpr", "claude-bpr-tui", "CLAUDE-APR",
])
def test_pooled_relay_faces(name):
    assert is_relay_pooled(name) and is_relay_backed(name)
    assert not is_relay_pinned(name)


@pytest.mark.parametrize("name", ["claude-apx-0", "claude-apx-7", "claude-bpx-12",
                                  "claude-cpx-3", "claude-alx-4", "claude-dtlx-2"])
def test_seat_pins(name):
    assert is_relay_pinned(name) and is_relay_backed(name)
    assert not is_relay_pooled(name)


@pytest.mark.parametrize("name", ["anthropic", "claude-code", "claude", "openai-codex",
                                  "claude-apr-3", "claude-apx", "claude-opus-5-5", "", None])
def test_not_relay_lanes(name):
    assert not is_relay_backed(name)


# ── credential pool: no stored quota exhaustion for relay lanes ───────────

def _auth(tmp_path, monkeypatch, pools: dict) -> None:
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": pools}))


def _row(cid, key, **status):
    return {"id": cid, "label": cid, "auth_type": "api_key", "priority": 0,
            "source": "manual", "access_token": key, **status}


def _disk_row(tmp_path, provider, cid):
    data = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    return next(r for r in data["credential_pool"][provider] if r["id"] == cid)


@pytest.mark.parametrize("provider", ["claude-alr", "claude-apr", "claude-apx-7", "claude-dtlr"])
def test_429_on_relay_lane_is_not_persisted_as_exhausted(tmp_path, monkeypatch, provider):
    _auth(tmp_path, monkeypatch, {provider: [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    pool = load_pool(provider)
    assert pool.select() is not None
    nxt = pool.mark_exhausted_and_rotate(status_code=429, credential_id="r1",
                                         api_key_hint="sk-relay", failure_reason="rate_limit")
    assert nxt is None  # nothing to rotate to: the relay rotates seats, not us
    row = _disk_row(tmp_path, provider, "r1")
    assert row.get("last_status") != "exhausted", row
    assert load_pool(provider).has_available() is True


def test_429_on_one_lane_name_leaves_sibling_names_agreeing(tmp_path, monkeypatch):
    """alr and apr are the same relay: a 429 on alr must not make alr believe
    'out' while apr believes 'ok' (Discord 1570522498922053854)."""
    _auth(tmp_path, monkeypatch, {"claude-alr": [_row("a", "sk-relay")],
                                  "claude-apr": [_row("b", "sk-relay")]})
    from agent.credential_pool import load_pool

    load_pool("claude-alr").mark_exhausted_and_rotate(
        status_code=429, credential_id="a", api_key_hint="sk-relay", failure_reason="rate_limit")
    assert load_pool("claude-alr").has_available() == load_pool("claude-apr").has_available() is True


@pytest.mark.parametrize("status", [
    # the live shape: 429, no reset time at all, status written a minute ago
    {"last_status": "exhausted", "last_error_code": 429, "last_status_at": "NOW-60"},
    # a reset stamped on the lane row, still in the future
    {"last_status": "exhausted", "last_error_code": 429, "last_status_at": "NOW-60",
     "last_error_reset_at": "NOW+86400"},
    # no timestamps at all
    {"last_status": "exhausted", "last_error_code": 429},
])
def test_stored_exhausted_row_does_not_bench_relay_lane(tmp_path, monkeypatch, status):
    now = time.time()
    status = {k: (now - 60 if v == "NOW-60" else now + 86400 if v == "NOW+86400" else v)
              for k, v in status.items()}
    _auth(tmp_path, monkeypatch, {"claude-apx-3": [_row("p3", "sk-pin", **status)]})
    from agent.credential_pool import load_pool

    pool = load_pool("claude-apx-3")
    assert pool.has_available() is True
    assert pool.next_available_at() is None
    assert pool.select() is not None
    # selection cleared the stale verdict on disk: nothing left to clean by hand
    assert _disk_row(tmp_path, "claude-apx-3", "p3").get("last_status") != "exhausted"


def test_relay_lane_auth_401_still_benches_the_local_key(tmp_path, monkeypatch):
    """A 401 is the LOCAL relay key, not a seat: that stays a local fact."""
    _auth(tmp_path, monkeypatch, {"claude-apr": [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    load_pool("claude-apr").mark_exhausted_and_rotate(
        status_code=401, credential_id="r1", api_key_hint="sk-relay", failure_reason="auth")
    assert _disk_row(tmp_path, "claude-apr", "r1").get("last_status") == "exhausted"
    assert load_pool("claude-apr").has_available() is False


def test_non_relay_provider_429_still_benched(tmp_path, monkeypatch):
    _auth(tmp_path, monkeypatch, {"deepseek": [_row("d1", "sk-a"), _row("d2", "sk-b")]})
    from agent.credential_pool import load_pool

    load_pool("deepseek").mark_exhausted_and_rotate(
        status_code=429, credential_id="d1", api_key_hint="sk-a", failure_reason="rate_limit")
    assert _disk_row(tmp_path, "deepseek", "d1").get("last_status") == "exhausted"


def test_pre_exhausted_relay_row_does_not_skip_same_route_retry(tmp_path, monkeypatch):
    """_recover_rate_limit rotated 'immediately' on a stale exhausted row,
    skipping the one same-route retry and re-stamping the row."""
    from agent import agent_runtime_helpers as arh

    _auth(tmp_path, monkeypatch, {"claude-alr": [_row(
        "r1", "sk-relay", last_status="exhausted", last_error_code=429,
        last_status_at=time.time() - 30 * 86400)]})
    from agent.credential_pool import load_pool

    pool = load_pool("claude-alr")
    calls = []
    out = arh._recover_rate_limit(
        pool, has_retried_429=False, error_context=None, api_key_hint="sk-relay",
        credential_id="r1", rotate_and_swap=lambda *a: calls.append(a) or False)
    assert calls == [] and out == (False, True)


# ── sticky fallback: quota verdict = the relay's reset or a short cache ──

def _arm(store, provider, cls, **kw):
    key = StickyKey.build("root", provider, "claude-fable-5-2")
    st = fp.arm_sticky(store, key, cls=cls, now=T0, failing=(provider, "claude-fable-5-2"),
                       fallback=("claude-bpr", "claude-opus-5-5"), jitter=1.0, **kw)
    return st.until_epoch - T0


@pytest.mark.parametrize("provider", ["claude-alr", "claude-bpr", "claude-dtlr", "claude-btpr"])
def test_relay_quota_model_without_relay_reset_is_a_short_cache(tmp_path, provider):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, provider, "quota_model") == fp.RELAY_QUOTA_CACHE_S


def test_relay_quota_model_uses_relay_stated_reset(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "claude-alr", "quota_model", stated_reset_s=7200.0) == 7200.0


def test_relay_quota_seat_never_doubles(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    first = _arm(store, "claude-alr", "quota_seat", retry_after_s=90.0)
    second = _arm(store, "claude-alr", "quota_seat", retry_after_s=90.0)
    assert first == second == 90.0
    assert _arm(StickyStore(db_path=tmp_path / "b.db"), "claude-alr", "quota_seat") == fp.RELAY_QUOTA_CACHE_S


def test_relay_quota_cache_capped_at_window_ceiling(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "claude-apr", "quota_model", stated_reset_s=30 * 86400.0) == fp.WINDOW_CEILING_DEFAULT_S


def test_non_relay_and_direct_pin_cooldowns_unchanged(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "openrouter", "quota_model") == 6 * 3600
    assert _arm(StickyStore(db_path=tmp_path / "p.db"), "claude-bpx-12", "quota_model") == 6 * 3600


def test_relay_quota_class_skips_the_local_legacy_bench():
    for cls in ("quota_seat", "quota_model"):
        assert fp.skips_legacy_arm(cls, "claude-alr") is True
        assert fp.skips_legacy_arm(cls, "claude-dtlr") is True
    assert fp.skips_legacy_arm("quota_model", "openrouter") is False
    assert fp.skips_legacy_arm("quota_model", "claude-bpx-12") is False
    assert fp.skips_legacy_arm("rate_upstream", "claude-alr") is False
    assert fp.skips_legacy_arm("conn", "openrouter") is True  # unchanged: skips_quota_gate


# ── round 2 (Apollo send-back on #294) ─────────────────────────────────────

# Every lane name that exists today (hermes-home lane table, 2026-11-12). A new
# name must be added here, so CI says which side of the relay predicate it is on.
POOLED_TODAY = ["claude-alr", "claude-alrf", "claude-alrs", "claude-apr", "claude-bpr",
                "claude-btpr", "claude-btprf", "claude-dtlr", "claude-dtlrf",
                "claude-clr", "claude-clrf"]
PINS_TODAY = ["claude-apx-1", "claude-bpx-12", "claude-cpx-3", "claude-alx-4",
              "claude-alxf-4", "claude-dpx-2", "claude-dtlx-5"]
NOT_RELAY_TODAY = {
    "claude-elr": "credit family: its own API budget, not subscription seats",
    "claude-epr": "credit family: its own API budget, not subscription seats",
    "claude-elx-2": "credit family pin: its own API budget, not subscription seats",
    # Legacy names still in auth.json. Those with an alias in hermes-home
    # lane-names.json reach a relay door when used unresolved, and then keep their
    # own pool verdict (named in the #294 handback); the rest have no alias.
    "claude-apr-2": "legacy per-key apr pool row, no alias in lane-names.json",
    "claude-app": "legacy pool name, no alias in lane-names.json",
    "claude-pool": "legacy pool name, no alias in lane-names.json",
    "claude-api-proxy-f1": "legacy proxy face, no alias in lane-names.json",
    "claude-bpp": "legacy alias -> claude-bpr (lane-names.json aliases)",
    "claude-api-proxy": "legacy alias -> claude-apx-0 (lane-names.json aliases)",
    "claude-proxy": "legacy alias -> claude-apx-0 (lane-names.json aliases)",
    "claude-subscription-proxy": "legacy alias -> claude-apx-0 (lane-names.json aliases)",
    "claude-bridge": "legacy alias -> claude-bpx-0 (lane-names.json aliases)",
}


@pytest.mark.parametrize("name", POOLED_TODAY)
def test_lane_table_pooled(name):
    assert is_relay_pooled(name) and not is_relay_pinned(name)


@pytest.mark.parametrize("name", PINS_TODAY)
def test_lane_table_pins(name):
    assert is_relay_pinned(name) and not is_relay_pooled(name)


@pytest.mark.parametrize("name", sorted(NOT_RELAY_TODAY))
def test_lane_table_not_relay(name):
    assert NOT_RELAY_TODAY[name]
    assert not is_relay_backed(name), NOT_RELAY_TODAY[name]


# -- the predicate is a positive match on quota evidence --

from agent.credential_pool import relay_lane_quota_is_relays as _q  # noqa: E402


@pytest.mark.parametrize("code,reason", [
    (429, None), (429, "rate_limit"),          # the 27 live shapes on the Studio
    (None, "rate_limit"), (200, "upstream_rate_limit"), (503, "pool_exhausted"),
])
def test_predicate_matches_quota_evidence(code, reason):
    for lane in ("claude-alr", "claude-apx-7"):
        assert _q(lane, code, reason) is True


@pytest.mark.parametrize("code,reason", [
    (503, None), (529, None), (529, "overloaded"), (500, "server_error"), (None, None),
    (401, None), (403, None), (429, "auth"), (None, "auth_permanent"), (402, "billing"),
])
def test_predicate_rejects_non_quota(code, reason):
    assert _q("claude-alr", code, reason) is False


def test_predicate_never_matches_non_relay():
    assert _q("deepseek", 429, "rate_limit") is False


@pytest.mark.parametrize("code,reason", [(503, None), (529, "overloaded")])
def test_relay_lane_503_is_stored_as_on_main(tmp_path, monkeypatch, code, reason):
    _auth(tmp_path, monkeypatch, {"claude-alr": [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    load_pool("claude-alr").mark_exhausted_and_rotate(
        status_code=code, credential_id="r1", api_key_hint="sk-relay", failure_reason=reason)
    assert _disk_row(tmp_path, "claude-alr", "r1").get("last_status") == "exhausted"
    assert load_pool("claude-alr").has_available() is False


def test_relay_lane_forced_refresh_failure_is_stored_as_on_main(tmp_path, monkeypatch):
    """_refresh_entry(force=True) on a non-OAuth entry marks with status None."""
    _auth(tmp_path, monkeypatch, {"claude-apr": [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    pool = load_pool("claude-apr")
    entry = pool.select()
    assert pool._refresh_entry(entry, force=True) is None
    assert _disk_row(tmp_path, "claude-apr", "r1").get("last_status") == "exhausted"


@pytest.mark.parametrize("status", [
    {"last_error_code": 429},                                         # 22 rows
    {"last_error_code": 429, "failure_reason": "rate_limit", "last_error_reset_at": "NOW+3600"},  # 4
    {"last_error_code": 429, "failure_reason": "rate_limit"},          # 1
])
def test_live_exhausted_shapes_are_ignored(tmp_path, monkeypatch, status):
    now = time.time()
    status = {k: (now + 3600 if v == "NOW+3600" else v) for k, v in status.items()}
    _auth(tmp_path, monkeypatch, {"claude-apx-9": [_row(
        "p9", "sk-pin", last_status="exhausted", last_status_at=now - 60, **status)]})
    from agent.credential_pool import load_pool

    assert load_pool("claude-apx-9").has_available() is True


def test_stored_503_row_still_benches_relay_lane(tmp_path, monkeypatch):
    _auth(tmp_path, monkeypatch, {"claude-alr": [_row(
        "r1", "sk-relay", last_status="exhausted", last_error_code=503,
        last_status_at=time.time() - 30)]})
    from agent.credential_pool import load_pool

    assert load_pool("claude-alr").has_available() is False


# -- jitter never pushes the cache past its ceiling --

def test_relay_quota_cooldown_ceiling_applies_after_jitter():
    assert fp.relay_quota_cooldown_s("quota_model", stated_reset_s=30 * 86400.0,
                                      jitter=1.1) == fp.WINDOW_CEILING_DEFAULT_S
    assert fp.relay_quota_cooldown_s("quota_model", stated_reset_s=100.0, jitter=1.1) == pytest.approx(110.0)


# -- restore gate: the relay's ineligible answer holds; nothing else changes --

FB = ("claude-bpr", "claude-opus-5-5")


def _restore_state(cls, *, provider="claude-alr", cold=False, compacted=False):
    """Past ``until``; fallback warm (or cold); optional compaction since it."""
    last_fb = T0 - (2 * 3600 if cold else 60)
    return fp.StickyState(
        primary_provider=provider, primary_model="claude-fable-5-2",
        fallback_provider=FB[0], fallback_model=FB[1], cls=cls, until_epoch=T0 - 1,
        active=True, entered_at=last_fb, last_fallback_call_epoch=last_fb,
        last_compaction_epoch=(T0 - 30) if compacted else None,
        last_primary_call_epoch=T0 - 600, last_primary_seat="sub-vps-6")


def _answer(eligible, *, with_key=True):
    obj = {"instance_id": "1:18811", "bound_seat": "sub-vps-6", "bound_eligible": eligible,
           "bound_expires_in_s": 900, "snapshot_age_s": 1}
    if with_key:
        obj["model_eligible"] = eligible
    return fp.parse_eligibility(obj)


def _decide(state, elig, provider="claude-alr", calls=None):
    def fn():
        if calls is not None:
            calls.append(1)
        return elig
    return fp.restore_allowed(state, T0, probe=True, primary_provider=provider, eligibility=fn)


def _main_decision(state, elig, provider="claude-alr"):
    """What main gives: the same gate with the relay-quota branch switched off."""
    from unittest import mock
    with mock.patch.object(fp, "RELAY_QUOTA_CLASSES", frozenset()):
        return fp.restore_allowed(state, T0, probe=True, primary_provider=provider,
                                  eligibility=lambda: elig)


@pytest.mark.parametrize("cls", ["quota_model", "quota_seat"])
@pytest.mark.parametrize("cold,compacted", [(False, False), (True, False), (False, True)])
def test_restore_holds_when_relay_says_model_ineligible(cls, cold, compacted):
    st = _restore_state(cls, cold=cold, compacted=compacted)
    d = _decide(st, _answer(False))
    assert d.allowed is False and d.reason == fp.RELAY_MODEL_INELIGIBLE_REASON
    # without the hold, cold / compaction would have returned
    if cold or compacted:
        assert _main_decision(st, _answer(False)).allowed is True


@pytest.mark.parametrize("cls", ["quota_model", "quota_seat"])
@pytest.mark.parametrize("cold,compacted", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("which", ["eligible", "none", "no_key"])
def test_restore_unchanged_when_relay_eligible_unreachable_or_keyless(cls, cold, compacted, which):
    elig = {"eligible": _answer(True), "none": None,
            "no_key": _answer(False, with_key=False)}[which]
    st = _restore_state(cls, cold=cold, compacted=compacted)
    got, want = _decide(st, elig), _main_decision(st, elig)
    assert (got.allowed, got.branch) == (want.allowed, want.branch)
    assert got.reason != fp.RELAY_MODEL_INELIGIBLE_REASON


def test_keyless_answer_is_marked_unknown():
    assert _answer(False, with_key=False).model_eligible_known is False
    assert _answer(False).model_eligible_known is True


@pytest.mark.parametrize("provider", ["claude-bpx-12", "openrouter", "anthropic"])
def test_restore_branch_not_reached_for_pin_or_non_relay(provider):
    st = _restore_state("quota_model", provider=provider, cold=True)
    d = _decide(st, _answer(False), provider=provider)
    assert d.reason != fp.RELAY_MODEL_INELIGIBLE_REASON
    assert d == _main_decision(st, _answer(False), provider=provider)


@pytest.mark.parametrize("cls", ["rate_upstream", "conn", "unclassified", "refusal"])
def test_restore_branch_not_reached_for_non_quota_class(cls):
    st = _restore_state(cls, cold=True)
    d = _decide(st, _answer(False))
    assert d.reason != fp.RELAY_MODEL_INELIGIBLE_REASON
    assert d == _main_decision(st, _answer(False))
