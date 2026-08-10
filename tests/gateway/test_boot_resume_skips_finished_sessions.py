"""Boot-resume must not spawn an LLM turn for a session with nothing to resume.

``resume_pending`` is a pre-drain HEDGE, marked for EVERY running session by
``_mark_resume_pending_for_shutdown`` (and re-marked wholesale by
``suspend_recently_active`` after an unclean exit) so a SIGKILL mid-drain cannot
lose in-flight work. It is cleared again only when ``stop()`` survives long
enough to run its clear pass. When the process dies before that — SIGKILL, OOM,
VM death — the marker survives on sessions whose turn had ALREADY delivered its
answer, and the next boot pays a full LLM turn to "recover" a finished
conversation while demoting that channel's ``busy_input_mode`` behind a banner
claiming the user's own work was interrupted.

The contract asserted here: the persisted transcript decides. A tail that proves
completion is skipped and its marker cleared; anything unfinished, ambiguous, or
unreadable still resumes exactly as before (fail-open).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import gateway.run as gateway_run
import hermes_state
from gateway.auto_resume import has_resumable_work
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


# --------------------------------------------------------------------------
# has_resumable_work: the pure classifier
# --------------------------------------------------------------------------


def test_completed_assistant_tail_has_no_resumable_work():
    assert (
        has_resumable_work(
            [
                {"role": "user", "content": "status?"},
                {"role": "assistant", "content": "All done.", "finish_reason": "stop"},
            ]
        )
        is False
    )


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}],
                "finish_reason": "tool_calls",
            },
            id="unanswered-tool-calls",
        ),
        pytest.param(
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            id="tool-result-never-answered",
        ),
        pytest.param(
            {"role": "user", "content": "and then?"},
            id="user-message-never-answered",
        ),
        pytest.param(
            {"role": "assistant", "content": "partial", "finish_reason": "interrupt_close"},
            id="interrupt-close",
        ),
        pytest.param(
            {"role": "assistant", "content": "partial", "finish_reason": None},
            id="no-finish-reason",
        ),
        pytest.param(
            {"role": "assistant", "content": "   ", "finish_reason": "stop"},
            id="empty-content-despite-stop",
        ),
        pytest.param(
            {
                "role": "assistant",
                "content": "text",
                "finish_reason": "stop",
                # hermes_state hands the safety-sensitive caller this sentinel
                # when tool_calls JSON is corrupt. Ambiguity must resume.
                "tool_calls": {"unparseable": True},
            },
            id="unparseable-tool-calls-sentinel",
        ),
        pytest.param(
            {"role": "assistant", "content": "text", "finish_reason": "verification_required"},
            id="unknown-future-finish-reason",
        ),
    ],
)
def test_unfinished_or_ambiguous_tails_keep_resumable_work(tail):
    assert has_resumable_work([{"role": "user", "content": "go"}, tail]) is True


def test_empty_transcript_fails_open():
    assert has_resumable_work([]) is True
    assert has_resumable_work([None, "junk"]) is True  # type: ignore[list-item]


# --------------------------------------------------------------------------
# Scheduler integration
# --------------------------------------------------------------------------


def _source(chat_id: str = "123") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id="u1",
    )


def _runner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    runner, adapter = make_restart_runner()
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.session_store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(),
    )
    db = runner.session_store._db
    assert isinstance(db, SessionDB)
    runner._session_db = AsyncSessionDB(db)
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def _scheduled_resume_stub(_adapter, _event, _session_key):
        return None

    monkeypatch.setattr(runner, "_run_startup_resume_event", _scheduled_resume_stub)
    return runner, adapter, db


def _seed(db: SessionDB, entry, rows: list[dict]) -> None:
    db.create_session(entry.session_id, "gateway", session_key=entry.session_key)
    for row in rows:
        db.append_message(
            entry.session_id,
            row["role"],
            row.get("content"),
            tool_calls=row.get("tool_calls"),
            tool_call_id=row.get("tool_call_id"),
            finish_reason=row.get("finish_reason"),
        )


_COMPLETED_TAIL = [
    {"role": "user", "content": "ship it"},
    {"role": "assistant", "content": "Shipped and verified.", "finish_reason": "stop"},
]

_INTERRUPTED_TAIL = [
    {"role": "user", "content": "ship it"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
        ],
        "finish_reason": "tool_calls",
    },
]


@pytest.mark.asyncio
async def test_finished_session_is_not_scheduled_and_marker_is_cleared(
    tmp_path, monkeypatch, caplog
):
    """The defect: a completed turn cost a real LLM turn on every boot."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    checked = await runner._prepare_boot_resume_work_check()
    assert checked == 1
    assert runner._boot_resume_has_work[entry.session_key] is False

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 0

    messages = [record.getMessage() for record in caplog.records]
    assert any("PHASE=boot_resume_skipped" in m for m in messages)
    assert not any("PHASE=boot_resume_scheduled" in m for m in messages)
    # No turn was spawned at all — that is the acceptance criterion.
    assert runner._background_tasks == set()
    assert runner._is_session_running(entry.session_key) is False
    # The hedge is retired so the next boot does not re-litigate it, but the
    # transcript and session_id are untouched.
    refreshed = runner.session_store._entries[entry.session_key]
    assert refreshed.resume_pending is False
    assert refreshed.session_id == entry.session_id
    assert len(db.get_messages(entry.session_id)) == len(_COMPLETED_TAIL)
    db.close()


