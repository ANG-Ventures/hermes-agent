"""Pre-API / preflight trigger must not re-scale a usage-anchored REAL figure.

Incident 2026-09-25 (t_bd01a34b): the pre-API arm replaced the rough request
estimate with ``anchored_context_tokens`` (provider-real prompt_tokens +
completion + rough delta) and then fed it to ``should_compress_calibrated``,
which multiplies its input by the rough-estimator skew. Real 488,061 x 1.547 =
755,030 >= 750,000 fired at 49% of a 1M window; 11/11 fires that day were
below threshold. The skew is rough/real, so it applies to ROUGH input only.
"""
from __future__ import annotations

import logging

import pytest

from agent import context_engine as ce
from agent.context_engine import (
    ContextEngine,
    should_compress_request,
    trigger_compare_tokens_for,
)

SKEW = 1.547
THRESHOLD = 750_000
CTX = 1_000_000


class _Engine(ContextEngine):
    def __init__(self, *, skews=(SKEW,), hard_frac: float = 0.95) -> None:
        self.threshold_tokens = THRESHOLD
        self.context_length = CTX
        self._skew_floor = 0.55
        self._hard_frac = hard_frac
        self._recent_skews = list(skews)
        self.seen: list[int] = []

    @property
    def name(self) -> str:
        return "test-engine"

    def update_from_response(self, usage) -> None:
        pass

    def should_compress(self, prompt_tokens: int = None) -> bool:
        self.seen.append(prompt_tokens)
        return (prompt_tokens or 0) >= self.threshold_tokens

    def compress(self, messages, current_tokens=None, focus_topic=None):
        return messages


@pytest.fixture(autouse=True)
def _scale_up_on(monkeypatch):
    # Live config has scale-up on (skew 1.547 > 1.0 is only possible then).
    monkeypatch.setattr(ce, "_scale_up_calibration_enabled", lambda: True)


def test_incident_anchored_real_is_not_skew_multiplied() -> None:
    e = _Engine()
    assert e._trigger_skew() == pytest.approx(SKEW)
    # Pre-fix path: the anchored value passed as if rough -> false fire.
    assert e.should_compress_calibrated(488_061) is True
    # Fixed path: anchored value compared unscaled -> no fire.
    assert should_compress_request(e, 314_682, None, anchored_tokens=488_061) is False
    assert e.seen[-1] == 488_061
    for real in (488_463, 513_183, 545_945):  # the other logged fires
        assert should_compress_request(e, 314_682, None, anchored_tokens=real) is False


def test_rough_without_anchor_still_calibrated() -> None:
    e = _Engine()
    # 314,682 x 1.547 = 486,813 -> no fire
    assert should_compress_request(e, 314_682, None) is False
    assert e.seen[-1] == round(314_682 * SKEW)
    # 500,000 x 1.547 = 773,500 -> fire (skew still applies to rough)
    assert should_compress_request(e, 500_000, None) is True
    assert e.seen[-1] == round(500_000 * SKEW)


def test_anchored_over_threshold_still_fires() -> None:
    e = _Engine()
    assert should_compress_request(e, 400_000, None, anchored_tokens=760_000) is True


def test_hard_frac_backstop_fires_on_raw_rough_regardless_of_anchor() -> None:
    e = _Engine(skews=(0.10,))
    # Anchored says tiny, rough is at the 95% ceiling: the 413 backstop wins.
    assert should_compress_request(e, 950_000, None, anchored_tokens=200_000) is True
    assert e.seen[-1] == 950_000
    assert trigger_compare_tokens_for(e, 950_000, None, 200_000) == 950_000


def test_legacy_plugin_without_anchored_kwarg_gets_plain_should_compress() -> None:
    class _Legacy(_Engine):
        def should_compress_calibrated(self, rough_tokens, messages=None):
            raise AssertionError("must not skew-scale an anchored figure")

    e = _Legacy()
    assert should_compress_request(e, 314_682, None, anchored_tokens=488_061) is False
    assert e.seen[-1] == 488_061


def test_legacy_plugin_anchored_path_keeps_hard_frac_backstop() -> None:
    class _Legacy(_Engine):
        def should_compress_calibrated(self, rough_tokens, messages=None):
            raise AssertionError("must not skew-scale an anchored figure")

    e = _Legacy()
    assert should_compress_request(e, 960_000, None, anchored_tokens=200_000) is True
    assert e.seen[-1] == 960_000


def test_kwargs_wrapper_rejecting_anchored_falls_back() -> None:
    e = _Engine()

    def _gate(*args, **kwargs):
        if kwargs:
            raise TypeError("unexpected keyword argument 'anchored_tokens'")
        return True

    e.should_compress_calibrated = _gate
    assert should_compress_request(e, 314_682, None, anchored_tokens=488_061) is False


def test_helpers_forward_messages_to_the_calibration() -> None:
    e = _Engine()
    seen = []
    orig = e._trigger_calibrated_tokens

    def _spy(rough, messages=None):
        seen.append(messages)
        return orig(rough, messages)

    e._trigger_calibrated_tokens = _spy
    msgs = [{"role": "user", "content": "x"}]
    should_compress_request(e, 100_000, msgs)
    trigger_compare_tokens_for(e, 100_000, msgs)
    assert seen == [msgs, msgs]


def test_compared_tokens_match_what_the_gate_tested() -> None:
    """The log prints trigger_compare_tokens_for; it must equal the value
    should_compress received, so a printed '>= threshold' is true."""
    e = _Engine()
    for rough, anchored in ((500_000, None), (314_682, 488_061), (960_000, 1)):
        fired = should_compress_request(e, rough, None, anchored_tokens=anchored)
        compared = trigger_compare_tokens_for(e, rough, None, anchored)
        assert compared == e.seen[-1]
        assert fired == (compared >= THRESHOLD)


def test_pre_api_log_line_prints_compared_value(caplog) -> None:
    """Source-level pin: the Pre-API log's first figure is the compared value."""
    import inspect

    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)
    i = src.index('"Pre-API compression: ~%s compared tokens >= %s threshold "')
    window = src[i : i + 600]
    assert "_trigger_compare_tokens_for(" in window
    assert "_should_compress_request(" in src
    assert "should_compress_calibrated\", _compressor.should_compress" not in src
