"""Iteration-limit summary request must not carry presentation fields.

``handle_max_iterations`` hand-builds its own ``api_messages`` and calls the
provider directly, so it does not go through the main loop's api_msg builder,
which pops ``display_kind`` / ``display_metadata`` (#764: "must never reach a
model"). Before this fix a DB-reloaded confab-notice row sent both keys (with
the bridge ``request_id`` and detector grammar label) on the summary request.
Strict Chat Completions gateways reject unknown message keys.

This drives the REAL path: turn 1 persists a confab-notice assistant row, turn
2 reloads history from SessionDB with ``max_iterations=1``, the provider asks
for a tool call, the budget runs out and the summary request fires. The
assertions read the captured wire bytes.
"""

from __future__ import annotations

import json

import pytest

from agent.confab_notice import CONFAB_NOTICE_DISPLAY_KIND, CONFAB_NOTICE_FIELD
from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from tests.agent.test_confab_notice_e2e import (  # noqa: F401 (fixture)
    VALID_NOTICE,
    _NoticeHandler,
    notice_env,
)

NOTICE = {**VALID_NOTICE, "request_id": "rid-764", "grammar": "QA-GRAMMAR-LABEL-764"}
LEAK_MARKERS = (
    "display_kind",
    "display_metadata",
    CONFAB_NOTICE_FIELD,
    "scaffold_confab_removed",
    "rid-764",
    "QA-GRAMMAR-LABEL-764",
)


def _is_summary_request(req: dict) -> bool:
    msgs = req.get("messages") or []
    return bool(msgs) and msgs[-1].get("content") == MAX_ITERATIONS_SUMMARY_REQUEST


def _send(handler, req: dict, message: dict, finish: str, notice=None) -> None:
    if req.get("stream") is True:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        delta = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, **tc} for i, tc in enumerate(message["tool_calls"])
            ]
        usage = {"id": "m", "choices": [],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
        if notice is not None:
            usage[CONFAB_NOTICE_FIELD] = notice
        for c in (
            {"id": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            usage,
        ):
            handler.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        return
    resp = {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", **message},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    if notice is not None:
        resp[CONFAB_NOTICE_FIELD] = notice
    body = json.dumps(resp).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _scripted_post(self):
    length = int(self.headers.get("Content-Length", 0))
    req = json.loads(self.rfile.read(length).decode())
    cls = type(self)
    cls.captured_requests.append(req)
    if "messages" not in req:
        return _send(self, req, {"content": "DONE"}, "stop")
    if _is_summary_request(req):
        return _send(self, req, {"content": "SUMMARY"}, "stop")
    step = cls.response_queue.pop(0) if cls.response_queue else "text"
    if step == "confab":
        return _send(self, req, {"content": "All good here."}, "stop", dict(NOTICE))
    if step == "tool_call":
        return _send(self, req, {"content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "no_such_tool", "arguments": "{}"},
        }]}, "tool_calls")
    return _send(self, req, {"content": "DONE"}, "stop")


@pytest.mark.parametrize("stream", [False, True], ids=["non_stream", "stream"])
def test_summary_request_strips_display_fields_from_reloaded_confab_row(
    notice_env, monkeypatch, stream  # noqa: F811
):
    make_agent, handler, db, sid, _statuses = notice_env
    monkeypatch.setattr(_NoticeHandler, "do_POST", _scripted_post)

    handler.response_queue.extend(["confab"])
    make_agent(stream=stream).run_conversation("hello", conversation_history=[], task_id="t1")

    history = db.get_messages_as_conversation(sid)
    assert any(m.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND for m in history), (
        "precondition: turn 1 must persist a confab-notice row"
    )

    handler.captured_requests = []
    handler.response_queue[:] = ["tool_call"]
    agent2 = make_agent(stream=stream)
    agent2.max_iterations = 1
    result = agent2.run_conversation("again", conversation_history=history, task_id="t2")

    reqs = [r for r in handler.captured_requests if "messages" in r]
    summary = [r for r in reqs if _is_summary_request(r)]
    assert summary, "iteration budget did not exhaust into the summary request"
    assert "SUMMARY" in (result.get("final_response") or "")
    # The replayed confab row really is in the summary request (so the
    # assertion below is about stripping, not about the row being absent).
    assert any(
        m.get("role") == "assistant" and m.get("content") == "All good here."
        for m in summary[0]["messages"]
    )
    for req in reqs:
        blob = json.dumps(req["messages"])
        for marker in LEAK_MARKERS:
            assert marker not in blob, (marker, "summary" if _is_summary_request(req) else "loop")


def test_hidden_legacy_placeholder_gets_neutral_payload_on_summary():
    """Mirror of the main loop's #88955 heal: once display_kind is stripped,
    an empty hidden assistant row with no sidecar must not go out empty."""
    import types
    from unittest.mock import patch

    from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER
    from agent.chat_completion_helpers import handle_max_iterations
    from run_agent import AIAgent

    agent = AIAgent(api_key="test-key", base_url="https://example.invalid/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._cached_system_prompt = "SYS"
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return "RAW"

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
    transport = types.SimpleNamespace(
        normalize_response=lambda _r: types.SimpleNamespace(content="SUMMARY"))
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "display_kind": "hidden",
         "display_metadata": {"k": "v"}},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a"},
    ]
    with patch.object(agent, "_ensure_primary_openai_client", return_value=client), \
            patch.object(agent, "_get_transport", return_value=transport):
        assert handle_max_iterations(agent, msgs, 5) == "SUMMARY"

    sent = calls[0]["messages"]
    assert all("display_kind" not in m and "display_metadata" not in m for m in sent)
    assert any(m.get("content") == _INTERRUPTED_PLACEHOLDER for m in sent)
    # The persisted transcript is untouched.
    assert msgs[1]["display_kind"] == "hidden" and msgs[1]["content"] == ""
