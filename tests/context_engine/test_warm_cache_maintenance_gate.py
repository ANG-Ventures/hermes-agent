"""A maintenance compaction must not burn a warm prompt cache below threshold.

Ace, 2026-08-08: "why did it compact again when i wasn't at the threshold? this
wastes kv cache for what?"

The size floor from #506/#508 answers "is the context big enough to be worth
compacting?" It cannot answer "is compacting worth it RIGHT NOW?" Measured
production case (session 20260806_084344_145f6c6d, 21:01:40):

    8 consecutive calls at 100% cache, uncached delta ~1,221 tokens/turn
    engine_preflight fired at 459,392 = 61% of the 750,000 threshold
    -> ABOVE the 375,000 floor, so the gate PERMITTED it
    next turn cost ~129,202 uncached = 106x more
    immediately followed by HTTP 429, Retry-After=600s

Measured over ALL logged compactions, 9 of 9 fired at >=90% cache. A warm cache
is the STEADY STATE of a long conversation, not bad luck — so a bare "is the
cache warm?" check would block everything, including the legitimate 750,935
threshold fire. The gate must be the CONJUNCTION:

    warm cache  AND  below the real threshold  AND  a maintenance trigger
"""

import logging
import types

import pytest

from plugins.context_engine.lcm.compaction import CompactionMixin
from plugins.context_engine.lcm.config import LCMConfig


class _Gate(CompactionMixin):
    """Minimal host exposing exactly what the gate reads."""

    def __init__(
        self,
        *,
        threshold=750_000,
        floor_ratio=0.5,
        warm_ratio=0.9,
        cache_ratio=1.0,
        cache_available=True,
    ):
        self.threshold_tokens = threshold
        self._config = types.SimpleNamespace(
            maintenance_min_pressure_ratio=floor_ratio,
            maintenance_max_cache_hit_ratio=warm_ratio,
        )
        self._cache_ratio = cache_ratio
        self.cache_metrics_available = cache_available

    @property
    def cache_read_ratio(self):
        return self._cache_ratio


# ------------------------------------------------------------- the regression


def test_the_2101_fire_is_now_deferred():
    """The exact production case Ace complained about."""
    g = _Gate(cache_ratio=0.998)
    assert g._maintenance_pressure_met(459_392) is False


def test_real_threshold_pressure_still_fires_while_warm():
    """Overflow beats cache — this must NOT regress."""
    g = _Gate(cache_ratio=1.0)
    assert g._maintenance_pressure_met(750_935) is True


def test_cold_cache_below_threshold_still_compacts():
    """Nothing warm to protect, so the maintenance work is worth doing."""
    g = _Gate(cache_ratio=0.10)
    assert g._maintenance_pressure_met(459_392) is True


@pytest.mark.parametrize(
    "tokens,cache,expected",
    [
        (459_392, 0.998, False),  # the 21:01 fire
        (459_392, 0.90, False),   # exactly at the gate
        (459_392, 0.899, True),   # just under -> allowed
        (750_935, 1.00, True),    # at threshold -> overflow wins
        (900_000, 1.00, True),    # over threshold -> overflow wins
        (100_000, 0.20, False),   # below the SIZE floor -> other gate
    ],
)
def test_conjunction_truth_table(tokens, cache, expected):
    g = _Gate(cache_ratio=cache)
    assert g._maintenance_pressure_met(tokens) is expected


# --------------------------------------------------------------- fails OPEN


def test_no_cache_metrics_fails_open():
    """No evidence to defer on -> behave exactly as before the gate existed."""
    g = _Gate(cache_ratio=0.0, cache_available=False)
    assert g._maintenance_pressure_met(459_392) is True


def test_unusable_cache_ratio_fails_open():
    g = _Gate(cache_ratio="not-a-number")
    assert g._maintenance_pressure_met(459_392) is True


def test_gate_disabled_by_default():
    """0.0 must reproduce pre-gate behavior exactly."""
    g = _Gate(warm_ratio=0.0, cache_ratio=1.0)
    assert g._maintenance_pressure_met(459_392) is True


def test_no_threshold_configured_is_permissive():
    g = _Gate(threshold=0, cache_ratio=1.0)
    assert g._maintenance_pressure_met(459_392) is True


# ------------------------------------------------------------- WIRING guards


def test_gate_is_actually_wired_into_the_pressure_check():
    """A correct helper wired into nothing is the failure that shipped 3x.

    Assert the CALL, not just the logic: _maintenance_pressure_met must consult
    _maintenance_cache_cost_acceptable, or the helper is decorative.
    """
    g = _Gate(cache_ratio=1.0)
    calls = []

    def _spy(observed):
        calls.append(observed)
        return True

    g._maintenance_cache_cost_acceptable = _spy
    g._maintenance_pressure_met(459_392)
    assert calls == [459_392], (
        "_maintenance_pressure_met must call _maintenance_cache_cost_acceptable "
        "with the observed token count"
    )


def test_size_floor_short_circuits_before_the_cache_check():
    """Below the size floor, the cache gate must not even be consulted."""
    g = _Gate(cache_ratio=0.0)
    called = []
    g._maintenance_cache_cost_acceptable = lambda o: called.append(o) or True
    assert g._maintenance_pressure_met(100_000) is False
    assert called == [], "size floor must short-circuit first"


def test_deferral_is_logged_loudly(caplog):
    """A silent defer is undiagnosable — this must be visible at INFO."""
    g = _Gate(cache_ratio=0.998)
    with caplog.at_level(logging.INFO, logger="plugins.context_engine.lcm.compaction"):
        g._maintenance_pressure_met(459_392)
    assert any("warm" in r.message.lower() for r in caplog.records), (
        "the warm-cache deferral must say so at INFO"
    )


# ------------------------------------------------------- CONFIG BRIDGE guard


def test_knob_defaults_to_disabled():
    assert LCMConfig().maintenance_max_cache_hit_ratio == 0.0


def test_knob_bridges_from_config_yaml(monkeypatch, tmp_path):
    """PR #508's lesson: a knob that never reaches the engine ships INERT.

    The uniform ENV_FIELD_SPECS loop has no config-file fallback, so this MUST
    have an explicit reader. Build the config the way production does and assert
    the value arrives.
    """
    import plugins.context_engine.lcm.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "_hermes_compression_float",
        lambda key, default: 0.9 if key == "maintenance_max_cache_hit_ratio" else default,
    )
    monkeypatch.delenv("LCM_MAINTENANCE_MAX_CACHE_HIT_RATIO", raising=False)
    built = cfg_mod.LCMConfig.from_env()
    assert built.maintenance_max_cache_hit_ratio == 0.9, (
        "compression.maintenance_max_cache_hit_ratio in config.yaml must reach "
        "the engine; if it doesn't, the gate is inert no matter how correct"
    )


def test_env_override_wins():
    import os

    import plugins.context_engine.lcm.config as cfg_mod

    old = os.environ.get("LCM_MAINTENANCE_MAX_CACHE_HIT_RATIO")
    os.environ["LCM_MAINTENANCE_MAX_CACHE_HIT_RATIO"] = "0.75"
    try:
        assert cfg_mod.LCMConfig.from_env().maintenance_max_cache_hit_ratio == 0.75
    finally:
        if old is None:
            os.environ.pop("LCM_MAINTENANCE_MAX_CACHE_HIT_RATIO", None)
        else:
            os.environ["LCM_MAINTENANCE_MAX_CACHE_HIT_RATIO"] = old
