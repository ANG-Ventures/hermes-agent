"""Card 2 (gateway half) — ``/model`` must carry the session effort through.

``agent.agent_runtime_helpers.switch_model`` lives in ``agent/`` and cannot see
gateway session state, so a ``/reasoning high`` override (stored on
``SessionState.conversation.reasoning_override``, NOT in config.yaml) is
invisible to it. The gateway ``/model`` handlers therefore have to hand the
session-resolved effort in explicitly.

These tests exercise the REAL mixin methods against a real
``_resolve_session_reasoning_config``, plus a source-level (AST) contract that
BOTH ``/model`` handlers thread the kwarg — the sibling of
``test_a4_model_switch_announce``'s both-handlers gate, and the reason this bug
class keeps coming back when only one site is fixed.
"""

import ast

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


CFG = {
    "model": {"default": "claude-sonnet-4-6"},
    "agent": {
        "reasoning_effort": "medium",
        "reasoning_overrides": {"gpt-5.5": "low"},
    },
}


def _source():
    return SessionSource(
        platform=Platform.DISCORD, user_id="u1", chat_id="c1", user_name="ace",
    )


def _runner(monkeypatch):
    monkeypatch.setattr(
        gateway_run, "_load_gateway_runtime_config", lambda: CFG, raising=False,
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._sessions = {}
    return runner


def _pin_session_effort(runner, session_key, effort):
    """Set a real ``/reasoning <effort>`` session override."""
    runner._session_state(session_key).conversation.reasoning_override = {
        "enabled": True, "effort": effort,
    }


class TestSwitchReasoningKwargs:
    def test_session_override_is_threaded(self, monkeypatch):
        runner = _runner(monkeypatch)
        key = "agent:main:discord:c1:c1"
        _pin_session_effort(runner, key, "high")

        kw = runner._switch_reasoning_kwargs(
            source=_source(), session_key=key, model="claude-opus-5",
        )
        assert kw == {
            "session_reasoning_config": {"enabled": True, "effort": "high"}
        }, "the session's /reasoning override must reach switch_model"

    def test_session_override_beats_per_model_override(self, monkeypatch):
        """Precedence: session > per-model. Switching to a model that HAS a
        per-model override must still respect an explicit session pin."""
        runner = _runner(monkeypatch)
        key = "agent:main:discord:c1:c1"
        _pin_session_effort(runner, key, "high")

        kw = runner._switch_reasoning_kwargs(
            source=_source(), session_key=key, model="gpt-5.5",
        )
        assert kw["session_reasoning_config"] == {"enabled": True, "effort": "high"}

    def test_without_override_resolves_per_model_for_the_new_model(self, monkeypatch):
        """No session pin → the per-model override for the NEW model, not the
        old model's and not the bare global default."""
        runner = _runner(monkeypatch)
        kw = runner._switch_reasoning_kwargs(
            source=_source(), session_key="agent:main:discord:c1:c1", model="gpt-5.5",
        )
        assert kw["session_reasoning_config"] == {"enabled": True, "effort": "low"}

    def test_resolution_failure_degrades_to_empty_kwargs(self, monkeypatch):
        """A resolver blow-up must yield ``{}`` — switch_model then applies its
        own keep-current rule. It must NOT yield a global-default value."""
        runner = _runner(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("resolver down")

        monkeypatch.setattr(
            type(runner), "_resolve_session_reasoning_config", _boom, raising=True,
        )
        assert runner._switch_reasoning_kwargs(source=_source(), model="m") == {}


class TestPostSwitchReasoningConfig:
    def test_reads_the_live_agent_not_the_requesting_value(self, monkeypatch):
        """#467 rule 3 — the displayed effort comes from the agent's ACTUAL
        post-switch state, so a per-model override applied inside switch_model
        is what the user is told."""
        runner = _runner(monkeypatch)
        key = "agent:main:discord:c1:c1"
        _pin_session_effort(runner, key, "high")

        class _Agent:
            reasoning_config = {"enabled": True, "effort": "xhigh"}

        got = runner._post_switch_reasoning_config(
            _Agent(), source=_source(), session_key=key, model="claude-opus-5",
        )
        assert got == {"enabled": True, "effort": "xhigh"}, (
            "must read the agent's post-switch config, not the session pin"
        )

    def test_falls_back_to_session_resolver_without_an_agent(self, monkeypatch):
        """No cached agent was swapped (fresh session) — fall back to the
        session-aware resolution for the NEW model."""
        runner = _runner(monkeypatch)
        key = "agent:main:discord:c1:c1"
        _pin_session_effort(runner, key, "high")

        got = runner._post_switch_reasoning_config(
            None, source=_source(), session_key=key, model="claude-opus-5",
        )
        assert got == {"enabled": True, "effort": "high"}

    def test_agent_without_reasoning_config_falls_back(self, monkeypatch):
        runner = _runner(monkeypatch)
        key = "agent:main:discord:c1:c1"
        _pin_session_effort(runner, key, "high")

        class _Bare:
            pass

        got = runner._post_switch_reasoning_config(
            _Bare(), source=_source(), session_key=key, model="claude-opus-5",
        )
        assert got == {"enabled": True, "effort": "high"}


def _handler_nodes():
    import gateway.slash_commands as sc

    tree = ast.parse(open(sc.__file__, encoding="utf-8").read())
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("_on_model_selected", "_finish_switch"):
                found[node.name] = node
    return found


def _switch_model_calls(node):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "switch_model"
    ]


@pytest.mark.parametrize("handler", ["_on_model_selected", "_finish_switch"])
def test_both_model_handlers_thread_session_reasoning(handler):
    """Source contract: EVERY ``switch_model`` call in BOTH /model handlers
    must splat ``_switch_reasoning_kwargs``. Wiring only one site leaves the
    other silently demoting the session's effort — the exact half-fix shape
    this bug class keeps recurring as."""
    node = _handler_nodes().get(handler)
    assert node is not None, f"{handler} not found in gateway/slash_commands.py"

    calls = _switch_model_calls(node)
    assert calls, f"{handler} makes no switch_model call"

    for call in calls:
        splatted = [
            kw for kw in call.keywords
            if kw.arg is None
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Attribute)
            and kw.value.func.attr == "_switch_reasoning_kwargs"
        ]
        explicit = [kw for kw in call.keywords if kw.arg == "session_reasoning_config"]
        assert splatted or explicit, (
            f"{handler}: switch_model call at line {call.lineno} does not pass the "
            "session-resolved reasoning config — a /reasoning override will be "
            "demoted to the config default for the switching turn"
        )


@pytest.mark.parametrize("handler", ["_on_model_selected", "_finish_switch"])
def test_both_model_handlers_read_post_switch_effort(handler):
    """#467 rule 3 at the source level: each handler must consult
    ``_post_switch_reasoning_config`` (the agent's ACTUAL state) rather than
    computing its displayed effort solely from the pre-switch session value."""
    node = _handler_nodes().get(handler)
    assert node is not None

    names = {
        n.func.attr for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_post_switch_reasoning_config" in names, (
        f"{handler} computes its effort display without reading the agent's "
        "post-switch reasoning_config"
    )
