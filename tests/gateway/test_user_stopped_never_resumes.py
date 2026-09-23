"""A turn the user ended with ``/stop`` must NEVER be auto-resumed on boot.

RULING (2026-09-21): an interrupted turn ALWAYS auto-resumes after a gateway
restart — except one ended with ``/stop``.

The defect this locks: ``has_resumable_work`` judges only the persisted
transcript TAIL. A ``/stop``ped turn whose transcript ends mid-tool-call is
byte-identical to one a restart amputated, so the gate re-prompted it on the
next boot (the 2026-09-20 incident: "four fresh sessions re-prompted on one
marker-less boot, one of them ``/stop``ped"). The fix is an explicit durable
``user_stopped`` marker that the tail cannot carry, superseded by the user's
next real message rather than by a clock.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import gateway.run as gateway_run
import hermes_state
from gateway.auto_resume import user_stop_blocks_resume
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


# --------------------------------------------------------------------------
# user_stop_blocks_resume: the pure classifier
# --------------------------------------------------------------------------


def test_stop_blocks_when_no_user_message_followed():
    rows = [
        {"id": 1, "role": "user", "content": "go"},
        {"id": 2, "role": "assistant", "content": "", "finish_reason": "tool_calls"},
    ]
    assert user_stop_blocks_resume(2, rows) is True


def test_stop_is_superseded_by_a_later_user_message():
    rows = [
        {"id": 1, "role": "user", "content": "go"},
        {"id": 2, "role": "assistant", "content": "", "finish_reason": "tool_calls"},
        {"id": 3, "role": "user", "content": "actually, continue"},
    ]
    assert user_stop_blocks_resume(2, rows) is False


def test_empty_user_row_does_not_supersede_the_stop():
    # The boot auto-resume path persists an EMPTY synthetic user row. That is
    # the harness talking, not the user, and must not clear a stop.
    rows = [
        {"id": 1, "role": "user", "content": "go"},
        {"id": 2, "role": "assistant", "content": "", "finish_reason": "tool_calls"},
        {"id": 3, "role": "user", "content": ""},
        {"id": 4, "role": "user", "content": "   "},
    ]
    assert user_stop_blocks_resume(2, rows) is True


def test_unknown_stop_rowid_fails_closed():
    # Opposite of every other gate in auto_resume.py, and deliberate: a stop
    # must never be downgraded into a resume by a failed rowid lookup.
    assert user_stop_blocks_resume(None, [{"id": 9, "role": "user", "content": "hi"}]) is True


def test_non_user_rows_after_the_stop_do_not_supersede_it():
    rows = [
        {"id": 5, "role": "assistant", "content": "partial"},
        {"id": 6, "role": "tool", "content": "{}", "tool_call_id": "c1"},
        {"id": 7, "role": "session_meta", "content": ""},
    ]
    assert user_stop_blocks_resume(4, rows) is True


# --------------------------------------------------------------------------
# Integration: real SessionStore + real SessionDB + real scheduler
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

    async def _scheduled_resume_stub(_adapter, _event, _session_key, *_rest):
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


# The exact ambiguous shape: a turn cut mid-tool-call. Identical bytes whether
# the gateway was killed or the user typed /stop — which is the whole defect.
_MID_TOOL_TAIL = [
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
async def test_interrupted_mid_tool_turn_still_resumes(tmp_path, monkeypatch, caplog):
    """Negative control: without a stop marker, nothing changes."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    assert await runner._prepare_boot_resume_work_check() == 1
    assert runner._boot_resume_has_work[entry.session_key] is True

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 1
    assert any("PHASE=boot_resume_scheduled" in m for m in caplog.messages)
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_stopped_mid_tool_turn_is_never_resumed(tmp_path, monkeypatch, caplog):
    """THE defect: same transcript bytes, but the user typed /stop."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    # /stop lands: durable marker written, hedge retired.
    assert await runner._mark_user_stopped(entry.session_key) is True
    marked = runner.session_store._entries[entry.session_key]
    assert marked.user_stopped_at is not None
    assert marked.user_stopped_message_id == db.get_messages(entry.session_id)[-1]["id"]
    assert marked.resume_pending is False

    # A post-stop crash re-marks the hedge wholesale — and must be refused.
    assert runner.session_store.suspend_recently_active(max_age_seconds=3600) == 0
    assert (
        runner.session_store.mark_resume_pending(entry.session_key, "restart_interrupted")
        is False
    )

    # Force the hedge on anyway (belt and braces: prove the boot gate alone
    # denies it, not just the mark-path guard). Set the reason too — a bare
    # resume_pending with no reason is rejected by a later, unrelated gate.
    _forced = runner.session_store._entries[entry.session_key]
    _forced.resume_pending = True
    _forced.resume_reason = "shutdown_timeout"

    assert await runner._prepare_boot_resume_work_check() == 1
    assert runner._boot_resume_has_work[entry.session_key] is False

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 0
    assert any("cause=user_stopped" in m for m in caplog.messages)
    assert not any("PHASE=boot_resume_scheduled" in m for m in caplog.messages)
    assert runner._background_tasks == set()
    # The transcript and session_id are untouched — the user can continue.
    assert runner.session_store._entries[entry.session_key].session_id == entry.session_id
    assert len(db.get_messages(entry.session_id)) == len(_MID_TOOL_TAIL)
    db.close()


@pytest.mark.asyncio
async def test_stop_then_new_user_message_then_interrupted_resumes(
    tmp_path, monkeypatch, caplog
):
    """The marker is superseded by the user speaking again, not by a clock."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)
    assert await runner._mark_user_stopped(entry.session_key) is True

    # The user comes back and the new turn is interrupted mid-tool-call again.
    _seed_rows = [
        {"role": "user", "content": "ok, keep going"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ],
            "finish_reason": "tool_calls",
        },
    ]
    for row in _seed_rows:
        db.append_message(
            entry.session_id,
            row["role"],
            row.get("content"),
            tool_calls=row.get("tool_calls"),
            finish_reason=row.get("finish_reason"),
        )
    # The clear that the inbound path performs; the rowid supersession below
    # is what actually decides, so assert the gate with the marker still set.
    assert runner.session_store.mark_resume_pending(
        entry.session_key, "shutdown_timeout"
    ) is False  # guard still refuses while the marker stands
    _forced = runner.session_store._entries[entry.session_key]
    _forced.resume_pending = True
    _forced.resume_reason = "shutdown_timeout"
    _forced.last_resume_marked_at = None

    assert await runner._prepare_boot_resume_work_check() == 1
    assert runner._boot_resume_has_work[entry.session_key] is True

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 1
    assert any("PHASE=boot_resume_scheduled" in m for m in caplog.messages)
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_clear_user_stopped_restores_normal_marking(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)
    assert await runner._mark_user_stopped(entry.session_key) is True
    assert (
        runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")
        is False
    )
    assert runner.session_store.clear_user_stopped(entry.session_key) is True
    assert runner.session_store._entries[entry.session_key].user_stopped_at is None
    assert (
        runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")
        is True
    )
    db.close()


