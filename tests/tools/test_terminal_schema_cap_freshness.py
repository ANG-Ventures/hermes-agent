"""The advertised foreground cap must match the ENFORCED one.

`TERMINAL_SCHEMA` is built at import; the config->env bridge
(`_ensure_terminal_env_bridged`) runs on first tool use, AFTER import. So a
`terminal.max_foreground_timeout` set in config.yaml was enforced correctly
but still ADVERTISED as the old value.

Measured 2026-08-11 with the cap set to 3600 in config.yaml:

    at IMPORT   -> constant: 600
    schema BEFORE bridge:    600
    enforced AFTER bridge:   3600
    schema AFTER bridge:     600     <- the model is told 600

This is not cosmetic. The description is the ONLY thing the model reads when
choosing a timeout, so an understated cap silently holds the model to the old
limit and makes the config key look inert even though enforcement honors it.
"""

from __future__ import annotations

import re

import pytest


def _cap_in(description: str) -> int:
    m = re.search(r"foreground max: (\d+)", description)
    assert m, f"cap not found in description: {description[:120]}"
    return int(m.group(1))


def _schema_cap(tt) -> int:
    return _cap_in(tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"])


def test_schema_matches_enforcement_after_the_bridge(monkeypatch):
    """The regression: raise the cap post-import, refresh, both agree."""
    import tools.terminal_tool as tt

    original = tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"]
    try:
        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "3600")
        tt._refresh_terminal_schema_limits()
        assert tt._foreground_max_timeout() == 3600
        assert _schema_cap(tt) == 3600, (
            "the schema advertises a stale cap — the model will hold itself to "
            "the old limit even though enforcement allows more"
        )
    finally:
        tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"] = original


def test_schema_follows_a_LOWERED_cap_too(monkeypatch):
    """Drift in the other direction is worse: advertising MORE than is allowed
    invites the model to pick a timeout that gets rejected."""
    import tools.terminal_tool as tt

    original = tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"]
    try:
        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "120")
        tt._refresh_terminal_schema_limits()
        assert tt._foreground_max_timeout() == 120
        assert _schema_cap(tt) == 120
    finally:
        tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"] = original


def test_refresh_mutates_the_registered_object_not_a_copy():
    """The registry holds a REFERENCE; rebinding the name would leave it stale.

    This is the assumption the whole in-place-mutation approach rests on, so
    it gets asserted rather than trusted.
    """
    import tools.terminal_tool as tt
    from tools.registry import registry

    entry = registry.get_entry("terminal")
    assert entry is not None, "terminal tool is not registered"
    assert entry.schema is tt.TERMINAL_SCHEMA, (
        "the registry does not share the module's schema object — an in-place "
        "refresh would not reach the model"
    )

    # And the refresh must be visible THROUGH the registry, not just the module.
    import os
    original = tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"]
    prev = os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT")
    try:
        os.environ["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = "2700"
        tt._refresh_terminal_schema_limits()
        assert _cap_in(
            entry.schema["parameters"]["properties"]["timeout"]["description"]
        ) == 2700
    finally:
        tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"] = original
        if prev is None:
            os.environ.pop("TERMINAL_MAX_FOREGROUND_TIMEOUT", None)
        else:
            os.environ["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = prev


def test_bridge_refreshes_the_schema(monkeypatch):
    """End-to-end: the bridge itself must leave the schema correct."""
    import tools.terminal_tool as tt

    original = tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"]
    try:
        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "1800")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", False, raising=False)
        tt._ensure_terminal_env_bridged()
        assert _schema_cap(tt) == tt._foreground_max_timeout()
    finally:
        tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"] = original


def test_refresh_runs_even_when_the_bridge_raises(monkeypatch):
    """`finally`: a broken config must not leave schema and enforcement disagreeing."""
    import tools.terminal_tool as tt

    original = tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"]
    try:
        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "2400")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", False, raising=False)

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("hermes_cli.config.read_raw_config", _boom, raising=False)
        tt._ensure_terminal_env_bridged()
        assert _schema_cap(tt) == tt._foreground_max_timeout() == 2400
    finally:
        tt.TERMINAL_SCHEMA["parameters"]["properties"]["timeout"]["description"] = original


def test_description_builder_is_the_single_source_of_truth():
    """Source contract: the literal must not re-hardcode the cap text."""
    import inspect
    import tools.terminal_tool as tt

    src = inspect.getsource(tt)
    schema_region = src[src.index("TERMINAL_SCHEMA = {"):]
    assert "_timeout_param_description(" in schema_region, (
        "the schema literal stopped using the shared builder — the two copies "
        "of the cap text will drift"
    )
    assert "foreground max: {FOREGROUND_MAX_TIMEOUT}" not in schema_region
