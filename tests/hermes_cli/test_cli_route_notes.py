"""CLI ``/model`` carries the switch in the route note alone (spec 2026-10-29 model-change-notes v3 §4, §9).

Regression for t_53ad66c4: the CLI still prepended its legacy ``[Note: model was just switched …]``
next to the route note, so with the selected model unavailable the served request told the model
both "you are running on opus" and "switched to fable". These tests drive the real ``HermesCLI.chat``
turn path (agent rebuild included) over a real ``AIAgent`` loop with a fake provider ladder, and
assert on what the SERVED request carried.
"""

from __future__ import annotations

import importlib
import queue
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tests.agent.test_route_notes as T
from agent import route_identity as ri

LEGACY = "[Note: model was just switched"


def _make_cli():
    stubs = {name: MagicMock() for name in (
        "prompt_toolkit", "prompt_toolkit.history", "prompt_toolkit.styles", "prompt_toolkit.patch_stdout",
        "prompt_toolkit.application", "prompt_toolkit.layout", "prompt_toolkit.layout.processors",
        "prompt_toolkit.filters", "prompt_toolkit.layout.dimension", "prompt_toolkit.layout.menus",
        "prompt_toolkit.widgets", "prompt_toolkit.key_binding", "prompt_toolkit.completion",
        "prompt_toolkit.formatted_text", "prompt_toolkit.auto_suggest")}
    config = {"model": {"default": T.OPUS, "base_url": "https://example.invalid/v1", "provider": "custom"},
              "display": {"compact": False, "tool_progress": "all"}, "agent": {}, "terminal": {"env_type": "local"}}
    with patch.dict(sys.modules, stubs), patch.dict("os.environ", {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}):
        import cli as _cli_mod
        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), \
                patch.dict(_cli_mod.__dict__, {"CLI_CONFIG": config}):
            cli = _cli_mod.HermesCLI()
    cli.model, cli.provider, cli.requested_provider = T.OPUS, "custom", "custom"
    cli.base_url, cli.api_key, cli.api_mode = "https://example.invalid/v1", "k-abcdef12", None
    cli._interrupt_queue = queue.Queue()
    cli._pending_input = queue.Queue()
    return cli


class CliLadder:
    """Scripted outcome per model the CLI's CURRENT agent sends to (the agent may be rebuilt)."""

    def __init__(self, cli):
        self.cli = cli
        self.script = {}
        self.calls = []

    def __call__(self, api_kwargs):
        model = self.cli.agent.model
        seq = self.script.get(model, [200])
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        self.calls.append((model, outcome, api_kwargs))
        if outcome == 503:
            raise T.Overloaded()
        return T._response(f"ok from {model}")

    def served_tail(self):
        model, _, kw = [c for c in self.calls if c[1] == 200][-1]
        content = kw["messages"][-1]["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return model, str(content)


def _init_agent(cli):
    from hermes_cli.cli_agent_setup_mixin import _current_runtime, _route_signature

    def build(**_kw):
        if cli.agent is None:
            cli.agent = T._make_agent(model=cli.model, fallbacks=(T.OPUS,), session_id=cli.session_id)
            cli._active_agent_route_signature = _route_signature(cli.model, _current_runtime(cli))
        return True
    return build


def _turn(cli, ladder, text):
    from agent import fallback_policy
    from run_agent import AIAgent
    fb_client = MagicMock(api_key="k-abcdef12", base_url="https://example.invalid/v1",
                          _custom_headers=None, default_headers=None)
    with ExitStack() as st:
        for p in (
            patch.object(cli, "_ensure_runtime_credentials", return_value=True),
            patch.object(cli, "_init_agent", side_effect=_init_agent(cli)),
            patch.object(cli, "_sync_fallback_chain_with_config"),
            patch.object(AIAgent, "_interruptible_api_call", side_effect=ladder),
            patch.object(AIAgent, "_save_trajectory"),
            patch.object(AIAgent, "_cleanup_task_resources"),
            patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch("agent.auxiliary_client.resolve_provider_client",
                  side_effect=lambda provider, model=None, **k: (fb_client, model)),
            patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
            patch("agent.fallback_wiring.restore_allowed",
                  side_effect=lambda *a, **k: fallback_policy.Decision(True, None, "test")),
        ):
            st.enter_context(p)
        cli.chat(text)
    if cli.agent is not None:
        cli.agent._rate_limited_until = 0


def _slash_model(cli, new_model):
    """The typed ``/model <name>`` commit (stage, swap the live agent, print, record)."""
    from hermes_cli.cli_model_switch_mixin import _commit_model_switch
    result = SimpleNamespace(
        new_model=new_model, target_provider="custom", provider_label="custom", api_key="k-abcdef12",
        base_url="https://example.invalid/v1", api_mode=None, runtime_capabilities=None, model_info=None,
        warning_message=None)
    with patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()), \
            patch("hermes_cli.model_switch.resolve_display_context_length", return_value=None), \
            patch.object(type(cli), "_persist_model_switch_to_session"):
        _commit_model_switch(cli, result, persist_global=False)


def _route_notes(text):
    return [part for part in text.split("\n\n") if part.startswith(ri.ROUTE_NOTE_OPENER)]


@pytest.mark.parametrize("enabled", [True, False])
def test_cli_model_switch_to_unavailable_model_sends_one_combined_note(enabled):
    cli = _make_cli()
    ladder = CliLadder(cli)
    with patch.object(ri, "route_notes_enabled", return_value=enabled):
        _turn(cli, ladder, "turn one")
        _slash_model(cli, T.FABLE)
        ladder.script = {T.FABLE: [503]}
        ladder.calls.clear()
        _turn(cli, ladder, "are you fable?")
    served, tail = ladder.served_tail()
    assert served == T.OPUS
    assert LEGACY not in tail
    if enabled:
        assert _route_notes(tail) == ["[Route note: the user selected fable-5-1; it is unavailable "
                                      "(provider overloaded): you are running on opus-5-5.]"]
    else:
        assert _route_notes(tail) == [] and tail.endswith("are you fable?")


@pytest.mark.parametrize("enabled", [True, False])
def test_cli_successful_model_switch_sends_a_single_note(enabled):
    cli = _make_cli()
    ladder = CliLadder(cli)
    with patch.object(ri, "route_notes_enabled", return_value=enabled):
        _turn(cli, ladder, "turn one")
        _slash_model(cli, T.SONNET)
        ladder.calls.clear()
        _turn(cli, ladder, "who are you?")
    served, tail = ladder.served_tail()
    assert served == T.SONNET
    assert LEGACY not in tail
    assert _route_notes(tail) == (["[Route note: the user switched to sonnet-5.]"] if enabled else [])