class _HungAgent:
    def __init__(self) -> None:
        self.interrupted = False

    def interrupt(self, *_a, **_k) -> bool:
        self.interrupted = True
        return True


@pytest.mark.parametrize(
    "invalidation_reason",
    [
        "stop_command",
        "stop_command_pending",
        "stop_command_handler",
        "stop_command_thread_sibling",
    ],
)
@pytest.mark.asyncio
async def test_every_stop_variant_persists_the_marker(
    tmp_path, monkeypatch, invalidation_reason
):
    """The marker is owed by the CLASS of stop reasons, not one call site.

    ``/stop`` reaches ``_interrupt_and_clear_session`` under four distinct
    invalidation reasons (the busy-path intercept, the pending-sentinel
    branch, the normal handler, and the thread-sibling fan-out). Each one is
    a user ending the turn on purpose, so each must leave the marker; a guard
    keyed to a single literal would silently miss three of them.
    """
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)

    await runner._interrupt_and_clear_session(
        entry.session_key,
        _source(),
        interrupt_reason=gateway_run._INTERRUPT_REASON_STOP,
        invalidation_reason=invalidation_reason,
    )

    marked = runner.session_store._entries[entry.session_key]
    assert marked.user_stopped_at is not None, invalidation_reason
    assert marked.user_stopped_message_id == db.get_messages(entry.session_id)[-1]["id"]
    assert marked.resume_pending is False
    db.close()


@pytest.mark.asyncio
async def test_new_command_does_not_persist_the_marker(tmp_path, monkeypatch):
    """Negative control on the same seam: ``/new`` resets outright.

    ``/new`` shares ``_interrupt_and_clear_session`` with ``/stop``. It is not
    a stop, and marking it would wrongly suppress the auto-resume of the FRESH
    turn the user starts next.
    """
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)

    await runner._interrupt_and_clear_session(
        entry.session_key,
        _source(),
        interrupt_reason=gateway_run._INTERRUPT_REASON_RESET,
        invalidation_reason="new_command",
    )

    assert runner.session_store._entries[entry.session_key].user_stopped_at is None
    db.close()


