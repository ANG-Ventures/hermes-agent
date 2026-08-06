"""Regression tests for oneshot chat identity forwarding."""

import sys
import types

import hermes_cli.oneshot as oneshot_mod


def _capture_agent_kwargs(monkeypatch, *, task=None, board=None):
    captured = {}

    if task is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    if board is None:
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()
            self._session_messages = []

        def run_conversation(self, _prompt):
            return {"final_response": "done"}

        def shutdown_memory_provider(self, messages=None):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-test", "provider": "openai"}},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "key",
            "base_url": "https://example.invalid",
            "provider": "openai",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(oneshot_mod, "_create_session_db_for_oneshot", lambda: None)

    assert oneshot_mod._run_agent(
        "hello", model="gpt-test", provider="openai", use_config_toolsets=False
    ) == ("done", {"final_response": "done"})
    return captured


def test_oneshot_forwards_kanban_chat_identity(monkeypatch):
    kwargs = _capture_agent_kwargs(monkeypatch, task=" t_3e694374 ", board=" wave3 ")

    assert kwargs["chat_id"] == "t_3e694374"
    assert kwargs["chat_name"] == "kanban / wave3 / t_3e694374"


def test_oneshot_keeps_plain_cli_chat_identity_empty(monkeypatch):
    kwargs = _capture_agent_kwargs(monkeypatch)

    assert kwargs["chat_id"] == ""
    assert kwargs["chat_name"] == ""
