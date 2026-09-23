"""Streaming-transport end-to-end coverage for the out-of-band confab notice.

FleetReview's "Streaming Untested" P1 on PR #764 observed that the chunk
fixture verified only SDK retention and direct extraction, and argued the
suite would stay green even if a valid notice were silently dropped for every
streaming response.

Measured disposition (see the module's test docstrings): the pre-existing
``test_confab_notice_e2e.py`` ``[stream]`` arm DOES bite — disabling the
forward of the accumulated notice onto the synthetic completion reds 2 of its
tests. What was genuinely uncovered is the AGGREGATOR's own contract: which
chunk the notice may ride, and what happens when more than one arrives. Those
are the branches this module drives, end to end through the real streaming
transport, all the way to the stamped assistant row.

Also pins the fixed **Duplicate Accepted** P1: a second valid notice in one
stream invalidates the accumulator and NO notice is forwarded.
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

# A SECOND, conflicting notice — different request_id and grammar, still
# schema-valid. This is the payload the duplicate contract must reject.
CONFLICTING_NOTICE = {
    "version": 1,
    "kind": "scaffold_confab_removed",
    "request_id": "ffffffff",
    "scope": "both",
    "grammar": "outbound",
}


class _StreamHandler(BaseHTTPRequestHandler):
    """Streams SSE chunks, placing notices wherever the scenario dictates.

    ``scenario`` is ``(text, notices_by_position)`` where the mapping keys are
    ``'first_delta'``, ``'content'``, ``'finish'`` and ``'usage'``.
    """

    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)

        if "messages" in req and type(self).response_queue:
            text, placements = type(self).response_queue.pop(0)
        else:
            text, placements = "DONE", {}

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        chunks = [
            ("first_delta", {"id": "m", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
            ("content", {"id": "m", "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
            ("finish", {"id": "m", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            ("usage", {"id": "m", "choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
        ]

        for position, chunk in chunks:
            if position in placements:
                chunk[CONFAB_NOTICE_FIELD] = placements[position]
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def stream_env():
    """Mock streaming provider + isolated HERMES_HOME + SessionDB.

    Every resource is acquired inside the try, so a failure part-way through
    setup still runs the matching teardown; the listening socket is released
    with ``server_close()`` so a failing run cannot leak a bound port.
    """
    _StreamHandler.captured_requests = []
    _StreamHandler.response_queue = []

    srv = None
    test_home = None
    db = None
    prev_home = os.environ.get("HERMES_HOME")

    try:
        srv = HTTPServer(("127.0.0.1", 0), _StreamHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        test_home = tempfile.mkdtemp(prefix="hermes_confab_stream_")
        os.makedirs(os.path.join(test_home, ".hermes"))
        os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

        from run_agent import AIAgent

        db = SessionDB(db_path=Path(test_home) / "state.db")
        sid = "sess-confab-stream"
        statuses: list = []

        def make_agent():
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
            # STREAMING is the whole point of this module.
            agent._disable_streaming = False
            agent.status_callback = lambda kind, message: statuses.append((kind, message))
            return agent

        yield make_agent, _StreamHandler, db, sid, statuses
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        if db is not None:
            db.close()
        if test_home is not None:
            shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _confab_statuses(statuses) -> list:
    return [m for kind, m in statuses if kind == "lifecycle" and "onfabulation" in m]


def _assistant_rows(db, sid) -> list:
    return [r for r in db.get_messages(sid) if r["role"] == "assistant"]


def _row_notice(row):
    meta = row["display_metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return (meta or {}).get(CONFAB_NOTICE_KEY)


class TestStreamingTransportCarriesTheNoticeThrough:
    """The notice must survive chunk processing → aggregation → stamped row."""

    @pytest.mark.parametrize("position", ["usage", "finish", "content", "first_delta"])
    def test_notice_on_any_chunk_reaches_the_persisted_assistant_row(
        self, stream_env, position
    ):
        """Contract puts it on the final usage chunk; earlier is tolerated.

        Driven end to end: real SSE bytes → streaming transport → normalized
        response → persisted row. A silent drop anywhere in that chain reds
        this test, which is exactly what the P1 asked for.
        """
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append(("All good here.", {position: dict(VALID_NOTICE)}))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        rows = _assistant_rows(db, sid)
        assert rows, "no assistant row persisted"
        row = rows[-1]

        assert row["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND
        assert _row_notice(row) == VALID_NOTICE
        # The user was told, on this turn, exactly once.
        assert len(_confab_statuses(statuses)) == 1
        # And the reply itself is untouched.
        assert row["content"] == "All good here."

    def test_a_clean_stream_stamps_nothing(self, stream_env):
        """Non-vacuity: the assertions above must be able to come out empty."""
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append(("All good here.", {}))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        rows = _assistant_rows(db, sid)
        assert rows[-1]["display_kind"] is None
        assert _confab_statuses(statuses) == []

    def test_a_schema_invalid_notice_in_the_stream_is_dropped(self, stream_env):
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append(
            ("All good here.", {"usage": {**VALID_NOTICE, "version": 99}})
        )

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        rows = _assistant_rows(db, sid)
        assert rows[-1]["display_kind"] is None
        assert _confab_statuses(statuses) == []


class TestDuplicateNoticeInOneStream:
    """P1 Duplicate Accepted — agent/chat_completion_helpers.py:5225.

    Two valid notices for one response means the producer is misbehaving and
    the consumer cannot know which one describes this reply. Publishing the
    first would hand the user and persisted history potentially WRONG triage
    metadata, so the contract is: reject the response extension entirely.
    """

    def test_two_conflicting_notices_forward_NO_notice(self, stream_env):
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append((
            "All good here.",
            {"content": dict(VALID_NOTICE), "usage": dict(CONFLICTING_NOTICE)},
        ))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        rows = _assistant_rows(db, sid)
        row = rows[-1]

        # Neither notice wins — the whole extension is rejected.
        assert row["display_kind"] is None, "a duplicate-notice response was still stamped"
        assert _row_notice(row) in (None, {}) or not _row_notice(row)
        assert _confab_statuses(statuses) == []
        # The reply text is still delivered untouched: rejecting the extension
        # must not cost the user the actual model answer.
        assert row["content"] == "All good here."

    def test_the_first_notice_is_not_silently_retained(self, stream_env):
        """The specific pre-fix behaviour, stated as its own assertion."""
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append((
            "All good here.",
            {"content": dict(VALID_NOTICE), "usage": dict(CONFLICTING_NOTICE)},
        ))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        persisted = json.dumps(_assistant_rows(db, sid)[-1], default=str)
        assert VALID_NOTICE["request_id"] not in persisted
        assert CONFLICTING_NOTICE["request_id"] not in persisted

    def test_two_IDENTICAL_notices_are_also_rejected(self, stream_env):
        """The contract counts notices, not distinct values.

        A consumer that de-duplicated by equality would look correct here and
        still be wrong: 'exactly one' is the validation contract, and two
        deliveries mean the producer is not honouring it.
        """
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append((
            "All good here.",
            {"content": dict(VALID_NOTICE), "usage": dict(VALID_NOTICE)},
        ))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        assert _assistant_rows(db, sid)[-1]["display_kind"] is None
        assert _confab_statuses(statuses) == []

    def test_one_notice_plus_one_INVALID_payload_still_accepts_the_valid_one(
        self, stream_env
    ):
        """Only VALID notices count toward the limit — junk is just dropped.

        Otherwise a provider could suppress a genuine catch by appending a
        malformed second payload.
        """
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append((
            "All good here.",
            {"content": dict(VALID_NOTICE), "usage": {"version": 99, "kind": "junk"}},
        ))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        row = _assistant_rows(db, sid)[-1]
        assert row["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND
        assert _row_notice(row) == VALID_NOTICE
        assert len(_confab_statuses(statuses)) == 1


class TestStreamingNoticeIsNeverReplayed:
    """Presentation-only, in the streaming lane too — asserted on wire bytes."""

    def test_next_streaming_request_carries_no_presentation_field(self, stream_env):
        make_agent, handler, db, sid, statuses = stream_env
        handler.response_queue.append(("All good here.", {"usage": dict(VALID_NOTICE)}))
        handler.response_queue.append(("second", {}))

        make_agent().run_conversation("hello", conversation_history=[], task_id="t1")

        history = db.get_messages_as_conversation(sid)
        assert any(m.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND for m in history)

        handler.captured_requests = []
        make_agent().run_conversation("again", conversation_history=history, task_id="t2")

        reqs = [r for r in handler.captured_requests if "messages" in r]
        assert reqs, "turn N+1 made no provider request"
        for req in reqs:
            blob = json.dumps(req.get("messages", []))
            assert "display_kind" not in blob
            assert "display_metadata" not in blob
            assert CONFAB_NOTICE_FIELD not in blob
            assert "scaffold_confab_removed" not in blob
            assert VALID_NOTICE["request_id"] not in blob
            assert "onfabulation" not in blob
