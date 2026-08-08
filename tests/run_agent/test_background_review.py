"""Regression tests for background review agent cleanup."""

from __future__ import annotations

import run_agent as run_agent_module
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.model = "fake-model"
    agent.platform = "telegram"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = "test-session"
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent._memory_store = object()
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._cached_system_prompt = "test-cached-system-prompt"
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.background_review_callback = None
    agent.status_callback = None
    agent._safe_print = lambda *_args, **_kwargs: None
    return agent


class ImmediateThread:
    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def test_background_review_shuts_down_memory_provider_before_close(monkeypatch):
    events = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            events.append(("init", kwargs))
            self._session_messages = []

        def run_conversation(self, **kwargs):
            events.append(("run_conversation", kwargs))

        def shutdown_memory_provider(self):
            events.append(("shutdown_memory_provider", None))

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert [name for name, _payload in events] == [
        "init",
        "run_conversation",
        "shutdown_memory_provider",
        "close",
    ]


def test_background_review_fork_opts_out_of_session_finalization(monkeypatch):
    """The review fork shares the parent's live session_id, so it must set
    ``_end_session_on_close = False``. Otherwise close() (now finalizing owned
    session rows) would end the still-active parent session mid-conversation
    every time the review fires (~every 10 turns). Regression for #12029.
    """
    seen = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []
            # Default matches AIAgent.__init__ (agent_init.py): owns its row.
            self._end_session_on_close = True

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "_end_session_on_close":
                seen["end_session_on_close"] = value

        def run_conversation(self, **kwargs):
            # By the time the fork runs, the opt-out must already be applied.
            seen["at_run_time"] = self._end_session_on_close

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert seen.get("end_session_on_close") is False
    assert seen.get("at_run_time") is False










def test_background_review_fork_keeps_skip_memory_with_mem0_write_flag_on(monkeypatch):
    """INV-1: enabling memory.background_review_mem0_write must NOT re-open the leak.

    The mem0-in-background-review feature adds a manager-free write tool
    (mem0_remember); it must NOT re-enable the fork's _memory_manager. The fork
    stays skip_memory=True even with the flag on — the ONLY mem0 write path is
    the manager-free mem0_remember registry tool, which never touches
    on_turn_start / prefetch_all / sync_all.
    """
    import hermes_cli.config as _config
    monkeypatch.setattr(_config, "load_config_readonly", lambda: {
        "memory": {"background_review_mem0_write": True}})

    captured_kwargs: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self._session_messages = []

        def run_conversation(self, **kwargs):
            pass

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()
    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert captured_kwargs.get("skip_memory") is True, (
        "Flag-on must NOT re-enable the fork's memory manager — the mem0 write "
        "is the manager-free mem0_remember tool, not a re-enabled _memory_manager."
    )



# ---------------------------------------------------------------------------
# memory_notifications mode: off | on | verbose
# ---------------------------------------------------------------------------

import json as _json

from agent.background_review import summarize_background_review_actions


def _memory_add_review():
    """A minimal review transcript: one memory add (assistant call + tool result)."""
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_mem1",
                    "function": {
                        "name": "memory",
                        "arguments": _json.dumps(
                            {
                                "action": "add",
                                "target": "memory",
                                "content": "User prefers terse replies",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_mem1",
            "content": _json.dumps(
                {"success": True, "message": "Entry added.", "target": "memory"}
            ),
        },
    ]


def _skill_patch_review():
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_skill1",
                    "function": {
                        "name": "skill_manage",
                        "arguments": _json.dumps(
                            {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_skill1",
            "content": _json.dumps(
                {
                    "success": True,
                    "message": "Patched SKILL.md in skill 'demo' (1 replacement).",
                    "_change": {"old": "a", "new": "b"},
                }
            ),
        },
    ]


def test_memory_notifications_off_returns_nothing():
    actions = summarize_background_review_actions(
        _memory_add_review(), [], notification_mode="off"
    )
    assert actions == []








def test_skill_patch_off_silent_verbose_shows_diff():
    assert (
        summarize_background_review_actions(
            _skill_patch_review(), [], notification_mode="off"
        )
        == []
    )
    verbose = summarize_background_review_actions(
        _skill_patch_review(), [], notification_mode="verbose"
    )
    assert len(verbose) == 1
    assert "demo" in verbose[0] and "→" in verbose[0]
