"""Every `compression.*` knob the LCM plugin reads must ARRIVE from config.yaml.

The failure this exists to stop is not a wrong value — it is a knob that is
**inert**: declared, documented, tested in isolation, and never reaching the
running engine. Five shipped that way in one arc (2026-08-08..10):

  #506  gate helper wired into 1 of 2 call sites
  #508  knob added to the uniform ENV loop, which has no config-file fallback
  #554  ContextEngine ABC lacked bind_session_state; agent_init's getattr()
        skipped the bind SILENTLY, so skew persistence never ran
  #563  a shipped DEFAULT read as an operator's explicit value, so PR #528's
        timeout floor never applied
  #537  the `hermes config set` validator called 4 of these keys unknown

Every one had green tests. Every one was inert in production. The common shape:
**the test exercised the function directly and never went through the real
config-resolution path.**

Per-knob guards now exist for the four that bit (test_lcm_maintenance_floor_wiring,
test_context_engine_session_binding, test_config_provenance_default_vs_explicit,
the #537 schema test). Each was written AFTER its own incident. This file is the
one that generalises: it **enumerates the knobs from the plugin source** and
proves each one arrives, so knob N+1 is covered before anyone writes it.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, REPO_ROOT)

LCM_CONFIG = os.path.join(
    REPO_ROOT, "plugins", "context_engine", "lcm", "config.py"
)

# Bridge helpers that read `compression.<key>` out of the user's config.yaml.
_BRIDGE_CALL = re.compile(
    r"_hermes_compression_(?:float|int|bool|str)\(\s*[\"']([A-Za-z0-9_]+)[\"']"
)


def _bridged_keys() -> set[str]:
    """Keys the LCM plugin currently reads from `compression.*`, from source."""
    if not os.path.exists(LCM_CONFIG):
        pytest.skip("LCM plugin config not present in this tree")
    src = open(LCM_CONFIG, encoding="utf-8").read()
    return set(_BRIDGE_CALL.findall(src))


def _knobs_under_test() -> set[str]:
    """The population to sweep — derived from BOTH sides, deliberately.

    🔴 The obvious implementation (enumerate the `_hermes_compression_float`
    call sites) is a VACUOUS ORACLE: deleting a knob's bridge — which is
    exactly the #508 defect — also deletes it from the population, so the
    sweep goes green on the very regression it exists to catch. Proven by
    mutation 2026-08-10: removing the `maintenance_min_pressure_ratio` bridge
    left 5/5 passing.

    The fix is to take the population from the INTERSECTION of what the engine
    exposes (LCMConfig fields) and what config.yaml documents (DEFAULT_CONFIG's
    compression block). Both sides survive a deleted bridge, so the knob stays
    under test and the missing bridge shows up as a non-arrival.
    """
    import dataclasses

    from plugins.context_engine.lcm.config import LCMConfig

    try:
        fields = {f.name for f in dataclasses.fields(LCMConfig)}
    except TypeError:  # pragma: no cover - not a dataclass
        fields = {n for n in dir(LCMConfig) if not n.startswith("_")}

    declared = set(_default_config_compression())
    return fields & declared


def _default_config_compression() -> dict:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    block = DEFAULT_CONFIG.get("compression")
    return block if isinstance(block, dict) else {}


def test_the_sweep_finds_knobs_at_all() -> None:
    """Positive control.

    A regex that matches nothing would make every assertion below vacuous and
    pass forever — the exact fake-green this suite exists to prevent.
    """
    keys = _knobs_under_test()
    assert len(keys) >= 4, (
        f"expected several bridged compression knobs, found {sorted(keys)}. "
        f"If the bridge helpers were renamed, update _BRIDGE_CALL — do NOT "
        f"let this sweep silently cover nothing."
    )


def test_every_bridged_knob_is_declared_in_default_config() -> None:
    """A knob absent from DEFAULT_CONFIG is invisible to `hermes config set`.

    That is the #537 defect: the validator walks DEFAULT_CONFIG, so an
    undeclared key is reported as "not a recognized config key — Hermes may not
    read it" even though the plugin reads it fine. A warning that is wrong by
    design trains the operator to ignore the one time it is right.
    """
    declared = _default_config_compression()
    missing = sorted(k for k in _bridged_keys() if k not in declared)
    assert not missing, (
        "these compression knobs are read by the LCM plugin but NOT declared "
        "in DEFAULT_CONFIG, so `hermes config set` will call them unknown:\n  "
        + "\n  ".join(missing)
    )


def test_every_bridged_knob_actually_arrives_from_config_yaml(monkeypatch) -> None:
    """THE sweep: feed each knob a distinctive value; assert the engine sees it.

    Drives the REAL loader (``LCMConfig.from_env()``) with the bridge helper
    monkeypatched — the same shape the per-knob guards use, but enumerated over
    every key instead of the one that happened to bite. A knob that only works
    when you hand-build the dataclass is precisely the inert shape.

    Note ``_hermes_compression_float`` clamps to ``0 < v <= 1``, so probes must
    live inside that band; a value outside it is silently replaced by the
    default and would make this sweep look broken when it is the knob's own
    (deliberate) validation.
    """
    from plugins.context_engine.lcm import config as cfg_mod

    declared = _default_config_compression()
    failures: list[str] = []

    for key in sorted(_knobs_under_test()):
        shipped = declared.get(key)
        if not isinstance(shipped, (int, float)) or isinstance(shipped, bool):
            continue

        # Inside the helper's accepted band, distinct from the shipped default
        # and from 0/1 sentinels.
        probe = 0.375 if float(shipped) != 0.375 else 0.625

        monkeypatch.setattr(
            cfg_mod,
            "_hermes_compression_float",
            lambda k, default, _key=key, _p=probe: _p if k == _key else default,
        )
        monkeypatch.delenv(f"LCM_{key.upper()}", raising=False)

        built = cfg_mod.LCMConfig.from_env()
        got = getattr(built, key, None)

        if got is None:
            failures.append(f"{key}: LCMConfig has no attribute named {key!r}")
        elif abs(float(got) - probe) > 1e-9:
            failures.append(
                f"{key}: compression.{key}={probe} in config.yaml, engine "
                f"resolved {got} (shipped default {shipped}) — does not ARRIVE"
            )

    assert not failures, (
        "these compression knobs do not reach the running engine:\n  "
        + "\n  ".join(failures)
        + "\n\nA knob that resolves to its default no matter what the operator "
          "sets is inert. Bridge it explicitly via _hermes_compression_float — "
          "the uniform ENV loop has NO config-file fallback (that was #508)."
    )


def test_live_config_knobs_resolve_through_the_production_loader() -> None:
    """The end-to-end shape: read the ACTUAL config, assert the gates are sane.

    Deliberately asserts invariants rather than exact values, so an operator
    retuning a threshold does not turn this red — but a knob silently pinned at
    a default it can never leave still shows up in the sweep above.
    """
    from plugins.context_engine.lcm.config import LCMConfig

    cfg = LCMConfig.from_env()

    ratio = getattr(cfg, "maintenance_min_pressure_ratio", None)
    if ratio is not None:
        assert 0.0 <= float(ratio) <= 1.0, (
            f"maintenance_min_pressure_ratio={ratio} is outside [0,1]; it is a "
            f"FRACTION of the threshold, not an absolute token count"
        )

    cache = getattr(cfg, "maintenance_max_cache_hit_ratio", None)
    if cache is not None:
        assert 0.0 <= float(cache) <= 1.0, (
            f"maintenance_max_cache_hit_ratio={cache} is outside [0,1]"
        )


def test_the_outer_compression_guard_never_undercuts_the_inner_deadline() -> None:
    """#563's invariant, asserted against the REAL config rather than a fixture.

    If the outer no-progress watchdog fires before the inner auxiliary
    deadline, `call_llm` never raises and every configured `fallback_providers`
    entry is structurally unreachable on a stall. That defect shipped twice:
    once as the original bug, once as an inert fix for it.
    """
    from agent.auxiliary_client import _effective_aux_timeout
    from agent.conversation_compression import (
        resolve_context_compression_timeouts,
    )

    inner = _effective_aux_timeout("compression", None)
    if not inner or inner <= 0:
        pytest.skip("no inner auxiliary compression deadline configured")

    idle, _ceiling = resolve_context_compression_timeouts()
    if idle <= 0:
        pytest.skip("operator disabled the owned progress wrapper (idle <= 0)")

    assert idle > inner, (
        f"outer no-progress guard {idle}s <= inner aux deadline {inner}s — a "
        f"stalled summariser is abandoned before call_llm can raise, so the "
        f"configured fallback_providers chain cannot engage"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
