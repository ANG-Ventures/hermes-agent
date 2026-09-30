"""Inherit-branch subagents re-gate the parent's tier override (t_97d48533).

With no delegation.provider the child copies parent.request_overrides
verbatim, but delegation.model may move it to a different route, e.g. a
gpt-6-astra ultrafast parent spawning a gpt-5.4-mini child. The tier keys were
gated against the parent's route and must be re-derived for the child's.
"""

import inspect
from unittest.mock import patch

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent, _resolve_delegation_credentials


class _FakeChild:
    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        self.provider = kwargs.get("provider")
        self.api_mode = kwargs.get("api_mode")
        self.service_tier = kwargs.get("service_tier")
        self.request_overrides = dict(kwargs.get("request_overrides") or {})
        self.session_id = "child-session"


def _ultrafast_parent():
    parent = _make_mock_parent()
    parent.provider = "openai-codex"
    parent.api_mode = "codex_responses"
    parent.base_url = "https://chatgpt.com/backend-api/codex"
    parent.model = "gpt-6-astra"
    parent.service_tier = "ultrafast"
    parent.request_overrides = {"service_tier": "ultrafast", "extra_body": {"k": 1}}
    return parent


def _spawn(parent, cfg):
    creds = _resolve_delegation_credentials(cfg, parent)
    assert creds["provider"] is None  # inherit branch
    extra = {}
    # Passed only where supported so the pre-fix base fails on behavior.
    if "explicit_tier_overrides" in inspect.signature(_build_child_agent).parameters:
        extra["explicit_tier_overrides"] = creds.get("explicit_tier_overrides")
    with patch("run_agent.AIAgent", _FakeChild):
        return _build_child_agent(
            task_index=0,
            goal="g",
            context=None,
            toolsets=None,
            model=creds["model"],
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
            override_provider=creds["provider"],
            override_base_url=creds["base_url"],
            override_api_key=creds["api_key"],
            override_api_mode=creds["api_mode"],
            override_request_overrides=creds.get("request_overrides"),
            **extra,
        )


@patch("tools.delegate_tool._load_config", return_value={})
def test_inherit_child_on_other_model_drops_ungated_tier(_cfg):
    child = _spawn(_ultrafast_parent(), {"model": "gpt-5.4-mini"})
    assert child.model == "gpt-5.4-mini"
    assert "service_tier" not in child.request_overrides
    assert "speed" not in child.request_overrides
    # Non-tier inherited overrides survive.
    assert child.request_overrides.get("extra_body") == {"k": 1}


@patch("tools.delegate_tool._load_config", return_value={})
def test_inherit_child_on_same_route_keeps_tier(_cfg):
    child = _spawn(_ultrafast_parent(), {})
    assert child.request_overrides.get("service_tier") == "ultrafast"


@patch("tools.delegate_tool._load_config", return_value={})
def test_explicit_delegation_tier_survives_regate(_cfg):
    child = _spawn(
        _ultrafast_parent(),
        {"model": "gpt-5.4-mini", "request_overrides": {"service_tier": "flex"}},
    )
    assert child.request_overrides.get("service_tier") == "flex"
