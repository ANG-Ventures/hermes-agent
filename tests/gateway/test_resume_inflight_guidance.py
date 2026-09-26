"""Resume guidance for a turn a gateway restart cut mid-flight.

Regression for 2026-09-25: parity merge #510 dropped the only production call
of ``_build_resume_pending_message``. The boot scheduler kept logging
``mode=auto`` for every interrupted sibling, but the resumed turn received the
generic "report the restore, ask what next, skip any unfinished work" note, so
Apollo's owner session dropped a PR create, a merge and a chain launch.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from gateway.auto_resume import describe_inflight_tool_calls
from gateway.run import _build_resume_pending_message

RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def _call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _history(*tail):
    return [{"role": "user", "content": "merge the PR and launch the chain"}, *tail]


def test_unanswered_mutating_call_is_named_with_check_effect():
    note = describe_inflight_tool_calls(_history(
        {"role": "assistant", "content": "",
         "tool_calls": [_call("c1", "terminal", {"command": "gh pr merge 42"})]},
    ))
    assert note is not None
    assert "terminal" in note and "gh pr merge 42" in note
    assert "check its effect before re-running it" in note


def test_unanswered_read_only_call_is_marked_reissue():
    note = describe_inflight_tool_calls(_history(
        {"role": "assistant", "content": "",
         "tool_calls": [_call("c1", "read_file", {"path": "/tmp/x"})]},
    ))
    assert "read_file" in note and "read-only: re-issue it" in note


def test_interrupted_result_counts_as_in_flight():
    note = describe_inflight_tool_calls(_history(
        {"role": "assistant", "content": "",
         "tool_calls": [_call("c1", "terminal", {"command": "pytest -q"})]},
        {"role": "tool", "tool_call_id": "c1",
         "content": '{"output": "[Command interrupted]", "exit_code": 130}'},
    ))
    assert note is not None and "pytest -q" in note


def test_answered_calls_and_raw_json_rows():
    answered = _history(
        {"role": "assistant", "content": "",
         # state.db rows carry tool_calls as JSON text
         "tool_calls": json.dumps([_call("c1", "terminal", {"command": "ls"})])},
        {"role": "tool", "tool_call_id": "c1", "content": '{"output": "a", "exit_code": 0}'},
    )
    assert describe_inflight_tool_calls(answered) is None
    unanswered = _history(
        {"role": "assistant", "content": "",
         "tool_calls": json.dumps([_call("c2", "write_file", {"path": "p"})])},
    )
    assert "write_file" in describe_inflight_tool_calls(unanswered)


def test_auto_mode_continues_and_names_inflight():
    inflight = describe_inflight_tool_calls(_history(
        {"role": "assistant", "content": "",
         "tool_calls": [_call("c1", "terminal", {"command": "gh pr create"})]},
    ))
    note, surface = _build_resume_pending_message(
        agent_history=[], message="", reason_phrase="a gateway shutdown",
        resume_mode="auto", resume_kind="sibling", inflight_note=inflight,
    )
    assert surface is False
    assert "continue your interrupted work now" in note
    assert "gh pr create" in note
    assert "skip any unfinished work" not in note


def test_new_message_in_auto_mode_does_not_drop_the_work():
    inflight = "Tool calls in flight when the gateway stopped (no result recorded): x."
    note, _ = _build_resume_pending_message(
        agent_history=[], message="status?", reason_phrase="a gateway shutdown",
        resume_mode="auto", resume_kind="sibling", inflight_note=inflight,
    )
    assert "Address the user's NEW message below FIRST" in note
    assert "CONTINUE that work unless the new message supersedes it" in note
    assert inflight in note
    assert "skip any unfinished work" not in note
    assert note.endswith("\n\nstatus?")


def test_prompt_mode_with_inflight_surfaces_instead_of_skipping():
    note, surface = _build_resume_pending_message(
        agent_history=[{"role": "assistant", "content": "working"}],
        message="", reason_phrase="a gateway shutdown", resume_mode="prompt",
        inflight_note="Tool calls in flight when the gateway stopped (no result recorded): y.",
    )
    assert surface is True
    assert "skip any unfinished work" not in note
    assert "(no result recorded): y." in note


def test_no_inflight_keeps_legacy_wording():
    note, surface = _build_resume_pending_message(
        agent_history=[{"role": "assistant", "content": "done"}],
        message="", reason_phrase="a gateway restart",
    )
    assert surface is False and "skip any unfinished work" in note


def _calls_outside_def(tree, fn_name):
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == fn_name:
            calls.append(node)
    return calls


def test_dispatch_site_consumes_the_scheduler_disposition():
    """The helper must be WIRED, not just unit-tested (the #510 regression)."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    calls = _calls_outside_def(tree, "_build_resume_pending_message")
    assert calls, "_build_resume_pending_message has no production call site"
    kw = {k.arg for c in calls for k in c.keywords}
    assert {"resume_mode", "auto_fallback_reason", "inflight_note"} <= kw
    reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and "_startup_resume_modes" in ast.unparse(n.func.value)
    ]
    assert reads, "nothing reads _startup_resume_modes: the auto/always disposition is dead"
