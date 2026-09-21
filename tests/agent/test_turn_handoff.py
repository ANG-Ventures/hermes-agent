"""A turn cut by an unrecoverable provider failure must leave a handoff.

THE BUG (2026-09-21 01:18, #apollo): the fallback chain exhausts mid-turn, the
turn dies with "Operation interrupted" / "the model provider is rate-limiting",
and everything in flight at the cut — the tool calls already issued, their
results, the half-written assistant text, the open todo items — is gone. The
next session reconstructs it from Discord scrollback: 15-25 minutes per
turnover.

This module writes ``$HERMES_HOME/state/turn-handoff/<session_key>.json`` at
the cut and injects it into the next turn. The invariants:

* the handoff is MACHINE-READABLE (structured JSON), not a prose message;
* tool results are truncated to 500 chars so a giant read_file can't bloat it;
* it expires after 24 h and is consumed exactly once;
* writing it can NEVER raise into the dying turn — a failed handoff costs
  context, a raised handoff costs the error message too.
"""

from __future__ import annotations

import json
import time

import pytest

from agent.turn_handoff import (
    HANDOFF_TTL_SECONDS,
    TOOL_RESULT_PREVIEW_CHARS,
    build_turn_handoff,
    capture_turn_handoff,
    consume_handoff_context,
    consume_turn_handoff,
    format_handoff_notice,
    handoff_path_for,
    prune_expired_handoffs,
    render_handoff_context,
    write_turn_handoff,
)


def _messages():
    return [
        {"role": "user", "content": "older turn", "row_id": 10},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "audit the fleet cron jobs", "row_id": 42},
        {
            "role": "assistant",
            "content": "Starting the audit. First the job list.",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "terminal",
                             "arguments": '{"command": "hermes cron list"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "job-a\njob-b"},
        {"role": "assistant", "content": "Two jobs. Now reading job-a."},
    ]


class _Store:
    def __init__(self, todos):
        self._todos = todos

    def read(self):
        return list(self._todos)


class _Agent:
    def __init__(
        self,
        *,
        session_key="discord:123",
        todos=None,
        streamed_text="",
    ):
        self._gateway_session_key = session_key
        self.session_id = "sess-abc"
        self.model = "claude-opus-5"
        self.provider = "claude-bpx-17"
        self._todo_store = _Store(todos or [])
        self._current_streamed_assistant_text = streamed_text

    @staticmethod
    def _strip_think_blocks(text):
        return text.replace("<think>secret scratchpad</think>", "")


# ── building ────────────────────────────────────────────────────────────


