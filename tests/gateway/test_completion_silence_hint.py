"""Every agent-facing background-completion turn must tell the model how to stay silent.

Contract (Ace, 2026-09-27): the gateway drops a reply that is exactly the silence token
(gateway/response_filters.SILENT_REPLY_TOKEN); an injected completion turn that never
names that token makes agents answer their own completions with "already handled" posts.
"""
from gateway.response_filters import SILENT_REPLY_TOKEN, is_intentional_silence_response
from gateway.run import GatewayRunner
from tools.process_registry import COMPLETION_SILENCE_HINT, format_process_notification


def test_hint_names_the_token_the_gateway_actually_suppresses():
    assert SILENT_REPLY_TOKEN in COMPLETION_SILENCE_HINT
    assert is_intentional_silence_response(SILENT_REPLY_TOKEN)


def test_single_process_completion_carries_hint_for_every_status():
    for evt in (
        {"type": "completion", "session_id": "proc_a", "command": "true", "exit_code": 0, "output": ""},
        {"type": "completion", "session_id": "proc_b", "command": "false", "exit_code": 1, "output": "boom"},
        {"type": "completion", "session_id": "proc_c", "command": "x", "exit_code": -15,
         "completion_reason": "killed", "output": ""},
    ):
        text = format_process_notification(evt)
        assert COMPLETION_SILENCE_HINT in text and text.endswith("]")


def test_coalesced_process_batch_carries_hint_and_no_ambiguous_wording():
    entries = [("t", {"session_id": f"proc_{i}", "exit_code": 0, "output": "ok"}, None) for i in range(3)]
    text = GatewayRunner._format_coalesced_process_completions(entries)
    assert COMPLETION_SILENCE_HINT in text and "absorb it silently" not in text


def test_coalesced_delegation_batch_carries_hint():
    text = GatewayRunner._format_coalesced_async_delegations(["[A]", "[B]"])
    assert COMPLETION_SILENCE_HINT in text and "absorb it silently" not in text
