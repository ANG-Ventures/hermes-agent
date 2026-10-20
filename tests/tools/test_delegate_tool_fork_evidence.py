"""Prism f5ff1cc07dca (post-merge #2144): a fork child's execution evidence is its OWN.

The fork child runs over the parent's inherited transcript (``conversation_history``), and
``run_conversation`` returns that history inside ``result["messages"]``. Every consumer of
the returned rows (``tool_trace``, ``output_tail``, the ``subagent_stop`` hook) must see
only the rows the child generated, or the parent's old tool rounds are reported as work
the child did. Driven through the real ``delegate_task`` entry point with only the
provider call stubbed.
"""
from __future__ import annotations

import copy
import json
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from tools import delegate_tool
from tools import delegate_tool_fork as fork
from tools.delegate_tool import delegate_task

from tests.tools.test_delegate_tool_fork_mode import _anthropic_response, _patches, _warm_parent, _wire_agent

_PARENT_TOOL_OUTPUT = "PARENT-ONLY-OUTPUT kestrel 12 percent"


def _parent_with_tool_round():
    """A warm, fork-eligible parent whose completed turn includes a ``terminal`` tool round."""
    parent = _warm_parent()  # one real turn: caches the system prompt the fork must reuse
    parent.last_served_at = time.time() - 30
    parent._session_messages = [
        {"role": "user", "content": "Check the Kestrel canary."},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_parent_1", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": "cat canary.txt"})},
        }]},
        {"role": "tool", "tool_call_id": "toolu_parent_1", "content": _PARENT_TOOL_OUTPUT},
        {"role": "assistant", "content": "The Kestrel canary is at 12 percent."},
    ]
    return parent


def _delegate(parent, replies=("done: the canary is fine",), **kw):
    """Run delegate_task for real; child provider calls answer with ``replies`` in order
    (plain text, no tools; the last one repeats)."""
    requests: list = []
    hook_calls: list = []

    def _create(self, kwargs, **_kw):
        requests.append(copy.deepcopy(kwargs))
        return _anthropic_response(replies[min(len(requests), len(replies)) - 1])

    real_build = delegate_tool._build_child_agent

    def _build_and_wire(*a, **k):
        c = real_build(*a, **k)
        return _wire_agent(c, model=c.model) if getattr(c, "provider", None) == "claude-alr" else c

    def _hook(name, **kwargs):
        if name == "subagent_stop":
            hook_calls.append(kwargs)
        return []

    with ExitStack() as st:
        _patches(st)
        st.enter_context(patch.object(AIAgent, "_create_request_anthropic_client", lambda self, *a, **k: MagicMock()))
        st.enter_context(patch.object(AIAgent, "_anthropic_messages_create", _create))
        st.enter_context(patch("agent.turn_api_call._should_stream", return_value=False))
        st.enter_context(patch.object(AIAgent, "_persist_session", lambda self, *a, **k: None))
        st.enter_context(patch.object(AIAgent, "_save_trajectory", lambda self, *a, **k: None))
        st.enter_context(patch("tools.delegate_tool._load_config", return_value={}))
        st.enter_context(patch("tools.delegate_tool._build_child_agent", side_effect=_build_and_wire))
        st.enter_context(patch("hermes_cli.plugins.invoke_hook", side_effect=_hook))
        out = delegate_task(goal="verify the canary", parent_agent=parent, background=False, **kw)
    return json.loads(out), requests, hook_calls


def test_fork_child_tool_trace_excludes_inherited_parent_tool_rounds():
    parent = _parent_with_tool_round()
    result, requests, hook_calls = _delegate(parent, inherit_context=True, inherit_mode="fork")
    assert requests, "the child made no provider request"
    # The inherited transcript still reaches the model (INV-2 is untouched) ...
    assert _PARENT_TOOL_OUTPUT in json.dumps(requests[0]["messages"], default=str)
    entry = result["results"][0]
    assert entry["status"] == "completed", entry
    # ... but the child executed no tools, so its evidence carries none.
    assert entry["tool_trace"] == [], entry["tool_trace"]
    assert _PARENT_TOOL_OUTPUT not in json.dumps(entry, default=str)
    assert hook_calls, "subagent_stop never fired"
    assert hook_calls[-1]["tool_call_history"] == []


def test_child_generated_messages_positional_and_fallback():
    history = _parent_with_tool_round()._session_messages
    own = [{"role": "user", "content": "[BOOMERANG FORK ...]"},
           {"role": "assistant", "content": "", "tool_calls": [{"id": "toolu_child_1", "type": "function",
                                                             "function": {"name": "read_file", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "toolu_child_1", "content": "child output"},
           {"role": "assistant", "content": "done"}]
    # Intact prefix: positional slice.
    assert fork.child_generated_messages(copy.deepcopy(history) + own, history) == own
    # Rewritten prefix (compression collapsed the head into a summary row): the inherited
    # tool round is still recognized by its tool_call ids and dropped.
    rewritten = [{"role": "user", "content": "[summary of earlier turns]"}] + copy.deepcopy(history[1:]) + own
    got = fork.child_generated_messages(rewritten, history)
    assert all("toolu_parent_1" not in json.dumps(m) for m in got)
    assert own[1] in got and own[2] in got and own[3] in got
    # With the loop's exported boundary, the turn's rows are exactly its own.
    assert fork.child_generated_messages(rewritten, history, len(rewritten) - len(own)) == own
    # Non-fork turns are untouched (same object).
    msgs = list(own)
    assert fork.child_generated_messages(msgs, None) is msgs
    result = {"messages": msgs, "final_response": "done"}
    assert fork.strip_inherited_from_result(result, None) is result


def test_child_answer_equal_to_an_inherited_answer_is_kept():
    """Prism bec64a4205fe repro: compression dropped the prefix and the child's answer equals
    an old parent answer. Ownership is positional, so the child's answer survives."""
    msgs = [{"role": "user", "content": "summary"}, {"role": "user", "content": "frame"},
            {"role": "assistant", "content": "done"}]
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "done"}]
    assert {"role": "assistant", "content": "done"} in fork.child_generated_messages(msgs, history)
    assert fork.child_generated_messages(msgs, history, 1) == msgs[1:]
    # The real result envelope carries the boundary; strip_inherited_from_result uses it.
    out = fork.strip_inherited_from_result({"messages": msgs, "current_turn_user_idx": 1}, history)
    assert out["messages"] == msgs[1:]


def test_fork_schema_correction_turn_carries_no_inherited_tool_rounds():
    """Prism 1503421f1fa6: the schema-correction continuation's rows are trimmed too."""
    parent = _parent_with_tool_round()
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    result, requests, hook_calls = _delegate(
        parent, replies=("not json", '{"ok": true}'),
        inherit_context=True, inherit_mode="fork", output_schema=schema,
    )
    assert len(requests) >= 2, "the schema-correction turn never ran"
    entry = result["results"][0]
    assert entry.get("schema_valid") is True, entry
    assert entry["tool_trace"] == [], entry["tool_trace"]
    assert _PARENT_TOOL_OUTPUT not in json.dumps(entry, default=str)
    assert hook_calls and hook_calls[-1]["tool_call_history"] == []
