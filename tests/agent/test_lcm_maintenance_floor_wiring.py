"""config.yaml -> LCMConfig wiring for the maintenance pressure floor.

PR #506 added ``maintenance_min_pressure_ratio`` to LCMConfig and gated the
compaction path on it, with 17 passing tests — and the knob was still INERT in
production. config.yaml said 0.5; the engine built by the production loader
read 0.0, because nothing bridged ``compression.maintenance_min_pressure_ratio``
into the dataclass. Every unit test passed because they all constructed the
config object directly.

The uniform ENV_FIELD_SPECS loop cannot express a config-file fallback (see the
"Fork-only calibration knobs" comment at the explicit block), so a field that
needs one MUST be read explicitly alongside skew_floor / calibration_hard_frac.

These tests assert the wiring end to end, the way the gateway actually loads it.
"""

import textwrap

import pytest

from plugins.context_engine.lcm.config import LCMConfig


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A temp HERMES_HOME whose config.yaml the loader will actually read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LCM_MAINTENANCE_MIN_PRESSURE_RATIO", raising=False)
    return tmp_path


def _write_config(home, body):
    (home / "config.yaml").write_text(textwrap.dedent(body))


def test_floor_reaches_the_engine_from_config_yaml(hermes_home):
    """The regression that shipped: config said 0.5, the engine read 0.0."""
    _write_config(hermes_home, """
        compression:
          threshold: 0.75
          maintenance_min_pressure_ratio: 0.5
    """)
    cfg = LCMConfig.from_env()
    assert cfg.maintenance_min_pressure_ratio == pytest.approx(0.5), (
        "compression.maintenance_min_pressure_ratio must bridge into LCMConfig; "
        "if this reads 0.0 the gate is present but inert in production"
    )


def test_absent_key_keeps_the_safe_default(hermes_home):
    """No key configured must mean upstream behavior (floor disabled)."""
    _write_config(hermes_home, """
        compression:
          threshold: 0.75
    """)
    assert LCMConfig.from_env().maintenance_min_pressure_ratio == 0.0


def test_env_override_wins_over_config_file(hermes_home, monkeypatch):
    """Same precedence as the sibling calibration knobs: env > config > default."""
    _write_config(hermes_home, """
        compression:
          maintenance_min_pressure_ratio: 0.5
    """)
    monkeypatch.setenv("LCM_MAINTENANCE_MIN_PRESSURE_RATIO", "0.65")
    assert LCMConfig.from_env().maintenance_min_pressure_ratio == pytest.approx(0.65)


def test_malformed_value_does_not_explode_the_loader(hermes_home):
    """A typo in config.yaml must not take the whole engine down."""
    _write_config(hermes_home, """
        compression:
          maintenance_min_pressure_ratio: "not-a-number"
    """)
    cfg = LCMConfig.from_env()  # must not raise
    assert isinstance(cfg.maintenance_min_pressure_ratio, float)


def test_the_knob_is_not_in_the_uniform_env_loop(hermes_home):
    """Guard against a double-read reintroducing the bug.

    The uniform ENV_FIELD_SPECS loop has no config-file fallback. If this field
    is ALSO listed there it runs after/before the explicit reader depending on
    order and can clobber the config.yaml value back to the default — which is
    exactly the shape of the original inert-knob bug.
    """
    from plugins.context_engine.lcm.config import ENV_FIELD_SPECS

    names = [getattr(s, "name", getattr(s, "field", None)) for s in ENV_FIELD_SPECS]
    assert "maintenance_min_pressure_ratio" not in names, (
        "read explicitly (needs a config-file fallback), not via the uniform loop"
    )


def test_every_compression_backed_knob_actually_bridges(hermes_home):
    """Contract: the fork's compression.* knobs must all reach LCMConfig.

    Written as a family assertion rather than one case, so the NEXT knob added
    this way fails here instead of silently shipping inert.
    """
    _write_config(hermes_home, """
        compression:
          skew_floor: 0.61
          calibration_hard_frac: 0.91
          maintenance_min_pressure_ratio: 0.42
    """)
    cfg = LCMConfig.from_env()
    assert cfg.skew_floor == pytest.approx(0.61)
    assert cfg.calibration_hard_frac == pytest.approx(0.91)
    assert cfg.maintenance_min_pressure_ratio == pytest.approx(0.42)


def test_dataclass_default_is_disabled():
    """Shipping default must be the no-op, so upgrades change nothing."""
    assert LCMConfig().maintenance_min_pressure_ratio == 0.0
