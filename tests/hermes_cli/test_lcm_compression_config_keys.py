"""`hermes config set compression.<lcm_knob>` must not cry wolf.

The LCM context-engine plugin reads several `compression.*` keys through its own
explicit `_hermes_compression_float` bridge (plugins/context_engine/lcm/config.py).
Those keys were absent from DEFAULT_CONFIG, so `_validate_config_key` classified
every one of them as unknown and `hermes config set` printed:

    ⚠ 'compression.skew_floor' is not a recognized config key — it was saved
      anyway, but Hermes may not read it.

The runtime DOES read them. Measured 2026-08-09: 4 of the 5 keys the plugin reads
warned falsely, including `skew_floor`, which had been warning since it shipped.

This matters beyond tidiness: a warning that is wrong by construction trains the
operator to ignore it, and the identical message is the ONLY signal for a genuinely
inert knob (the failure mode that shipped three times in this subsystem). A false
positive here disarms a real alarm elsewhere.
"""

import re
from pathlib import Path

import pytest

from hermes_cli.config import DEFAULT_CONFIG, _validate_config_key

LCM_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "context_engine" / "lcm" / "config.py"
)


def _keys_the_plugin_reads() -> set[str]:
    """Every `compression.*` key the LCM plugin reads, straight from its source.

    Derived, not hardcoded: a new knob added to the plugin is picked up here
    automatically, so this test fails the moment someone adds a bridge entry
    without declaring it in the schema.
    """
    src = LCM_CONFIG.read_text()
    return set(re.findall(r'_hermes_compression_\w+\(\s*["\']([\w_]+)["\']', src))


def test_the_plugin_actually_reads_some_compression_keys():
    """Positive control — a regex matching nothing would make every test vacuous."""
    keys = _keys_the_plugin_reads()
    assert len(keys) >= 4, f"expected the LCM bridge to read several keys, found {keys}"


@pytest.mark.parametrize("knob", sorted(_keys_the_plugin_reads()))
def test_every_lcm_compression_knob_validates(knob):
    """No key the runtime reads may be reported as unrecognized."""
    key = f"compression.{knob}"
    is_known, _ = _validate_config_key(key)
    assert is_known, (
        f"{key} is read by the LCM plugin but not declared in DEFAULT_CONFIG, so "
        f"`hermes config set {key}` warns that Hermes may not read it — it does. "
        "Add it to config_defaults.py's compression block."
    )


@pytest.mark.parametrize("knob", sorted(_keys_the_plugin_reads()))
def test_declared_knobs_are_present_in_the_schema(knob):
    """The schema entry must exist, not merely pass an open-container escape."""
    assert knob in DEFAULT_CONFIG.get("compression", {}), (
        f"compression.{knob} must be a real schema entry so `config show` and the "
        "did-you-mean suggester can see it"
    )


def test_a_typo_is_still_caught_and_suggested():
    """NEGATIVE CONTROL: widening the schema must not blunt typo detection.

    If this ever passes as 'known', the fix has degenerated into accepting
    anything under `compression.`, which would hide real mistakes.
    """
    is_known, suggestion = _validate_config_key(
        "compression.maintenance_min_presure_ratio"  # note: missing an 's'
    )
    assert is_known is False
    assert suggestion == "compression.maintenance_min_pressure_ratio", (
        f"expected a did-you-mean pointing at the real key, got {suggestion!r}"
    )


def test_unrelated_garbage_under_compression_is_still_unknown():
    is_known, _ = _validate_config_key("compression.definitely_not_a_real_knob")
    assert is_known is False


def test_core_compression_keys_still_validate():
    """Regression guard for the pre-existing keys."""
    for key in ("compression.threshold", "compression.target_ratio", "compression.enabled"):
        is_known, _ = _validate_config_key(key)
        assert is_known, f"{key} regressed to unknown"
