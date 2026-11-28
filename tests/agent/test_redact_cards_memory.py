"""Card masking on the memory paths (t_0171c33b): post-turn sync, turn-start recall, memory tool calls.

Every card number here is MADE UP (same fixtures as test_redact_cards.py): Luhn-valid, last fours 0366 / 2226.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from agent import redact_cards

VISA = "4532015112830366"
MC = "5555444433332226"


def _cfg(**security):
    return patch("hermes_cli.config.load_config_readonly", return_value={"security": security})


class _StubMemoryProvider:
    """Records what reaches a memory provider (sync_turn and queue_prefetch)."""

    def __init__(self):
        self.synced, self.prefetched = [], []

    @property
    def name(self):
        return "stub"

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        self.synced.append((user_content, assistant_content, messages))

    def queue_prefetch(self, query, *, session_id=""):
        self.prefetched.append(query)


def _memory_turn(user_text, reply, messages, **cfg):
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider
    from run_agent import AIAgent

    stub_cls = type("_Stub", (_StubMemoryProvider, MemoryProvider), {
        "initialize": lambda self, session_id="", **k: None, "is_available": lambda self: True,
        "system_prompt_block": lambda self: "", "prefetch": lambda self, q, *, session_id="": "",
        "get_tool_schemas": lambda self: [], "handle_tool_call": lambda self, *a, **k: ""})
    stub = stub_cls()
    manager = MemoryManager()
    manager.add_provider(stub)
    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager, agent.session_id = manager, "s1"
    with _cfg(**cfg):
        agent._sync_external_memory_for_turn(
            original_user_message=user_text, final_response=reply, interrupted=False, messages=messages)
    assert manager.flush_pending(timeout=10)
    manager.shutdown_all()
    return stub


def _card_turn():
    user = f"pay with {VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]} please, it is the work card"
    reply = f"Done: charged the card {MC} for the order."
    messages = [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
    return user, reply, messages


def test_memory_capture_masks_user_text_and_reply_by_default():
    assert redact_cards._KNOB_DEFAULTS["mask_card_numbers_memory_capture"] is True
    user, reply, messages = _card_turn()
    rows = json.dumps(messages)
    stub = _memory_turn(user, reply, messages)
    (got_user, got_reply, got_messages), = stub.synced
    assert got_user == "pay with XXXX XXXX XXXX 0366 please, it is the work card"
    assert got_reply == "Done: charged the card XXXXXXXXXXXX2226 for the order."
    assert stub.prefetched == [got_user]
    assert len(got_user) == len(user) and len(got_reply) == len(reply)
    # The transcript rows are not changed by this mask.
    assert json.dumps(messages) == rows
    assert got_messages == [{"role": "user", "content": got_user}, {"role": "assistant", "content": got_reply}]


def test_memory_capture_unmasked_when_switched_off():
    user, reply, messages = _card_turn()
    stub = _memory_turn(user, reply, messages, mask_card_numbers_memory_capture=False)
    assert stub.synced == [(user, reply, messages)]
    assert stub.prefetched == [user]


def test_memory_capture_turn_without_card_is_byte_identical():
    user = "what is on the calendar for 2026-11-28? order 1234 5678 ref 4000"
    reply = "Two meetings: 10:00 and 14:30. Ticket 12345678901234567 is closed."
    stub = _memory_turn(user, reply, None)
    (got_user, got_reply, _), = stub.synced
    assert got_user == user
    assert got_reply == reply


def _turn_start(user_text, **cfg):
    from agent.turn_context import _memory_turn_start_and_prefetch

    class _Mgr:
        def __init__(self):
            self.started, self.prefetched = [], []

        def on_turn_start(self, n, query, **k):
            self.started.append(query)

        def prefetch_all(self, query, *, session_id=""):
            self.prefetched.append(query)
            return ""

    agent = type("_A", (), {})()
    agent._memory_manager, agent._user_turn_count, agent.session_id = _Mgr(), 1, "s1"
    with _cfg(**cfg):
        _memory_turn_start_and_prefetch(agent, user_text)
    return agent._memory_manager


def test_turn_start_recall_query_masked_by_default():
    user, _, _ = _card_turn()
    mgr = _turn_start(user)
    masked = "pay with XXXX XXXX XXXX 0366 please, it is the work card"
    assert mgr.started == [masked] and mgr.prefetched == [masked]


def test_turn_start_recall_query_unmasked_when_switched_off():
    user, _, _ = _card_turn()
    mgr = _turn_start(user, mask_card_numbers_memory_capture=False)
    assert mgr.started == [user] and mgr.prefetched == [user]


# ── a fact the agent saves itself goes through MemoryManager.handle_tool_call ──

def _save_fact(fact, **cfg):
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider

    calls = []
    stub_cls = type("_ToolStub", (MemoryProvider,), {
        "name": property(lambda self: "stub"),
        "initialize": lambda self, session_id="", **k: None, "is_available": lambda self: True,
        "system_prompt_block": lambda self: "", "prefetch": lambda self, q, *, session_id="": "",
        "sync_turn": lambda self, *a, **k: None,
        "get_tool_schemas": lambda self: [{"name": "stub_conclude", "description": "save a fact",
                                           "parameters": {"type": "object", "properties": {}}}],
        "handle_tool_call": lambda self, name, args, **k: calls.append((name, dict(args)))
        or json.dumps({"result": "Fact stored."})})
    manager = MemoryManager()
    manager.add_provider(stub_cls())
    with _cfg(**cfg):
        result = manager.handle_tool_call("stub_conclude", {"conclusion": fact, "n": 3})
    manager.shutdown_all()
    return calls, result


def test_saved_fact_reaches_provider_masked_and_result_says_so():
    fact = f"Ace's work card is {VISA}, use it for Amazon"
    calls, result = _save_fact(fact)
    assert calls == [("stub_conclude", {"conclusion": "Ace's work card is XXXXXXXXXXXX0366, use it for Amazon", "n": 3})]
    data = json.loads(result)
    assert data["result"] == "Fact stored."
    assert "masked to its last four digits" in data["card_number_masked"]
    assert "anything this call saved holds the masked number" in data["card_number_masked"]


def test_saved_fact_unmasked_when_switched_off():
    fact = f"Ace's work card is {VISA}, use it for Amazon"
    calls, result = _save_fact(fact, mask_card_numbers_memory_capture=False)
    assert calls == [("stub_conclude", {"conclusion": fact, "n": 3})]
    assert json.loads(result) == {"result": "Fact stored."}


def test_saved_fact_without_card_is_untouched():
    fact = "Ace prefers order 112-1234567-1234567 delivered on Tuesdays"
    calls, result = _save_fact(fact)
    assert calls == [("stub_conclude", {"conclusion": fact, "n": 3})]
    assert json.loads(result) == {"result": "Fact stored."}


def _transcript_turn():
    user, reply, _ = _card_turn()
    call = {"id": "c1", "type": "function", "function": {"name": "pay", "arguments": json.dumps({"card": VISA})}}
    return user, reply, [
        {"role": "user", "content": [{"type": "text", "text": user}]},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": f"charged {MC}"},
        {"role": "assistant", "content": reply},
    ]


def test_memory_capture_masks_provider_copy_of_transcript():
    user, reply, messages = _transcript_turn()
    rows = json.dumps(messages)
    stub = _memory_turn(user, reply, messages)
    (_, got_reply, got_messages), = stub.synced
    # Every card in the copy the provider gets is masked (user parts, tool-call args, tool output, the reply) ...
    assert VISA not in json.dumps(got_messages) and MC not in json.dumps(got_messages)
    assert "0366" in json.dumps(got_messages) and "2226" in json.dumps(got_messages)
    # ... the reply still matches its own row, so a provider that locates the turn by the reply still finds it ...
    assert got_messages[-1]["content"] == got_reply
    # ... and the stored transcript is not changed.
    assert json.dumps(messages) == rows and messages[-1]["content"] == reply


def test_memory_capture_transcript_without_card_is_passed_through():
    messages = [{"role": "user", "content": "hello there"}, {"role": "assistant", "content": "hi, ticket 12345678901234567"}]
    stub = _memory_turn("hello there", "hi, ticket 12345678901234567", messages)
    (_, _, got_messages), = stub.synced
    assert got_messages is messages


def test_memory_tool_selector_reaches_provider_exactly():
    uri = f"viking://user/memories/{VISA}.md"
    calls, result = _save_fact(uri)
    assert calls == [("stub_conclude", {"conclusion": uri, "n": 3})]
    assert json.loads(result) == {"result": "Fact stored."}
