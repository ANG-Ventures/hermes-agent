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

import pytest

from agent.confab_notice import (
    CONFAB_NOTICE_DISPLAY_KIND,
    CONFAB_NOTICE_FIELD,
    CONFAB_NOTICE_KEY,
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
