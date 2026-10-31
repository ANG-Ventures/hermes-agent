"""Regression tests for oneshot chat identity forwarding."""

import sys
import types

import hermes_cli.oneshot as oneshot_mod


def _capture_agent_kwargs(monkeypatch, *, task=None, db=None):
    captured = {}

    if task is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    if db is None:
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_DB", db)

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()
            self._session_messages = []

        def run_conversation(self, _prompt, conversation_history=None):
            # upstream _run_agent forwards conversation_history (None for a fresh one-shot)
            assert conversation_history is None
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


def test_oneshot_forwards_kanban_chat_identity(monkeypatch, tmp_path):
    """S7: the board in the chat name is derived from the worker's DB pin, not a slug env var."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "not-this-one")
    kb.create_board("wave3")
    db = str(kb.board_dir("wave3") / "kanban.db")
    kwargs = _capture_agent_kwargs(monkeypatch, task=" t_3e694374 ", db=db)

    assert kwargs["chat_id"] == "t_3e694374"
    assert kwargs["chat_name"] == "kanban / wave3 / t_3e694374"


def test_oneshot_keeps_plain_cli_chat_identity_empty(monkeypatch):
    kwargs = _capture_agent_kwargs(monkeypatch)

    assert kwargs["chat_id"] == ""
    assert kwargs["chat_name"] == ""
