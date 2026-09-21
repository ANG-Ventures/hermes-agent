"""Cron's ``model`` object accepts the unified ``reasoning_effort`` key.

Ace 2026-09-21: the override object is ``{model, provider, reasoning_effort}``
on all three surfaces (delegate_task, kanban, cronjob). Cron already took a
sibling top-level ``reasoning_effort``; these tests pin that a caller using
the UNIFIED nested shape is honoured rather than silently dropped — the exact
silent-fallthrough class this work exists to close.

Note the deliberate asymmetry: the fork's parity pin (2026-08-30) keeps
``reasoning_effort`` OFF cron's model-facing schema, because models never
choose model config here. So the key is accepted by the handler (CLI and
internal callers) without being advertised to the model.
"""

import tools.cronjob_tools as ct


def _captured(monkeypatch):
    """Run the model-facing handler and capture what reached cronjob()."""
    seen = {}

    def _fake_cronjob(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ct, "cronjob", _fake_cronjob)
    return seen


def test_nested_reasoning_effort_is_forwarded(monkeypatch):
    seen = _captured(monkeypatch)
    ct._cronjob_tool_handler(
        {
            "action": "list",
            "model": {"model": "gpt-5.6-sol-900k", "reasoning_effort": "high"},
        }
    )
    assert seen["reasoning_effort"] == "high"


def test_top_level_reasoning_effort_still_wins(monkeypatch):
    """The explicit sibling arg is the more specific statement of intent."""
    seen = _captured(monkeypatch)
    ct._cronjob_tool_handler(
        {
            "action": "list",
            "reasoning_effort": "low",
            "model": {"model": "gpt-5.6-sol-900k", "reasoning_effort": "high"},
        }
    )
    assert seen["reasoning_effort"] == "low"


def test_absent_effort_stays_none(monkeypatch):
    seen = _captured(monkeypatch)
    ct._cronjob_tool_handler(
        {"action": "list", "model": {"model": "gpt-5.6-sol-900k"}}
    )
    assert seen["reasoning_effort"] is None


def test_model_and_provider_still_resolve(monkeypatch):
    """The nested-effort addition must not disturb model/provider resolution."""
    seen = _captured(monkeypatch)
    ct._cronjob_tool_handler(
        {
            "action": "list",
            "model": {
                "model": "gpt-5.6-sol-900k",
                "provider": "openai-codex",
                "reasoning_effort": "high",
            },
        }
    )
    assert seen["model"] == "gpt-5.6-sol-900k"
    assert seen["provider"] == "openai-codex"


def test_reasoning_effort_stays_off_the_model_facing_schema():
    """Fork parity pin 2026-08-30: models never choose model config on cron.

    Guards against a future edit "unifying" the schema and silently handing
    the model a knob this fork deliberately withheld.
    """
    props = ct.CRONJOB_SCHEMA["parameters"]["properties"]
    assert "reasoning_effort" not in props
    assert "reasoning_effort" not in props["model"]["properties"]