@pytest.mark.asyncio
async def test_negative_control_genuinely_interrupted_session_still_resumes(
    tmp_path, monkeypatch, caplog
):
    """A session amputated mid-tool-call must still recover, unchanged."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    assert await runner._prepare_boot_resume_work_check() == 1
    assert runner._boot_resume_has_work[entry.session_key] is True

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 1

    messages = [record.getMessage() for record in caplog.records]
    assert any("PHASE=boot_resume_scheduled" in m for m in messages)
    assert not any("PHASE=boot_resume_skipped" in m for m in messages)
    assert runner.session_store._entries[entry.session_key].resume_pending is True
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_self_resume_handoff_is_exempt_from_the_finished_work_gate(
    tmp_path, monkeypatch
):
    """A deliberate SELF restart's tail is SUPPOSED to be complete.

    The handoff note is the work, so the transcript cannot answer the question
    and the gate must not fire.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(
        entry.session_key,
        "restart_interrupted",
        resume_kind="self",
        resume_handoff="finish the deploy verification",
    )

    await runner._prepare_boot_resume_work_check()
    assert entry.session_key not in runner._boot_resume_has_work
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_gate_runs_in_prompt_mode_not_only_auto(tmp_path, monkeypatch):
    """The wasted turn is paid in prompt mode too, so the check must run there.

    ``_prepare_auto_resume_decisions`` returns 0 in prompt mode (it classifies
    tails only for auto), but the finished-work map must still be populated.
    """
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "prompt")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    assert await runner._prepare_auto_resume_decisions() == 0
    assert runner._boot_resume_has_work[entry.session_key] is False
    assert runner._schedule_resume_pending_sessions() == 0
    db.close()


@pytest.mark.asyncio
async def test_gate_fails_open_when_the_transcript_cannot_be_read(
    tmp_path, monkeypatch
):
    """An unreadable transcript must resume exactly as it does today."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    class _Boom:
        async def get_messages(self, *_args, **_kwargs):
            raise RuntimeError("transcript unavailable")

    runner._session_db = _Boom()
    assert await runner._prepare_boot_resume_work_check() == 0
    assert runner._boot_resume_has_work == {}
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_gate_fails_open_when_no_session_db_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    runner._session_db = None
    assert await runner._prepare_boot_resume_work_check() == 0
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_mixed_boot_schedules_only_the_session_with_real_work(
    tmp_path, monkeypatch
):
    """The measured production shape: one interrupted session, N bystanders."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)

    interrupted = runner.session_store.get_or_create_session(_source("interrupted"))
    _seed(db, interrupted, _INTERRUPTED_TAIL)
    assert runner.session_store.mark_resume_pending(
        interrupted.session_key, "shutdown_timeout"
    )

    bystanders = []
    for index in range(4):
        entry = runner.session_store.get_or_create_session(_source(f"bystander-{index}"))
        _seed(db, entry, _COMPLETED_TAIL)
        assert runner.session_store.mark_resume_pending(
            entry.session_key, "shutdown_timeout"
        )
        bystanders.append(entry)

    assert await runner._prepare_boot_resume_work_check() == 5
    assert runner._schedule_resume_pending_sessions() == 1
    assert runner._is_session_running(interrupted.session_key) is True
    for entry in bystanders:
        assert runner._is_session_running(entry.session_key) is False
    await asyncio.gather(*runner._background_tasks)
    db.close()
