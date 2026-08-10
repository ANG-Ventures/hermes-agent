"""Verification-loop synthetic scaffolding must never reach durable session state.

verify_on_stop / pre_verify inject a synthetic user nudge to keep the agent
going one more turn before it can claim completion. The assistant response is
real content that persists and is emitted to the UI as an interim message.
Only the nudge (the synthetic user message) is flagged, so only the nudge
gets stripped from the durable transcript. This test file verifies:

  - The verification-loop flags remain registered in
    ``_EPHEMERAL_SCAFFOLDING_FLAGS`` (so nudges are stripped).
  - The DB flush drops only the nudge, keeping the assistant candidate.
  - The JSON log drops only the nudge, keeping the assistant candidate.
"""

import json
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest


_MISSING = object()


def _is_reload_target(module_name):
    return (
        module_name == "run_agent"
        or module_name.startswith("agent.")
        or module_name.startswith("tools.")
        or module_name.startswith("hermes_")
    )


@contextmanager
def _fresh_run_agent():
    saved_modules = {
        name: module
        for name, module in list(sys.modules.items())
        if _is_reload_target(name)
    }
    saved_parent_attrs = {}
    for name in saved_modules:
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None:
            saved_parent_attrs[(parent, child_name)] = parent.__dict__.get(
                child_name, _MISSING
            )

    for name in sorted(saved_modules, key=lambda value: value.count("."), reverse=True):
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None:
            parent.__dict__.pop(child_name, None)
        sys.modules.pop(name, None)

    try:
        import run_agent

        yield run_agent
    finally:
        current_modules = {
            name: module
            for name, module in list(sys.modules.items())
            if _is_reload_target(name)
        }
        for name, module in sorted(
            current_modules.items(),
            key=lambda item: item[0].count("."),
            reverse=True,
        ):
            parent_name, separator, child_name = name.rpartition(".")
            parent = sys.modules.get(parent_name) if separator else None
            if parent is not None and parent.__dict__.get(child_name) is module:
                parent.__dict__.pop(child_name, None)
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        for (parent, child_name), value in saved_parent_attrs.items():
            if value is _MISSING:
                parent.__dict__.pop(child_name, None)
            else:
                parent.__dict__[child_name] = value


@pytest.fixture
def fresh_run_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with _fresh_run_agent() as run_agent:
        yield run_agent


def test_fresh_run_agent_restores_precollected_module_identity(tmp_path):
    """Fresh imports must not replace modules held by later test files."""
    import agent
    import agent.transports.codex as original_codex

    with pytest.MonkeyPatch.context() as scoped_patch:
        scoped_patch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _fresh_run_agent():
            assert sys.modules.get("agent.transports.codex") is not original_codex

    assert sys.modules["agent.transports.codex"] is original_codex
    assert agent.__dict__["transports"].__dict__["codex"] is original_codex


def test_verification_flags_registered_as_ephemeral(fresh_run_agent):
    ra = fresh_run_agent

    assert "_verification_stop_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
    assert "_pre_verify_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS

    # The nudge messages ARE scaffolding (they carry the synthetic flag).
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True}
    )
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True}
    )
    # Real messages (including the assistant candidate) are not.
    assert not ra._is_ephemeral_scaffolding({"role": "user", "content": "hi"})
    assert not ra._is_ephemeral_scaffolding({"role": "assistant", "content": "premature done"})


def _make_agent(ra, session_id, tmp_path):
    agent = ra.AIAgent(
        session_id=session_id,
        api_key="test-key",
        base_url="http://127.0.0.1:8000/v1",
        provider="openai-compat",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "logs"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    return agent


def test_db_flush_drops_only_nudge_keeps_candidate(tmp_path, fresh_run_agent):
    """The assistant candidate is NOT flagged synthetic, so it persists.
    Only the nudge (flagged synthetic) is dropped from the DB flush."""
    ra = fresh_run_agent
    agent = _make_agent(ra, "sess_db", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        # Assistant candidate — NOT flagged synthetic, persists.
        {"role": "assistant", "content": "premature done"},
        # Nudge — flagged synthetic, gets dropped.
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._flush_messages_to_session_db(messages, conversation_history=[])

    persisted = [
        msg.get("content")
        for _args, kwargs in agent._session_db.append_messages_batch.call_args_list
        for msg in kwargs["messages"]
    ]
    assert "hi" in persisted
    assert "verified and clean" in persisted
    # The assistant candidate persists — it is real content.
    assert "premature done" in persisted
    # Only the nudge is dropped.
    assert "[System: run tests]" not in persisted


def test_json_log_drops_only_nudge_keeps_candidate(tmp_path, fresh_run_agent):
    """The assistant candidate is NOT flagged synthetic, so it persists in the
    JSON log. Only the nudge (flagged synthetic) is dropped."""
    ra = fresh_run_agent
    agent = _make_agent(ra, "sess_json", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        # Assistant candidate — NOT flagged synthetic, persists.
        {"role": "assistant", "content": "premature done"},
        # Nudge — flagged synthetic, gets dropped.
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._save_session_log(messages)

    log_file = agent.logs_dir / "session_sess_json.json"
    assert log_file.exists()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    contents = [m.get("content") for m in data["messages"]]
    # The assistant candidate persists — it is real content.
    assert "premature done" in contents
    assert "verified and clean" in contents
    assert "hi" in contents
    # Only the nudge is dropped.
    assert "[System: run tests]" not in contents
    assert all(not m.get("_pre_verify_synthetic") for m in data["messages"])
