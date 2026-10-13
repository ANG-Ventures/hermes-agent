"""``compression.per_model_builtins``: one universal threshold unless the knob opts in.

Ace ruling 2026-10-12 ("I want it to be universal"): with the knob false (the default, and
the value when the key is absent) every built-in family resolves to the GLOBAL threshold.
``codex_gpt55_autoraise`` is a legacy alias read only when the new key is unset.
"""

from __future__ import annotations

import pytest

from agent.auxiliary_client import (
    _compression_threshold_for_model,
    per_model_builtins_enabled,
    resolve_compression_threshold,
)

# Every family the built-in table knows: (model, provider).
BUILTIN_FAMILIES = [
    ("gpt-5.3-codex-spark", "openai-codex"),
    ("gpt-5.5", "openai-codex"),
    ("gpt-5.4-pro", "openai-codex"),
    ("trinity-large-thinking", "openrouter"),
]


def test_every_family_has_a_builtin() -> None:
    # Guards the parametrization below: each row really is a built-in exception.
    for model, provider in BUILTIN_FAMILIES:
        assert _compression_threshold_for_model(model, provider) is not None, model


@pytest.mark.parametrize("model,provider", BUILTIN_FAMILIES)
@pytest.mark.parametrize("compression_cfg", [
    {},  # key absent: default must be universal
    {"per_model_builtins": False},
    {"per_model_builtins": "false"},
    {"codex_gpt55_autoraise": False},  # legacy alias
    {"per_model_builtins": False, "codex_gpt55_autoraise": True},  # new key wins
])
def test_knob_off_returns_global_for_every_builtin(model, provider, compression_cfg) -> None:
    assert per_model_builtins_enabled(compression_cfg) is False
    assert resolve_compression_threshold(
        None, model, provider, global_threshold=0.65, per_model_builtins=False,
    ) == 0.65
    # Default kwarg is the universal behaviour too.
    assert resolve_compression_threshold(None, model, provider, global_threshold=0.65) == 0.65


@pytest.mark.parametrize("model,provider", BUILTIN_FAMILIES)
@pytest.mark.parametrize("compression_cfg", [
    {"per_model_builtins": True},
    {"codex_gpt55_autoraise": True},  # legacy alias, new key unset
])
def test_knob_on_restores_legacy_builtins(model, provider, compression_cfg) -> None:
    assert per_model_builtins_enabled(compression_cfg) is True
    assert resolve_compression_threshold(
        None, model, provider, global_threshold=0.65, per_model_builtins=True,
    ) == _compression_threshold_for_model(model, provider)


def test_explicit_per_model_map_still_wins_with_knob_off() -> None:
    assert resolve_compression_threshold(
        {"gpt-5.3-codex-spark": 0.5}, "gpt-5.3-codex-spark", "openai-codex",
        global_threshold=0.65,
    ) == 0.5


def _agent_threshold(monkeypatch, compression: dict, model: str, provider: str):
    """Run the real agent_init config parse against a stub agent."""
    from types import SimpleNamespace

    from agent.agent_init import _compression_threshold

    agent = SimpleNamespace(model=model, provider=provider)
    threshold, _notice, _global, _pm, builtins = _compression_threshold(agent, compression)
    return threshold, builtins, agent._compression_threshold_autoraised


@pytest.mark.parametrize("model,provider", BUILTIN_FAMILIES)
def test_agent_init_universal_by_default(monkeypatch, model, provider) -> None:
    threshold, builtins, raised = _agent_threshold(monkeypatch, {"threshold": 0.65}, model, provider)
    assert (threshold, builtins, raised) == (0.65, False, None)


def test_agent_init_knob_on_raises_spark(monkeypatch) -> None:
    threshold, builtins, raised = _agent_threshold(
        monkeypatch, {"threshold": 0.50, "per_model_builtins": True}, "gpt-5.3-codex-spark", "openai-codex",
    )
    assert (threshold, builtins) == (0.70, True)
    assert raised == {"model": "gpt-5.3-codex-spark", "from": 0.50, "to": 0.70}


@pytest.mark.parametrize("yaml_body,expected", [
    ("compression:\n  threshold: 0.65\n", False),  # nothing set: universal
    ("compression:\n  codex_gpt55_autoraise: true\n", True),  # legacy alias survives the defaults merge
    ("compression:\n  per_model_builtins: false\n  codex_gpt55_autoraise: true\n", False),  # new key wins
])
def test_merged_defaults_do_not_mask_legacy_alias(tmp_path, monkeypatch, yaml_body, expected) -> None:
    from hermes_cli.config import load_config_readonly

    (tmp_path / "config.yaml").write_text(yaml_body)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert per_model_builtins_enabled(load_config_readonly()["compression"]) is expected
