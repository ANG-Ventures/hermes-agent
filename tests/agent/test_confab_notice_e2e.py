"""End-to-end consumer tests for the out-of-band confab notice.

These drive the real agent loop against an in-process mock provider that
returns a v1 ``hermes_confab_notice`` on the response envelope, in BOTH the
streaming (final usage chunk) and non-streaming (top-level completion) shapes
the spec defines.

The load-bearing assertions:

* the user is told, once, on the current turn (``_emit_status`` → CLI + gateway);
* the assistant row in ``state.db`` carries ``display_kind`` +
  ``display_metadata`` — the durable triage record;
* the NEXT provider request body contains neither presentation field nor any
  notice text anywhere in ``messages[]`` (asserted on the captured wire bytes,
  not on internal state);
* with today's bridge — in-band marker, no extension field — nothing changes.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.confab_notice import (
    CONFAB_NOTICE_DISPLAY_KIND,
    CONFAB_NOTICE_FIELD,
    CONFAB_NOTICE_KEY,
    is_metadata_only_tool_notice,
)
from hermes_state import SessionDB

VALID_NOTICE = {
    "version": 1,
    "kind": "scaffold_confab_removed",
    "request_id": "3b264082",
    "scope": "visible",
    "grammar": "inbound",
}


class _NoticeHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        # The model context-length probe also POSTs here; only a real
        # chat-completions payload may consume a queued scripted response.
        if "messages" in req and type(self).response_queue:
            scripted = type(self).response_queue.pop(0)
            text, notice = scripted[:2]
            finish = scripted[2] if len(scripted) > 2 else "stop"
        else:
            text, notice = "DONE", None
            finish = "stop"

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            # Per the contract the extension rides the FINAL usage chunk
            # (choices: []), never a content delta.
            usage_chunk = {
                "id": "m",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            if notice is not None:
                usage_chunk[CONFAB_NOTICE_FIELD] = notice
            chunks.append(usage_chunk)
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            resp = {
                "id": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": finish,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            if notice is not None:
                resp[CONFAB_NOTICE_FIELD] = notice
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def notice_env(monkeypatch):
    """Mock provider + isolated HERMES_HOME + shared SessionDB.

    Yields ``(make_agent, handler, db, sid)``; ``make_agent(stream=...)``
    builds a fresh AIAgent bound to the shared store so a second call models
    turn N+1 replaying history.
    """
    _NoticeHandler.captured_requests = []
    _NoticeHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _NoticeHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    test_home = tempfile.mkdtemp(prefix="hermes_confab_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    from run_agent import AIAgent

    monkeypatch.setattr("agent.title_generator._auto_title_enabled", lambda: False)

    db = SessionDB(db_path=Path(test_home) / "state.db")
    sid = "sess-confab"
    statuses: list = []

    def make_agent(stream: bool = False):
        agent = AIAgent(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat",
            model="test-model",
            max_iterations=10,
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            platform="cli",
            session_db=db,
            session_id=sid,
        )
        agent.valid_tool_names = set()
        agent._disable_streaming = not stream
        # Gateway leg of _emit_status — what Discord/Telegram receive.
        agent.status_callback = lambda kind, message: statuses.append((kind, message))
        return agent

    try:
        yield make_agent, _NoticeHandler, db, sid, statuses
    finally:
        srv.shutdown()
        db.close()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _chat_requests(handler) -> list:
    return [r for r in handler.captured_requests if "messages" in r]


def _lifecycle_texts(statuses) -> list:
    return [m for kind, m in statuses if kind == "lifecycle"]


def _confab_statuses(statuses) -> list:
    return [m for m in _lifecycle_texts(statuses) if "onfabulation" in m]


@pytest.mark.parametrize("stream", [False, True], ids=["non_stream", "stream"])
class TestConfabNoticeEndToEnd:
    @pytest.mark.parametrize("metadata", [
        {CONFAB_NOTICE_KEY: {**VALID_NOTICE, "kind": "tool_call_as_text"}},
        {CONFAB_NOTICE_KEY: {**VALID_NOTICE, "kind": "tool_call_as_text", "version": 99}},
    ])
    def test_tagged_system_content_is_not_lost_on_replay(self, notice_env, stream, metadata):
        make_agent, handler, db, sid, statuses = notice_env
        sentinel = "QA-REPLAY-SYSTEM-CONTENT-ALLOWED"
        history = [{
            "role": "system", "content": sentinel,
            "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
            "display_metadata": metadata,
        }]
        handler.response_queue.append(("Done.", None))
        make_agent(stream=stream).run_conversation("hello", conversation_history=history, task_id="t1")
        assert sentinel in json.dumps(_chat_requests(handler)[0]["messages"])

    def test_valid_empty_tool_event_is_not_replayed(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        history = [{
            "role": "system", "content": "", "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
            "display_metadata": {CONFAB_NOTICE_KEY: {**VALID_NOTICE, "kind": "tool_call_as_text"}},
        }]
        handler.response_queue.append(("Done.", None))
        make_agent(stream=stream).run_conversation("hello", conversation_history=history, task_id="t1")
        assert not any(m.get("role") == "system" and m.get("content") == ""
                       for m in _chat_requests(handler)[0]["messages"])

    def test_db_reloaded_tool_event_is_not_sent_in_iteration_summary(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        notice = {**VALID_NOTICE, "kind": "tool_call_as_text", "request_id": "summary-event"}
        handler.response_queue.extend([("", notice), ("One.", None)])
        assert make_agent(stream=stream).run_conversation(
            "hello", conversation_history=[], task_id="t1"
        )["final_response"] == "One."

        history = db.get_messages_as_conversation(sid)
        assert len([m for m in history if m.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND]) == 1
        handler.captured_requests = []
        handler.response_queue.extend([("Trying.", None, "tool_calls"), ("Summary.", None)])
        agent = make_agent(stream=stream)
        agent.max_iterations = 1
        assert agent.run_conversation(
            "again", conversation_history=history, task_id="t2"
        )["final_response"] == "Summary."

        requests = _chat_requests(handler)
        assert len(requests) == 2
        summary_messages = requests[-1]["messages"]
        assert sum(m.get("role") == "system" for m in summary_messages) == 1
        assert not any(m.get("role") == "system" and not m.get("content") for m in summary_messages)
        assert all("display_kind" not in m and "display_metadata" not in m for m in summary_messages)
        assert notice["request_id"] not in json.dumps(summary_messages)

    def test_contentful_tagged_system_reaches_iteration_summary(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        sentinel = "CONTENTFUL-TAGGED-SYSTEM-SUMMARY"
        history = [{"role": "system", "content": sentinel,
                    "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
                    "display_metadata": {CONFAB_NOTICE_KEY: {**VALID_NOTICE, "kind": "tool_call_as_text"}}}]
        handler.response_queue.extend([("Trying.", None, "tool_calls"), ("Summary.", None)])
        agent = make_agent(stream=stream)
        agent.max_iterations = 1
        agent.run_conversation("again", conversation_history=history, task_id="summary-content")
        assert sentinel in json.dumps(_chat_requests(handler)[-1]["messages"])

    @pytest.mark.parametrize("engine", ["builtin", "lcm"])
    @pytest.mark.parametrize("index", [0, 1])
    def test_compaction_keeps_tool_event_ephemeral(self, notice_env, stream, engine, index, tmp_path):
        from agent.transports.anthropic import AnthropicTransport

        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.extend([
            ("", {**VALID_NOTICE, "kind": "tool_call_as_text"}), ("First.", None)
        ])
        assert make_agent(stream=stream).run_conversation(
            "first", conversation_history=[], task_id="writer"
        )["final_response"] == "First."
        event = next(m for m in db.get_messages_as_conversation(sid)
                     if is_metadata_only_tool_notice(m))
        handler.captured_requests = []
        turns = [{"role": "user" if i % 2 == 0 else "assistant",
                  "content": f"turn {i} " + " ".join(f"w{i}x{j}" for j in range(900))}
                 for i in range(40)]
        history = turns.copy()
        history.insert(index, event)
        agent = make_agent(stream=stream)
        agent.ephemeral_system_prompt = "REAL-PROMPT-ANCHOR"
        agent.compression_enabled = True
        if engine == "lcm":
            from plugins.context_engine.lcm.config import LCMConfig
            from plugins.context_engine.lcm.engine import LCMEngine
            config = LCMConfig(database_path=str(tmp_path / "lcm.db"), fresh_tail_count=1,
                               leaf_chunk_tokens=1, context_threshold=0.01)
            cc = LCMEngine(config=config, hermes_home=str(tmp_path))
            cc.update_model("test-model", 200_000, provider="unit-test")
            cc.on_session_start(sid, hermes_home=str(tmp_path), model="test-model",
                                provider="unit-test", context_length=200_000, platform="pytest")
            agent.context_compressor = cc
        else:
            cc = agent.context_compressor
            cc.threshold_tokens = 2000
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "compacted summary"
        resp.usage = None
        handler.response_queue.append(("Done.", None))
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = agent.run_conversation("next", conversation_history=history, task_id="compact")
        assert len(result["messages"]) < len(history), "compaction did not shorten history"
        wire = _chat_requests(handler)[-1]["messages"]
        assert sum(m.get("role") == "system" for m in wire) == 1
        assert "REAL-PROMPT-ANCHOR" in json.dumps(AnthropicTransport().build_kwargs(
            model="claude-opus-4-6", messages=wire, tools=None, max_tokens=1024,
            reasoning_config=None).get("system"))
        assert any(is_metadata_only_tool_notice(m) for m in result["messages"])
        persisted = [r for session in {sid, agent.session_id}
                     for r in db.get_messages(session) if r.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND]
        assert persisted and all(is_metadata_only_tool_notice(r) for r in persisted)

    @pytest.mark.parametrize("engine", ["builtin", "lcm"])
    @pytest.mark.parametrize("event_position", [3, 38, 47])
    @pytest.mark.parametrize("fallback", [False, True])
    def test_compaction_event_does_not_split_parallel_tool_results(self, notice_env, stream, engine, event_position, fallback, tmp_path):
        make_agent, handler, db, sid, _ = notice_env
        handler.response_queue.extend([
            ("", {**VALID_NOTICE, "kind": "tool_call_as_text"}), ("First.", None)
        ])
        make_agent(stream=stream).run_conversation("first", conversation_history=[], task_id="writer")
        event = next(m for m in db.get_messages_as_conversation(sid)
                     if is_metadata_only_tool_notice(m))
        big = " ".join(f"w{j}" for j in range(700))
        history = [{"role": "user", "content": f"lead {big}"},
                   {"role": "assistant", "content": f"lead reply {big}"}]
        for i in range(8):
            calls = [{"id": f"call_{i}_{k}", "type": "function",
                      "function": {"name": "read_file", "arguments": "{}"}} for k in range(2)]
            history.extend([
                {"role": "user", "content": f"u{i} {big}"},
                {"role": "assistant", "content": "", "tool_calls": calls},
                *({"role": "tool", "tool_call_id": call["id"],
                   "content": f"result {call['id']} {big}"} for call in calls),
                {"role": "assistant", "content": "Done."},
            ])
        for i in range(4):
            history.extend([{"role": "user", "content": f"short question {i}"},
                            {"role": "assistant", "content": "Done."}])

        def run(arm):
            arm_sid = f"tool-group-{engine}-{arm}-{stream}"
            rows = json.loads(json.dumps(history))
            if arm == "event":
                rows.insert(event_position, dict(event))  # writer's user -> event -> assistant position
            agent = make_agent(stream=stream)
            agent.session_id = arm_sid
            agent.compression_enabled = True
            if engine == "lcm":
                from plugins.context_engine.lcm.config import LCMConfig
                from plugins.context_engine.lcm.engine import LCMEngine
                cc = LCMEngine(config=LCMConfig(
                    database_path=str(tmp_path / f"{arm}.db"), fresh_tail_count=8,
                    leaf_chunk_tokens=1, context_threshold=0.01), hermes_home=str(tmp_path))
                cc.update_model("test-model", 200_000, provider="unit-test")
                cc.on_session_start(arm_sid, hermes_home=str(tmp_path), model="test-model",
                                    provider="unit-test", context_length=200_000, platform="pytest")
                agent.context_compressor = cc
            else:
                agent.context_compressor.threshold_tokens = 2000
                agent.context_compressor._generate_summary = lambda *a, **kw: "compacted summary"
            calls = []
            if fallback:
                compress = agent.context_compressor.compress
                def strict_engine(input_rows, **kwargs):
                    calls.append((input_rows, kwargs))
                    if "force" in kwargs:
                        raise TypeError("legacy engine does not accept force")
                    return compress(input_rows, **kwargs)
                agent.context_compressor.compress = strict_engine
            response = MagicMock()
            response.choices = [MagicMock()]
            response.choices[0].message.content = "compacted summary"
            response.usage = None
            handler.captured_requests = []
            handler.response_queue[:] = [("Done.", None)]
            with patch("agent.auxiliary_client.call_llm", return_value=response):
                result = agent.run_conversation("next question", conversation_history=rows,
                                                task_id=f"compact-{arm}")
            assert agent.context_compressor.compression_count > 0, "compression did not fire"
            if fallback:
                assert any("force" in kwargs for _, kwargs in calls)
                assert any(set(kwargs) == {"current_tokens"} for _, kwargs in calls)
                assert all(not any(is_metadata_only_tool_notice(m) for m in input_rows)
                           for input_rows, _ in calls)
            wire1 = _chat_requests(handler)[-1]["messages"]
            stored = db.get_messages_as_conversation(agent.session_id)
            handler.captured_requests = []
            handler.response_queue[:] = [("Second.", None)]
            resumed = make_agent(stream=stream)
            resumed.session_id = agent.session_id
            resumed.compression_enabled = False
            resumed.run_conversation("follow up", conversation_history=stored,
                                     task_id=f"resume-{arm}")
            wire2 = _chat_requests(handler)[-1]["messages"]
            return wire1, wire2, stored

        control1, control2, control_rows = run("control")
        event1, event2, event_rows = run("event")
        def ids(rows):
            return ([tc["id"] for m in rows for tc in m.get("tool_calls", [])],
                    [m["tool_call_id"] for m in rows if m.get("role") == "tool"])
        assert ids(event1) == ids(control1)
        assert ids(event2) == ids(control2)
        assert ids([m for m in event_rows if not is_metadata_only_tool_notice(m)]) == ids(control_rows)
        def transcript(rows):
            return [(m.get("role"), m.get("content"), m.get("tool_calls"), m.get("tool_call_id"))
                    for m in rows if not is_metadata_only_tool_notice(m)]
        assert transcript(event_rows) == transcript(control_rows)
        assert transcript(event1) == transcript(control1)
        assert transcript(event2) == transcript(control2)
        assert sum(is_metadata_only_tool_notice(m) for m in event_rows) == 1
        event_idx = next(i for i, m in enumerate(event_rows) if is_metadata_only_tool_notice(m))
        assert not (event_rows[event_idx - 1].get("role") == "tool"
                    and event_rows[event_idx + 1].get("role") == "tool")
        successor = history[event_position]
        if successor.get("tool_calls"):
            matches = [i for i, m in enumerate(event_rows)
                       if m.get("tool_calls") == successor["tool_calls"]]
            if matches:
                assert event_idx < matches[0], "event moved after its original successor"
        elif successor.get("content") == "Done.":
            # All final answers have identical text. Match this successor's
            # occurrence from the END, not the first content-equal twin.
            rank = sum(m.get("role") == "assistant" and m.get("content") == "Done."
                       for m in history[event_position:])
            matches = [i for i, m in enumerate(event_rows)
                       if m.get("role") == "assistant" and m.get("content") == "Done."]
            if event_position == 47:
                assert len(matches) >= rank + 2, "duplicate predecessors and successor must survive"
            if len(matches) >= rank:
                assert event_idx < matches[-rank], "event moved after its own text successor"
            predecessor = history[event_position - 1]
            positions = [i for i, m in enumerate(event_rows)
                         if m.get("role") == predecessor["role"] and
                         (m.get("tool_call_id") == predecessor.get("tool_call_id")
                          if predecessor.get("role") == "tool"
                          else m.get("content") == predecessor.get("content"))]
            if event_position == 47:
                assert positions, "original predecessor must survive compression"
                assert event_idx == positions[-1] + 1, "event must follow its own user turn"
                assert event_rows[event_idx + 1].get("content") == "Done."
                assert event_rows[event_idx + 2].get("content") == "short question 3"
            if positions:
                assert positions[-1] < event_idx, "event moved before its original predecessor"

    @pytest.mark.parametrize("engine", ["builtin", "lcm"])
    @pytest.mark.parametrize("event_position", [1, 3, 7, 19])
    def test_compaction_event_stays_between_original_neighbours_with_head_twins(
        self, notice_env, stream, engine, event_position, tmp_path
    ):
        make_agent, handler, db, sid, _ = notice_env
        handler.response_queue[:] = [
            ("", {**VALID_NOTICE, "kind": "tool_call_as_text"}), ("First.", None)
        ]
        make_agent(stream=stream).run_conversation("first", conversation_history=[], task_id="writer")
        event = next(m for m in db.get_messages_as_conversation(sid)
                     if is_metadata_only_tool_notice(m))
        big = " ".join(f"w{j}" for j in range(700))
        history: list[dict] = [{"role": "user", "content": "start"},
                               {"role": "assistant", "content": "Done."}]
        for i in range(8):
            call_id = f"call_headtwin_{i}"
            history.extend([
                {"role": "user", "content": f"u{i} {big}"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": call_id, "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": f"result {call_id} {big}"},
                {"role": "assistant", "content": "Done."},
            ])

        def run(arm):
            rows = json.loads(json.dumps(history))
            if arm == "event":
                rows.insert(event_position, dict(event))
            agent = make_agent(stream=stream)
            agent.session_id = f"headtwin-{engine}-{event_position}-{arm}"
            agent.compression_enabled = True
            if engine == "lcm":
                from plugins.context_engine.lcm.config import LCMConfig
                from plugins.context_engine.lcm.engine import LCMEngine
                cc = LCMEngine(config=LCMConfig(
                    database_path=str(tmp_path / f"{arm}.db"), fresh_tail_count=8,
                    leaf_chunk_tokens=1, context_threshold=0.01), hermes_home=str(tmp_path))
                cc.update_model("test-model", 200_000, provider="unit-test")
                cc.on_session_start(agent.session_id, hermes_home=str(tmp_path), model="test-model",
                                    provider="unit-test", context_length=200_000, platform="pytest")
                agent.context_compressor = cc
            else:
                agent.context_compressor.threshold_tokens = 8000
                agent.context_compressor._generate_summary = lambda *a, **kw: "compacted summary"
            response = MagicMock()
            response.choices = [MagicMock()]
            response.choices[0].message.content = "compacted summary"
            response.usage = None
            handler.captured_requests = []
            handler.response_queue[:] = [("Done.", None)]
            with patch("agent.auxiliary_client.call_llm", return_value=response):
                agent.run_conversation("next question", conversation_history=rows, task_id="compact")
            assert agent.context_compressor.compression_count > 0
            wire1 = _chat_requests(handler)[-1]["messages"]
            stored = db.get_messages_as_conversation(agent.session_id)
            handler.captured_requests = []
            handler.response_queue[:] = [("Second.", None)]
            resumed = make_agent(stream=stream)
            resumed.session_id = agent.session_id
            resumed.compression_enabled = False
            resumed.run_conversation("follow up", conversation_history=stored, task_id="resume")
            return wire1, _chat_requests(handler)[-1]["messages"], stored

        control1, control2, control_rows = run("control")
        event1, event2, event_rows = run("event")
        def projection(rows):
            return [(m.get("role"), m.get("content"), m.get("tool_calls"), m.get("tool_call_id"))
                    for m in rows if not is_metadata_only_tool_notice(m)]
        assert projection(event1) == projection(control1)
        assert projection(event2) == projection(control2)
        assert projection(event_rows) == projection(control_rows)
        event_indices = [i for i, m in enumerate(event_rows) if is_metadata_only_tool_notice(m)]
        assert len(event_indices) == 1
        index = event_indices[0]
        summary = next(i for i, m in enumerate(event_rows)
                       if "compacted summary" in str(m.get("content")))
        if event_position == 1:
            if any(m.get("content") == "start" for m in control_rows):
                assert index == 1, "head event moved behind its original successor"
                assert event_rows[index - 1]["content"] == "start"
                assert event_rows[index + 1]["content"] == "Done."
            else:
                assert index > summary, "event with no surviving head neighbour entered the head"
        elif event_position in (3, 7):
            assert index > summary, "dropped-middle event moved into the protected head"
        else:
            predecessor = history[event_position - 1]["content"]
            successor = history[event_position]["content"]
            if any(m.get("content") == predecessor for m in control_rows):
                assert event_rows[index - 1]["content"] == predecessor
                assert event_rows[index + 1]["content"] == successor
            else:
                assert index > summary, "event from the dropped middle entered protected head"

    def test_contentful_tagged_system_is_not_stripped_before_compression(self, notice_env, stream):
        make_agent, handler, db, sid, _ = notice_env
        tagged = {"role": "system", "content": "CONTENTFUL-COMPRESS-ANCHOR",
                  "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
                  "display_metadata": {CONFAB_NOTICE_KEY: {**VALID_NOTICE, "kind": "tool_call_as_text"}}}
        rows = [tagged]
        for i in range(40):
            rows.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"turn {i} " + " ".join(f"w{j}" for j in range(900))})
        agent = make_agent(stream=stream)
        agent.compression_enabled = True
        agent.context_compressor.threshold_tokens = 2000
        agent.context_compressor._generate_summary = lambda *a, **kw: "compacted summary"
        compress = agent.context_compressor.compress
        seen = []
        def inspect_engine(input_rows, **kwargs):
            seen.append(input_rows)
            return compress(input_rows, **kwargs)
        agent.context_compressor.compress = inspect_engine
        handler.response_queue.append(("Done.", None))
        agent.run_conversation("next", conversation_history=rows, task_id="contentful-compress")
        assert seen and any(tagged in attempt for attempt in seen)

    def test_same_request_id_retry_has_one_durable_event(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        notice = {**VALID_NOTICE, "kind": "tool_call_as_text", "request_id": "same-rid"}
        handler.response_queue.extend([("", notice), ("", notice), ("Done.", None)])
        result = make_agent(stream=stream).run_conversation("hello", conversation_history=[], task_id="t1")
        assert result["final_response"] == "Done."
        assert len(_chat_requests(handler)) == 3
        assert sum("Tool call not executed" in text for text in _lifecycle_texts(statuses)) == 1
        assert len([r for r in db.get_messages(sid) if r["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND]) == 1

    def test_scaffold_notice_preserves_in_band_tool_guard(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        guard = "Tool call not executed. Re-issue using the native interface."
        handler.response_queue.append((guard, dict(VALID_NOTICE)))
        result = make_agent(stream=stream).run_conversation("hello", conversation_history=[], task_id="t1")
        assert len(_chat_requests(handler)) == 1
        assert result["final_response"] == guard
        assert len(_confab_statuses(statuses)) == 1
        assert [r["content"] for r in db.get_messages(sid) if r["role"] == "assistant"] == [guard]

    @pytest.mark.parametrize("kind", ["tool_call_unparseable", "tool_call_as_text"])
    @pytest.mark.parametrize("prose", ["", "Work is incomplete."])
    def test_tool_notice_recovers_before_empty_response(self, notice_env, stream, kind, prose):
        make_agent, handler, db, sid, statuses = notice_env
        notice = {**VALID_NOTICE, "kind": kind, "grammar": "opaque-label"}
        handler.response_queue.extend([(prose, notice), ("Recovered model answer.", None)])
        agent = make_agent(stream=stream)
        result = agent.run_conversation("hello", conversation_history=[], task_id="t1")
        assert len(_chat_requests(handler)) == 2
        assert not getattr(agent, "_empty_content_retries", 0)
        correction = _chat_requests(handler)[1]["messages"][-1]["content"]
        assert "re-issue" in correction.lower()
        assert ("JSON" in correction) == (kind == "tool_call_unparseable")
        assert ("native" in correction.lower()) == (kind == "tool_call_as_text")
        assert result["final_response"] == "Recovered model answer."
        assert all(correction not in (r["content"] or "") for r in db.get_messages(sid))
        notices = [r for r in db.get_messages(sid) if r["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND]
        assert len(notices) == 1
        metadata = notices[0]["display_metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert metadata[CONFAB_NOTICE_KEY] == notice
        assert not _confab_statuses(statuses)
        assert [r["content"] for r in db.get_messages(sid) if r["role"] == "assistant"] == ["Recovered model answer."]

        history = db.get_messages_as_conversation(sid)
        handler.captured_requests = []
        handler.response_queue.append(("second", None))
        make_agent(stream=stream).run_conversation("again", conversation_history=history, task_id="t2")
        requests = _chat_requests(handler)
        assert len(requests) == 1
        blob = json.dumps(requests[0]["messages"])
        assert "display_kind" not in blob
        assert "display_metadata" not in blob
        assert notice["grammar"] not in blob
        assert correction not in blob
        assert "Recovered model answer." in blob

    @pytest.mark.parametrize("kind", ["tool_call_unparseable", "tool_call_as_text"])
    def test_tool_notice_recovery_exhausts_shared_budget(self, notice_env, stream, kind):
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.extend([
            ("", {**VALID_NOTICE, "kind": kind, "request_id": f"attempt-{i}"})
            for i in range(6)
        ])
        agent = make_agent(stream=stream)
        result = agent.run_conversation("hello", conversation_history=[], task_id="t1")
        assert len(_chat_requests(handler)) == 4
        assert result["failed"] is True
        assert not getattr(agent, "_empty_content_retries", 0)
        assert not any(m.get("_dropped_toolcall_nudge") for m in result["messages"])
        assert len([r for r in db.get_messages(sid) if r["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND]) == 4
        assert not [r for r in db.get_messages(sid) if r["role"] == "assistant"]

    def test_tool_notice_and_dropped_call_share_budget(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.extend([
            ("Trying.", None, "tool_calls"),
            ("", {**VALID_NOTICE, "kind": "tool_call_unparseable"}),
            ("Trying again.", None, "tool_calls"),
            ("", {**VALID_NOTICE, "kind": "tool_call_as_text", "request_id": "last"}),
        ])
        agent = make_agent(stream=stream)
        result = agent.run_conversation("hello", conversation_history=[], task_id="t1")
        assert len(_chat_requests(handler)) == 4
        assert result["failed"] is True
        assert not getattr(agent, "_empty_content_retries", 0)
        assert not any(m.get("_dropped_toolcall_nudge") for m in result["messages"])
        assert not [r for r in db.get_messages(sid) if r["role"] == "assistant"]

    def test_status_shown_once_and_row_persisted(self, notice_env, stream):
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.append(("All good here.", dict(VALID_NOTICE)))

        agent = make_agent(stream=stream)
        agent.run_conversation("hello", conversation_history=[], task_id="t1")

        # 1. Current-turn warning, delivered out of band, exactly once.
        assert len(_confab_statuses(statuses)) == 1

        # 2. Durable triage record on the assistant row.
        rows = [r for r in db.get_messages(sid) if r["role"] == "assistant"]
        assert rows, "no assistant row persisted"
        row = rows[-1]
        assert row["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND
        meta = row["display_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta[CONFAB_NOTICE_KEY] == VALID_NOTICE
        assert meta[CONFAB_NOTICE_KEY]["version"] == 1
        assert meta[CONFAB_NOTICE_KEY]["kind"] == "scaffold_confab_removed"

        # 3. Assistant content is byte-identical to the cleaned model text —
        #    nothing appended, nothing prepended.
        assert row["content"] == "All good here."

    def test_next_request_carries_neither_field_nor_notice_text(
        self, notice_env, stream
    ):
        """Asserted on the OUTGOING wire bytes, not on internal state."""
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.append(("All good here.", dict(VALID_NOTICE)))
        handler.response_queue.append(("second", None))

        agent1 = make_agent(stream=stream)
        agent1.run_conversation("hello", conversation_history=[], task_id="t1")

        # Turn N+1 — fresh agent, history reloaded from the store.
        history = db.get_messages_as_conversation(sid)
        assert any(m.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND for m in history)

        handler.captured_requests = []
        agent2 = make_agent(stream=stream)
        agent2.run_conversation("again", conversation_history=history, task_id="t2")

        reqs = _chat_requests(handler)
        assert reqs, "turn N+1 made no provider request"
        for req in reqs:
            blob = json.dumps(req.get("messages", []))
            assert "display_kind" not in blob
            assert "display_metadata" not in blob
            assert CONFAB_NOTICE_FIELD not in blob
            assert "scaffold_confab_removed" not in blob
            assert VALID_NOTICE["request_id"] not in blob
            assert "onfabulation" not in blob
            for m in req.get("messages", []):
                assert "display_kind" not in m
                assert "display_metadata" not in m

    def test_todays_bridge_behavior_unchanged(self, notice_env, stream):
        """No extension field (in-band marker era) → nothing changes."""
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.append(("All good here.", None))

        agent = make_agent(stream=stream)
        agent.run_conversation("hello", conversation_history=[], task_id="t1")

        assert _confab_statuses(statuses) == []
        rows = [r for r in db.get_messages(sid) if r["role"] == "assistant"]
        assert rows[-1]["display_kind"] is None
        assert rows[-1]["display_metadata"] in (None, "", {})
        assert rows[-1]["content"] == "All good here."

    def test_invalid_notice_is_ignored_not_displayed_or_persisted(
        self, notice_env, stream
    ):
        """Spec test 7 — unvalidated data never becomes trusted status."""
        make_agent, handler, db, sid, statuses = notice_env
        handler.response_queue.append(
            ("All good here.", {**VALID_NOTICE, "version": 99})
        )

        agent = make_agent(stream=stream)
        agent.run_conversation("hello", conversation_history=[], task_id="t1")

        assert _confab_statuses(statuses) == []
        rows = [r for r in db.get_messages(sid) if r["role"] == "assistant"]
        assert rows[-1]["display_kind"] is None