@pytest.mark.asyncio
async def test_real_stop_command_handler_marks_then_boot_refuses(tmp_path, monkeypatch):
    """The WIRING, end to end, driven by the actual ``/stop`` handler.

    Every other test in this file calls ``_mark_user_stopped`` (or
    ``_interrupt_and_clear_session``) itself, which proves the mechanism but
    assumes the wiring. Deleting the marker-persisting branch from
    ``_interrupt_and_clear_session`` left all of them green — the fix was
    inert and nothing said so. This test enters at
    ``_handle_stop_command`` with a real running agent, so the marker is a
    CONSEQUENCE of a user typing ``/stop``, then proves a fresh boot reading
    the reloaded-from-disk store refuses to resume.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _MID_TOOL_TAIL)
    last_id = db.get_messages(entry.session_id)[-1]["id"]
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    # A hung agent is present, so /stop takes the interrupt-and-clear branch.
    runner._running_agents[entry.session_key] = _HungAgent()
    await runner._handle_stop_command(
        MessageEvent(text="/stop", source=_source(), user_id="u1")
    )

    marked = runner.session_store._entries[entry.session_key]
    assert marked.user_stopped_at is not None
    assert marked.user_stopped_message_id == last_id
    assert marked.resume_pending is False

    # Reload from disk — what a SIGKILL plus a reboot actually does.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    revived = reloaded.get_or_create_session(_source())
    assert revived.user_stopped_at is not None
    assert revived.user_stopped_message_id == last_id

    reloaded_db = reloaded._db
    assert isinstance(reloaded_db, SessionDB)
    runner2, adapter2 = make_restart_runner()
    runner2.config = runner.config
    runner2.session_store = reloaded
    runner2._session_db = AsyncSessionDB(reloaded_db)
    runner2.adapters = {Platform.TELEGRAM: adapter2}
    monkeypatch.setattr(runner2, "_run_startup_resume_event", _noop_resume_event)
    # Force the hedge back on, as the boot-time wholesale re-mark would.
    forced = reloaded._entries[revived.session_key]
    forced.resume_pending = True
    forced.resume_reason = "shutdown_timeout"
    forced.last_resume_marked_at = None

    assert await runner2._prepare_boot_resume_work_check() == 1
    assert runner2._boot_resume_has_work[revived.session_key] is False
    assert runner2._schedule_resume_pending_sessions() == 0
    assert runner2._background_tasks == set()
    db.close()


async def _noop_resume_event(_adapter, _event, _session_key, *_rest):
    return None


def test_stop_rowid_is_exclusive_not_inclusive():
    """The stamped row is the stop's OWN anchor — it cannot supersede itself.

    ``_mark_user_stopped`` stamps the session's NEWEST persisted row. When
    ``/stop`` lands before the assistant row has flushed, that newest row is
    the user's own message — the one that STARTED the turn being stopped. A
    ``>=`` comparison would let that row count as "the user spoke again",
    clearing the marker at the instant it was written and resuming exactly the
    turn the user killed. The boundary must be strict.
    """
    rows = [{"id": 1, "role": "user", "content": "go"}]
    assert user_stop_blocks_resume(1, rows) is True
    # And the NEXT user row, one rowid later, does supersede it.
    rows.append({"id": 2, "role": "user", "content": "actually, continue"})
    assert user_stop_blocks_resume(1, rows) is False


def test_marker_survives_sigkill_written_before_ack(tmp_path, monkeypatch):
    """Durability: the marker is on disk before /stop can acknowledge.

    Simulated by dropping the whole in-memory store and reloading from the
    persisted routing index — the same thing a SIGKILL + reboot does.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    assert store.mark_user_stopped(
        entry.session_key, boot_id="boot-abc", last_message_id=42
    ) is True

    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    revived = reloaded.get_or_create_session(_source())
    assert revived.user_stopped_at is not None
    assert revived.user_stopped_boot_id == "boot-abc"
    assert revived.user_stopped_message_id == 42
    # And the reloaded store still refuses to re-arm it.
    assert reloaded.mark_resume_pending(revived.session_key, "restart_interrupted") is False
