"""`/resume-handoff` is a real, registered, gateway-dispatchable command.

The turn-cut notice tells the user to send ``/resume-handoff``. A notice that
names a command the dispatcher does not know is worse than no notice, so this
pins the registration end-to-end: the registry entry, alias resolution, and
the handler's behaviour with and without a saved handoff.
"""

from __future__ import annotations

import types

import pytest

from agent.turn_handoff import capture_turn_handoff
from gateway.turn_handoff_command import render_resume_handoff_reply
from hermes_cli.commands import (
    GATEWAY_KNOWN_COMMANDS,
    is_gateway_known_command,
    resolve_command,
)


class _Store:
    def read(self):
        return [{"id": "1", "content": "finish the audit", "status": "pending"}]


def _agent(session_key="discord:chan-7"):
    a = types.SimpleNamespace()
    a._gateway_session_key = session_key
    a.session_id = "s1"
    a.provider = "claude-bpx-17"
    a.model = "claude-opus-5"
    a._todo_store = _Store()
    return a


def _messages():
    return [
        {"role": "user", "content": "audit the cron jobs", "row_id": 42},
        {"role": "assistant", "content": "Listing jobs.",
         "tool_calls": [{"id": "c1", "function": {
             "name": "terminal", "arguments": '{"command":"hermes cron list"}'}}]},
    ]


# ── registration ────────────────────────────────────────────────────────


class TestRegistration:
    def test_command_is_registered(self):
        assert resolve_command("resume-handoff") is not None

    def test_canonical_name_is_stable(self):
        assert resolve_command("resume-handoff").name == "resume-handoff"

    def test_it_is_gateway_dispatchable(self):
        assert is_gateway_known_command("resume-handoff")
        assert "resume-handoff" in GATEWAY_KNOWN_COMMANDS

    def test_the_underscore_alias_resolves_to_the_same_command(self):
        assert resolve_command("resume_handoff").name == "resume-handoff"

    def test_it_does_not_shadow_the_existing_resume_command(self):
        """/resume must still be the session-resume command."""
        assert resolve_command("resume").name == "resume"


# ── handler behaviour ───────────────────────────────────────────────────


class TestHandler:
    def test_replies_with_the_handoff_when_one_exists(self, tmp_path):
        capture_turn_handoff(_agent(), _messages(), turn_start_idx=0,
                             reason="the provider is rate-limiting",
                             root=tmp_path)
        reply = render_resume_handoff_reply(_agent(), root=tmp_path)
        assert "audit the cron jobs" in reply
        assert "terminal" in reply
        assert "finish the audit" in reply

    def test_says_so_plainly_when_there_is_nothing_to_resume(self, tmp_path):
        reply = render_resume_handoff_reply(_agent(), root=tmp_path)
        assert "no saved handoff" in reply.lower()

    def test_the_handoff_is_consumed(self, tmp_path):
        capture_turn_handoff(_agent(), _messages(), turn_start_idx=0,
                             reason="x", root=tmp_path)
        assert "audit" in render_resume_handoff_reply(_agent(), root=tmp_path)
        second = render_resume_handoff_reply(_agent(), root=tmp_path)
        assert "no saved handoff" in second.lower()

    def test_a_missing_session_key_is_handled(self, tmp_path):
        agent = _agent(session_key=None)
        assert "no saved handoff" in render_resume_handoff_reply(
            agent, root=tmp_path
        ).lower()

    def test_handler_never_raises(self, tmp_path):
        reply = render_resume_handoff_reply(types.SimpleNamespace(), root=tmp_path)
        assert isinstance(reply, str) and reply
