"""t_aa698edc: a relay quota_model (model capped on every seat) benches the model
until the relay's stated reset, even when that reset is past the 6 h ceiling for
an unidentified window.

2026-10-27: Fable 7d_oi exhausted 26/26, earliest reset ~9.6 h away. The relay
(claude-pool t_aa698edc) now answers ``x-relay-error-class: quota_model`` with
``Retry-After`` = the earliest real reset and ``x-relay-quota-window:
seven_day_overage_included``. The harness must read that window from the header
(the 503 text never says "weekly"), so the cooldown is clamped to the 7 d
ceiling, not 6 h. Drives the real ``try_activate_fallback`` (shared ``wired``
fixture: temp home, faked resolver + eligibility only).
"""

from __future__ import annotations

import time

import pytest

from agent import fallback_policy as fp
from agent.error_classifier import FailoverReason
from tests.agent.test_fallback_policy import (  # noqa: F401  (wired is a fixture)
    OPUS, _Err, _fail, _state, _wired_agent, wired,
)

RESET_S = 34_560  # the incident's first gap (12:24 PT -> 05:00Z)
TEXT = ("no eligible sub for the requested model; this model's budget is capped "
        "on every subscription while other models are unaffected")


def _model_scope_503(window="seven_day_overage_included", reset=RESET_S):
    headers = {"x-relay-error-class": "quota_model", "retry-after": str(reset),
               "x-relay-eligible": "0"}
    if window:
        headers["x-relay-quota-window"] = window
    return _Err(TEXT, 503, headers=headers, body={"error": TEXT})


def test_model_scope_quota_model_benches_until_the_stated_reset(wired):
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, _model_scope_503(), reason=FailoverReason.rate_limit) is True
    assert (a.provider, a.model) == OPUS
    st = _state(a)
    assert st is not None and st.active and st.cls == "quota_model"
    # The stated reset exactly (x U(1.0, 1.1) jitter), not the 6 h ceiling.
    cooldown = st.until_epoch - time.time()
    assert RESET_S * 0.99 <= cooldown <= RESET_S * 1.11, cooldown


def test_weekly_window_header_maps_to_the_7d_ceiling():
    for name in ("seven_day", "seven_day_overage_included", "seven_day_sonnet", "7d_oi"):
        assert fp.ceiling_window(None, {"x-relay-quota-window": name}) == "7d", name
    assert fp.ceiling_window(None, {"x-relay-quota-window": "five_hour"}) == "5h"
    assert fp.ceiling_window("5h", {"x-relay-quota-window": "seven_day"}) == "5h"  # text wins
    assert fp.ceiling_window(None, {}) is None


def test_without_a_window_the_6h_ceiling_still_holds(wired):
    """No window named: today's clamp (a malformed reset cannot bench for days)."""
    home, calls = wired
    a = _wired_agent()
    assert _fail(a, _model_scope_503(window=None), reason=FailoverReason.rate_limit) is True
    st = _state(a)
    cooldown = st.until_epoch - time.time()
    assert cooldown == pytest.approx(fp.WINDOW_CEILING_DEFAULT_S,
                                     rel=0.11), cooldown


DTLR_FABLE = ("claude-dtlr", "claude-fable-5-1")


def test_chain_walk_on_quota_model_advances_the_resume_target(wired, monkeypatch):
    """t_fc121c8e: primary Fable benched quota_model, fallback[0] is Fable on
    another relay and refuses quota_model too, the walk lands on Opus. The
    next rebuild resumes on Opus with no call to fallback[0]; the primary's
    until/n/cls are untouched by the walk (B1)."""
    from agent import fallback_wiring as fw
    from tests.agent.test_fallback_policy import FABLE

    home, calls = wired
    # Fable is capped everywhere: the primary is not eligible, so the
    # fallback_failed return is refused and the chain walks.
    monkeypatch.setattr(fw, "_eligibility_fn", lambda agent: (lambda: None))
    chain = [{"provider": DTLR_FABLE[0], "model": DTLR_FABLE[1]},
             {"provider": OPUS[0], "model": OPUS[1]}]
    a = _wired_agent()
    a._fallback_chain = list(chain)
    assert _fail(a, _model_scope_503(), reason=FailoverReason.rate_limit) is True
    assert (a.provider, a.model) == DTLR_FABLE
    before = _state(a)
    assert (before.fallback_provider, before.fallback_model) == DTLR_FABLE
    assert _fail(a, _model_scope_503(), reason=FailoverReason.rate_limit) is True
    assert (a.provider, a.model) == OPUS
    after = _state(a)
    assert (after.until_epoch, after.n, after.cls) == (before.until_epoch, before.n, before.cls)
    assert (after.fallback_provider, after.fallback_model, after.fallback_index) == (*OPUS, 1)

    b = _wired_agent()
    b._fallback_chain = list(chain)
    n_resolved = len(calls["resolve"])
    assert fw.decide_rebuild_for_agent(b) == "resume"
    assert (b.provider, b.model) == OPUS
    assert calls["resolve"][n_resolved:] == [OPUS]  # fallback[0] never called
    assert (b._primary_runtime["provider"], b._primary_runtime["model"]) == FABLE
    final = _state(b)
    assert (final.until_epoch, final.n, final.cls) == (before.until_epoch, before.n, before.cls)