class TestBuildTurnHandoff:
    def test_captures_the_last_user_message_id(self):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="provider is rate-limiting")
        assert h["last_user_message"]["row_id"] == 42
        assert "audit the fleet cron jobs" in h["last_user_message"]["content"]

    def test_captures_the_in_progress_assistant_text_in_order(self):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        text = h["assistant_progress"]
        assert "Starting the audit" in text
        assert "Now reading job-a" in text
        # Prior turns must not bleed in.
        assert "older answer" not in text

    def test_captures_tool_calls_with_their_results(self):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        assert len(h["tool_calls"]) == 1
        call = h["tool_calls"][0]
        assert call["name"] == "terminal"
        assert "hermes cron list" in call["arguments"]
        assert call["result_preview"] == "job-a\njob-b"

    def test_tool_results_are_truncated_to_the_preview_budget(self):
        msgs = _messages()
        msgs[4]["content"] = "x" * 5000
        h = build_turn_handoff(_Agent(), msgs, turn_start_idx=2, reason="x")
        preview = h["tool_calls"][0]["result_preview"]
        assert len(preview) <= TOOL_RESULT_PREVIEW_CHARS + 32
        assert preview.startswith("x" * 100)
        assert "truncated" in preview

    def test_a_tool_call_with_no_result_is_recorded_as_unfinished(self):
        """The call that was in flight AT the cut is the most valuable one."""
        msgs = _messages()[:4]  # assistant issued call_1, no tool row follows
        h = build_turn_handoff(_Agent(), msgs, turn_start_idx=2, reason="x")
        assert h["tool_calls"][0]["result_preview"] is None
        assert h["tool_calls"][0]["completed"] is False

    def test_captures_open_todo_items_only(self):
        agent = _Agent(todos=[
            {"id": "1", "content": "read job-a", "status": "completed"},
            {"id": "2", "content": "read job-b", "status": "in_progress"},
            {"id": "3", "content": "write the report", "status": "pending"},
            {"id": "4", "content": "dropped", "status": "cancelled"},
        ])
        h = build_turn_handoff(agent, _messages(), turn_start_idx=2, reason="x")
        assert [t["content"] for t in h["open_todos"]] == [
            "read job-b", "write the report",
        ]

    def test_records_the_cut_reason_and_route(self):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="provider is rate-limiting")
        assert h["reason"] == "provider is rate-limiting"
        assert h["provider"] == "claude-bpx-17"
        assert h["model"] == "claude-opus-5"
        assert h["created_at"] == pytest.approx(time.time(), abs=5)

    def test_partial_stream_is_preserved_without_reasoning(self):
        agent = _Agent(
            streamed_text=(
                "<think>secret scratchpad</think>"
                "Half-written answer visible to the user…"
            )
        )
        msgs = [{"role": "user", "content": "hi", "row_id": 1}]

        h = build_turn_handoff(agent, msgs, turn_start_idx=0, reason="x")

        assert h is not None
        assert h["assistant_progress"] == "Half-written answer visible to the user…"
        assert "secret scratchpad" not in h["assistant_progress"]

    def test_partial_stream_and_materialized_progress_are_preserved_once(self):
        agent = _Agent(
            streamed_text=(
                "Earlier completed progress.\n\n"
                "Half-written answer visible to the user…"
            )
        )
        msgs = [
            {"role": "user", "content": "hi", "row_id": 1},
            {"role": "assistant", "content": "Earlier completed progress."},
        ]

        h = build_turn_handoff(agent, msgs, turn_start_idx=0, reason="x")

        assert h is not None
        assert h["assistant_progress"].count("Earlier completed progress.") == 1
        assert "Half-written answer visible to the user…" in h["assistant_progress"]

    def test_a_user_only_early_cut_still_yields_a_handoff(self):
        msgs = [{"role": "user", "content": "hi", "row_id": 1}]

        h = build_turn_handoff(_Agent(), msgs, turn_start_idx=0, reason="x")

        assert h is not None
        assert h["last_user_message"] == {"row_id": 1, "content": "hi"}
        assert h["assistant_progress"] == ""
        assert h["tool_calls"] == []

    def test_an_out_of_range_turn_index_is_tolerated(self):
        assert build_turn_handoff(_Agent(), _messages(), turn_start_idx=99,
                                  reason="x") is None

    def test_missing_todo_store_is_tolerated(self):
        agent = _Agent()
        del agent._todo_store
        h = build_turn_handoff(agent, _messages(), turn_start_idx=2, reason="x")
        assert h["open_todos"] == []


# ── round trip ──────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_partial_stream_user_and_todo_round_trip_into_next_turn(self, tmp_path):
        agent = _Agent(
            todos=[{
                "id": "todo-1",
                "content": "finish the fleet audit",
                "status": "in_progress",
            }],
            streamed_text="Half-written answer visible to the user…",
        )
        msgs = [{
            "role": "user",
            "content": "audit the fleet cron jobs",
            "row_id": 42,
        }]

        notice = capture_turn_handoff(
            agent,
            msgs,
            turn_start_idx=0,
            reason="quota wall",
            root=tmp_path,
        )
        context = consume_handoff_context(agent, root=tmp_path)

        assert "Handoff saved" in notice
        assert "audit the fleet cron jobs" in context
        assert "Half-written answer visible to the user…" in context
        assert "finish the fleet audit" in context
        assert consume_handoff_context(agent, root=tmp_path) == ""

    def test_write_then_consume_returns_the_same_payload(self, tmp_path):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="quota wall")
        assert write_turn_handoff("discord:123", h, root=tmp_path) is True
        loaded = consume_turn_handoff("discord:123", root=tmp_path)
        assert loaded["last_user_message"]["row_id"] == 42
        assert loaded["tool_calls"][0]["name"] == "terminal"

    def test_consume_is_once_only(self, tmp_path):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        write_turn_handoff("discord:123", h, root=tmp_path)
        assert consume_turn_handoff("discord:123", root=tmp_path) is not None
        assert consume_turn_handoff("discord:123", root=tmp_path) is None

    def test_sessions_do_not_cross_contaminate(self, tmp_path):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        write_turn_handoff("discord:123", h, root=tmp_path)
        assert consume_turn_handoff("telegram:999", root=tmp_path) is None
        assert consume_turn_handoff("discord:123", root=tmp_path) is not None

    def test_absent_handoff_is_not_an_error(self, tmp_path):
        assert consume_turn_handoff("nobody", root=tmp_path) is None

    def test_corrupt_handoff_is_dropped_not_raised(self, tmp_path):
        p = handoff_path_for("discord:123", root=tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        assert consume_turn_handoff("discord:123", root=tmp_path) is None
        assert not p.exists(), "a corrupt handoff must not be retried forever"

    def test_a_session_key_with_path_separators_cannot_escape_the_root(self, tmp_path):
        key = "../../etc/passwd"
        p = handoff_path_for(key, root=tmp_path)
        assert tmp_path in p.parents
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        write_turn_handoff(key, h, root=tmp_path)
        assert consume_turn_handoff(key, root=tmp_path) is not None

    def test_write_never_raises_on_an_unwritable_root(self, tmp_path):
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("x")
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="x")
        assert write_turn_handoff("k", h, root=blocked / "sub") is False

    def test_writing_none_is_a_no_op(self, tmp_path):
        assert write_turn_handoff("k", None, root=tmp_path) is False


