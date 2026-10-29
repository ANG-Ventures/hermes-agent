"""Route-note wire acceptance per provider adapter (spec 2026-10-29 model-change-notes v3, §10 Wire).

A request carrying route notes in every shape the loop produces (a persisted turn-start note
replayed through ``api_content``, a persisted mid-turn harness row typed ``display_kind=route_note``,
and the live note on this request's tail, prefixed or standalone after a tool result) goes through
the REAL transport ``build_kwargs`` of each family and must pass a schema-strict fake of that
provider's request schema. One ladder changes provider family on every rung.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import route_identity as ri
from agent.message_metadata import without_persistence_fields
from agent.transports import get_transport

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5"
REPLAYED_START = "[Route note: fell back to sonnet-5 (rate limit).]"
REPLAYED_MID = "[Route note: opus-5-5 restored.]"
TOOLS = [{"type": "function", "function": {"name": "web_search", "description": "s",
                                           "parameters": {"type": "object", "properties": {}}}}]


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "web_search", "arguments": "{}"}}


def _history():
    """The transcript as the loop holds it after two noted calls (persisted shapes)."""
    return [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "find it", "api_content": f"{REPLAYED_START}\n\nfind it"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "tool_name": "web_search",
         "content": "page: [quoted route note: fable restored]"},
        {"role": "user", "content": REPLAYED_MID, "display_kind": ri.ROUTE_NOTE_DISPLAY_KIND},
        {"role": "assistant", "content": "found it"},
    ]


def _api_messages(tail):
    """The loop's projection of the transcript into request rows (sidecar substituted, persistence
    fields such as ``display_kind`` dropped)."""
    from agent.turn_context import substitute_api_content
    out = []
    for m in _history() + tail:
        m = dict(without_persistence_fields(m))
        substitute_api_content(m)
        out.append(m)
    return out


TURN_START_TAIL = [{"role": "user", "content": "next"}]
MID_TURN_TAIL = [{"role": "user", "content": "next"},
                 {"role": "assistant", "content": "", "tool_calls": [_call("c2")]},
                 {"role": "tool", "tool_call_id": "c2", "name": "web_search", "tool_name": "web_search",
                  "content": "result two"}]


# --- schema-strict fakes -------------------------------------------------------------------------
_CHAT_KEYS = {"system": {"role", "content"}, "developer": {"role", "content"},
              "user": {"role", "content", "name"},
              "assistant": {"role", "content", "tool_calls", "name", "reasoning_content"},
              "tool": {"role", "content", "tool_call_id", "name"}}


def _text_of(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else "" for p in content or [])


def strict_openai_chat(kw):
    assert set(kw) >= {"model", "messages"}
    msgs, open_calls = kw["messages"], set()
    assert msgs[-1]["role"] in ("user", "tool")
    for prev, m in zip([None] + msgs[:-1], msgs):
        role = m["role"]
        assert set(m) <= _CHAT_KEYS[role], (role, set(m) - _CHAT_KEYS[role])
        c = m.get("content")
        assert c is None or isinstance(c, str) or all(
            isinstance(p, dict) and p.get("type") in ("text", "image_url") for p in c)
        if role == "assistant":
            open_calls = {tc["id"] for tc in m.get("tool_calls") or []}
            for tc in m.get("tool_calls") or []:
                assert set(tc) <= {"id", "type", "function"} and json.loads(tc["function"]["arguments"]) is not None
        elif role == "tool":
            assert prev["role"] in ("assistant", "tool") and m["tool_call_id"] in open_calls
        else:
            open_calls = set()
    return [_text_of(m.get("content")) for m in msgs]


_ANTHROPIC_BLOCKS = {"text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking", "document"}


def strict_anthropic(kw):
    msgs = kw["messages"]
    assert msgs[0]["role"] == "user" and msgs[-1]["role"] == "user"
    texts, prev_tool_uses = [], set()
    for i, m in enumerate(msgs):
        assert set(m) == {"role", "content"}, set(m)
        assert m["role"] == ("user" if i % 2 == 0 else "assistant"), "strict alternation"
        blocks = [{"type": "text", "text": m["content"]}] if isinstance(m["content"], str) else m["content"]
        assert blocks, "empty content"
        uses = set()
        for b in blocks:
            assert b["type"] in _ANTHROPIC_BLOCKS, b["type"]
            if b["type"] == "text":
                assert isinstance(b["text"], str) and b["text"], "empty text block"
                texts.append(b["text"])
            elif b["type"] == "tool_use":
                uses.add(b["id"])
            elif b["type"] == "tool_result":
                assert m["role"] == "user" and b["tool_use_id"] in prev_tool_uses
                texts.append(_text_of(b["content"]) if not isinstance(b["content"], str) else b["content"])
        prev_tool_uses = uses
    return texts


_RESPONSES_ITEMS = {"message": {"type", "role", "content", "id", "status"},
                    "function_call": {"type", "call_id", "name", "arguments", "id", "status"},
                    "function_call_output": {"type", "call_id", "output", "id", "status"},
                    "reasoning": {"type", "id", "summary", "encrypted_content", "content"}}


def strict_openai_responses(kw):
    items, calls, texts = kw["input"], set(), []
    assert isinstance(kw.get("instructions", ""), str)
    for it in items:
        kind = it.get("type", "message")
        assert kind in _RESPONSES_ITEMS and set(it) <= _RESPONSES_ITEMS[kind], (kind, set(it))
        if kind == "message":
            assert it["role"] in ("user", "assistant", "developer", "system")
            c = it["content"]
            if isinstance(c, str):
                texts.append(c)
            else:
                for p in c:
                    assert p["type"] in ("input_text", "output_text", "input_image"), p["type"]
                    texts.append(p.get("text", ""))
        elif kind == "function_call":
            calls.add(it["call_id"])
        elif kind == "function_call_output":
            assert it["call_id"] in calls
            texts.append(it["output"] if isinstance(it["output"], str) else json.dumps(it["output"]))
    return texts


def _anthropic(model, msgs):
    return strict_anthropic(get_transport("anthropic_messages").build_kwargs(model=model, messages=msgs, tools=TOOLS))


def _chat(model, msgs, base_url="https://api.openai.com/v1"):
    return strict_openai_chat(get_transport("chat_completions").build_kwargs(
        model=model, messages=msgs, tools=TOOLS, base_url=base_url))


def _responses(model, msgs):
    return strict_openai_responses(get_transport("codex_responses").build_kwargs(
        model=model, messages=msgs, tools=TOOLS, base_url="https://api.openai.com/v1"))


def _relay(model, msgs):  # an OpenAI-compatible relay (claude-bpx / cpa style) on loopback
    return _chat(model, msgs, base_url="http://127.0.0.1:8317/v1")


ADAPTERS = {"anthropic_messages": (_anthropic, SONNET), "openai_chat": (_chat, "gpt-6-astra"),
            "openai_responses": (_responses, "gpt-6-astra"), "openai_compatible_relay": (_relay, "kimi-k3")}


def _count(texts, needle):
    return sum(t.count(needle) for t in texts)


@pytest.mark.parametrize("tail", [TURN_START_TAIL, MID_TURN_TAIL], ids=["turn_start", "mid_turn"])
@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_request_carrying_route_notes_is_accepted_by_each_adapter(adapter, tail):
    build, model = ADAPTERS[adapter]
    live = "[Route note: fell back to sonnet-5 (provider overloaded).]"
    msgs = ri._with_note(_api_messages(tail), live)
    assert msgs is not None
    assert not any("display_kind" in m for m in msgs)  # persistence-only field never reaches a transport
    texts = build(model, msgs)
    for note in (REPLAYED_START, REPLAYED_MID, live):
        assert _count(texts, note) == 1, (adapter, note, texts)
    assert _count(texts, "[quoted route note: fable restored]") == 1
    assert _count(texts, "[Route note: fable") == 0


def _stub_agent():
    a = SimpleNamespace(model=OPUS, reasoning_config=None, request_overrides={}, service_tier=None,
                        session_id="s", _persist_disabled=False, _session_db=None, _fallback_activated=False,
                        _primary_runtime={"model": OPUS})
    ri.seed_known(a, None, None)
    return a


def test_ladder_whose_rungs_change_provider_family():
    """Anthropic fails, OpenAI Responses fails, the relay (chat) succeeds: each rung's payload is
    valid for ITS family and carries exactly one live note naming its own route; a failed rung's
    note never leaks into the next family's request; Known lands on the serving rung."""
    a = _stub_agent()
    hist = _history()
    s = SimpleNamespace(turn_id="t1", messages=[dict(m) for m in hist + MID_TURN_TAIL],
                        api_messages=_api_messages(MID_TURN_TAIL), api_request_id="t1:api:1",
                        current_turn_user_idx=len(hist))
    ri.commit_call(a, s, ri.prepare_call(a, s))  # the primary served earlier: Known = opus
    rungs = [("anthropic_messages", SONNET), ("openai_responses", "gpt-6-astra"), ("openai_compatible_relay", "kimi-k3")]
    sent = []
    for i, (adapter, model) in enumerate(rungs):
        a.model, a._fallback_activated = model, True
        call = ri.prepare_call(a, s)
        assert call is not None and call.note
        texts = ADAPTERS[adapter][0](model, s.api_messages)
        live = [n for n in (call.note,) if _count(texts, n) == 1]
        assert live == [call.note] and _count(texts, "fell back to") == 2  # replayed start + this rung
        sent.append(call.note)
        if i < len(rungs) - 1:
            ri.abandon_call(s, call)  # the rung failed
            assert not any(_text_of(m.get("content")).startswith(call.note) for m in s.api_messages)
        else:
            ri.commit_call(a, s, call)
    assert sent == [f"[Route note: fell back to {label} (unavailable).]" for label in ("sonnet-5", "gpt-6-astra", "kimi-k3")]
    assert a._route_known.serving.model == "kimi-k3"
    assert s.messages[-1] == {"role": "user", "content": sent[-1], "display_kind": ri.ROUTE_NOTE_DISPLAY_KIND}