# ── expiry ──────────────────────────────────────────────────────────────


class TestExpiry:
    def _stale(self, tmp_path, key, age):
        p = handoff_path_for(key, root=tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"created_at": time.time() - age,
                                 "assistant_progress": "old"}))
        return p

    def test_an_expired_handoff_is_not_consumed(self, tmp_path):
        self._stale(tmp_path, "k", HANDOFF_TTL_SECONDS + 60)
        assert consume_turn_handoff("k", root=tmp_path) is None

    def test_an_expired_handoff_is_deleted_on_consume(self, tmp_path):
        p = self._stale(tmp_path, "k", HANDOFF_TTL_SECONDS + 60)
        consume_turn_handoff("k", root=tmp_path)
        assert not p.exists()

    def test_a_fresh_handoff_survives(self, tmp_path):
        self._stale(tmp_path, "k", 60)
        assert consume_turn_handoff("k", root=tmp_path) is not None

    def test_ttl_is_24h(self):
        assert HANDOFF_TTL_SECONDS == 24 * 60 * 60

    def test_prune_removes_only_expired_files(self, tmp_path):
        self._stale(tmp_path, "old", HANDOFF_TTL_SECONDS + 60)
        self._stale(tmp_path, "new", 60)
        assert prune_expired_handoffs(root=tmp_path) == 1
        assert handoff_path_for("new", root=tmp_path).exists()
        assert not handoff_path_for("old", root=tmp_path).exists()

    def test_prune_on_a_missing_root_is_zero(self, tmp_path):
        assert prune_expired_handoffs(root=tmp_path / "nope") == 0


# ── rendering ───────────────────────────────────────────────────────────


class TestRendering:
    def test_notice_is_three_lines_and_names_the_resume_path(self):
        h = build_turn_handoff(_Agent(), _messages(), turn_start_idx=2,
                               reason="the provider is rate-limiting")
        notice = format_handoff_notice(h)
        assert len(notice.strip().splitlines()) == 3
        assert "Handoff saved" in notice
        assert "/resume-handoff" in notice
        assert "rate-limiting" in notice

    def test_notice_reports_what_was_captured(self):
        agent = _Agent(todos=[{"id": "1", "content": "t", "status": "pending"}])
        h = build_turn_handoff(agent, _messages(), turn_start_idx=2, reason="x")
        notice = format_handoff_notice(h)
        assert "1 tool call" in notice
        assert "1 open todo" in notice

    def test_context_render_carries_the_load_bearing_facts(self):
        agent = _Agent(todos=[
            {"id": "1", "content": "write the report", "status": "pending"},
        ])
        h = build_turn_handoff(agent, _messages(), turn_start_idx=2,
                               reason="quota wall")
        ctx = render_handoff_context(h)
        assert "audit the fleet cron jobs" in ctx
        assert "terminal" in ctx
        assert "job-a" in ctx
        assert "write the report" in ctx
        assert "Now reading job-a" in ctx

    def test_context_render_of_an_empty_handoff_is_empty(self):
        assert render_handoff_context(None) == ""
        assert render_handoff_context({}) == ""
