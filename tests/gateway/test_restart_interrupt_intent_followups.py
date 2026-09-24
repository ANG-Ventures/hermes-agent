"""Replay of the 2026-09-23 Apollo restart (pid 49726 -> 7734).

03:59 in-band restart with 16 active work units deferred stop() for the full
1800 s after-turn cap; the deploy lane's ``--busy-policy interrupt`` intent at
04:05 was ignored; 4 queued follow-ups were discarded while draining; the boot
notice read ``planned=False by=-``. Each test below fails on the pre-fix tree.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.fork_ext import restart_followups as rf
from gateway.fork_ext.unclean_restart_notice import (
    INTERRUPT_DRAIN_CAP_MAX_S,
    interrupt_drain_cap,
    read_interrupt_restart_intent,
    read_planned_restart,
    record_in_band_restart,
)
from gateway.platforms.base import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "logs").mkdir()
    return tmp_path


def _write_intent(home: Path, **overrides) -> dict:
    row = {
        "event": "intent",
        "busy_policy": "interrupt",
        "drain_cap_s": 60,
        "target_profile": "default",
        "initiator_profile": "apollo",
        "origin_mode": "external",
        "token": "3f9a7d6c34f7",
        "pid_before": os.getpid(),
        "epoch": round(time.time(), 3),
    }
    row.update(overrides)
    with (home / "logs" / "gateway-restart-ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def _ledger_rows(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-restart-ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- ASK 1: busy_policy=interrupt caps the after-turn wait -------------------


@pytest.mark.asyncio
async def test_interrupt_intent_mid_wait_cuts_1800s_after_turn_wait(home):
    """16 active at request, 13 finish, interrupt intent lands mid-wait,
    3 still running when stop() is entered (they are the drain's interrupt set)."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 1800.0
    keys = [f"agent:main:telegram:dm:{i}" for i in range(16)]
    for key in keys:
        runner._running_agents[key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.sleep(0.2)
    for key in keys[3:]:
        del runner._running_agents[key]
    await asyncio.sleep(0.2)
    runner.stop.assert_not_awaited()  # policy=wait semantics until the intent

    _write_intent(home, drain_cap_s=0.3)
    await asyncio.wait_for(runner._restart_task, timeout=5.0)

    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )
    # Exactly the still-running sessions reach stop(), whose drain-timeout
    # path marks each resume_pending(interrupted=True).
    assert sorted(runner._running_agents) == sorted(keys[:3])


@pytest.mark.asyncio
async def test_interrupt_intent_present_before_request_caps_immediately(home):
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 1800.0
    runner._running_agents["agent:main:telegram:dm:1"] = MagicMock()
    _write_intent(home, drain_cap_s=0)

    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {"busy_policy": "wait"},
        {"busy_policy": ""},
        {"pid_before": 1},
        {"pid_before": None},
        {"epoch": time.time() - 7200},
        {"target_profile": "aegis"},
        {"event": "kickstart"},
    ],
)
def test_non_matching_intent_rows_never_cap(home, overrides):
    _write_intent(home, **overrides)
    assert read_interrupt_restart_intent() is None


def test_newest_matching_intent_wins_and_cap_is_clamped(home):
    _write_intent(home, busy_policy="wait")
    row = _write_intent(home, drain_cap_s=900, token="newest")
    got = read_interrupt_restart_intent()
    assert got is not None and got["token"] == "newest"
    assert interrupt_drain_cap(got) == INTERRUPT_DRAIN_CAP_MAX_S
    assert interrupt_drain_cap({"drain_cap_s": "nan"}) == INTERRUPT_DRAIN_CAP_MAX_S
    assert interrupt_drain_cap({"drain_cap_s": 12}) == 12
    assert row["pid_before"] == os.getpid()


# -- ASK 3 / Apollo: no silent follow-up discard during drain ---------------


def _event(chat: str, text: str) -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=make_restart_source(chat_id=chat)
    )


@pytest.mark.asyncio
async def test_four_draining_followups_are_spooled_and_replayed_on_boot(home):
    old, _ = make_restart_runner()
    old._draining = True
    old._restart_requested = True
    sent = {f"agent:main:telegram:dm:{i}": _event(str(i), f"follow-up {i}") for i in range(4)}
    for key, event in sent.items():
        assert await old._preserve_followup_across_restart(key, event, None) is True
    assert len(list(rf.spool_dir(home).glob("*.json"))) == 4

    new, _ = make_restart_runner()
    new._startup_restore_queue = []
    new._startup_restore_in_progress = True
    assert await new._load_restart_followups() == 4
    replayed = [(e.source.chat_id, e.text) for e in new._startup_restore_queue]
    assert replayed == [(str(i), f"follow-up {i}") for i in range(4)]
    assert len(list(rf.spool_dir(home).glob("*.json"))) == 4
    # Files remain durable until the startup-restore adapter accepts them.


@pytest.mark.asyncio
async def test_transcribed_pending_text_wins_over_raw_event_text(home):
    runner, _ = make_restart_runner()
    runner._restart_requested = True
    event = _event("9", "/tmp/voice.ogg")
    assert await runner._preserve_followup_across_restart("k", event, "transcript") is True
    (record,) = [json.loads(p.read_text()) for p in rf.spool_dir(home).glob("*.json")]
    assert record["text"] == "transcript"


@pytest.mark.asyncio
async def test_voice_transcript_wins_and_audio_is_not_carried_twice(home):
    """Post-turn drain already transcribed the voice note: the spool keeps the
    transcript as text, drops the transcribed audio (no second STT on replay),
    and still carries the event's other identity fields."""
    runner, _ = make_restart_runner()
    runner._restart_requested = True
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=make_restart_source(chat_id="9"),
        media_urls=["/cache/voice.ogg"],
        media_types=["audio/ogg"],
        internal=True,
        metadata={"k": "v"},
    )
    assert await runner._preserve_followup_across_restart("k", event, "the transcript") is True
    (record,) = [json.loads(p.read_text()) for p in rf.spool_dir(home).glob("*.json")]
    assert record["text"] == "the transcript"
    assert record["event"]["media_urls"] == []
    assert record["event"]["media_types"] == []
    assert record["event"]["message_type"] == "text"
    assert record["event"]["internal"] is True
    assert record["event"]["metadata"] == {"k": "v"}


@pytest.mark.asyncio
async def test_media_placeholder_is_not_spooled_as_text(home):
    """A caption-less photo drains as a placeholder string; the carried media
    rebuilds it on replay, so the spool keeps the raw (empty) text + media."""
    from gateway.run import _build_media_placeholder

    runner, _ = make_restart_runner()
    runner._restart_requested = True
    event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=make_restart_source(chat_id="9"),
        media_urls=["/cache/p.jpg"],
        media_types=["image/jpeg"],
    )
    placeholder = _build_media_placeholder(event)
    assert placeholder
    assert await runner._preserve_followup_across_restart("k", event, placeholder) is True
    (record,) = [json.loads(p.read_text()) for p in rf.spool_dir(home).glob("*.json")]
    assert record["text"] == ""
    assert record["event"]["media_urls"] == ["/cache/p.jpg"]
    assert record["event"]["message_type"] == "photo"


@pytest.mark.asyncio
async def test_stop_sweep_spools_follow_ups_parked_on_adapter(home):
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    adapter._pending_messages["agent:main:telegram:dm:7"] = _event("7", "queued during drain")
    assert await runner._spool_adapter_pending_for_restart() == 1
    assert "agent:main:telegram:dm:7" not in adapter._pending_messages
    records, _ = rf.take_followups(home)
    assert [r["text"] for r in records] == ["queued during drain"]


def test_stale_spool_is_not_replayed(home):
    rf.spool_followup("k", "old", make_restart_source().to_dict(), home=home, now=time.time() - 7 * 3600)
    records, stale = rf.take_followups(home)
    assert records == [] and stale == 1


def test_draining_site_preserves_instead_of_discarding():
    """Source contract on the one site inside _run_agent_admitted that used to
    drop the follow-up: the draining branch must hand it to the spool."""
    src = RUN_PY.read_text(encoding="utf-8")
    assert "Discarding pending follow-up" not in src
    head = "if self._draining and (pending_event or pending):"
    start = src.find(head)
    assert start != -1, "draining follow-up branch not found"
    end = src.find("if pending_event or pending:", start + len(head))
    assert end != -1
    assert "await self._preserve_followup_across_restart(" in src[start:end]


# -- ASK 4 / requester logging: in-band restart is attributable -------------


@pytest.mark.asyncio
async def test_in_band_restart_logs_requester_and_reads_planned(home, caplog):
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0.0
    caplog.set_level("WARNING", logger="gateway.run")

    def sigusr1_handler():
        return runner.request_restart(detached=False, via_service=True)

    assert sigusr1_handler() is True
    await asyncio.wait_for(runner._restart_task, timeout=5.0)

    line = next(r.getMessage() for r in caplog.records if "PHASE=restart_requested" in r.getMessage())
    assert "requester=sigusr1_handler@" in line
    rows = [r for r in _ledger_rows(home) if r.get("event") == "in_band"]
    assert len(rows) == 1 and rows[0]["pid_before"] == os.getpid()
    assert rows[0]["initiator_profile"].startswith("self:sigusr1_handler@")

    ended = datetime.now(timezone.utc).isoformat()
    planned = read_planned_restart(ended, home)
    assert planned is not None and planned["event"] == "in_band"


@pytest.mark.asyncio
async def test_named_launch_without_profile_env_honors_intent_and_planned_notice(tmp_path, monkeypatch):
    named = tmp_path / "profiles" / "qa-profile"
    (named / "logs").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(named))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    row = _write_intent(named, target_profile="qa-profile", drain_cap_s=0.2)
    assert read_interrupt_restart_intent() == row
    assert read_planned_restart(datetime.now(timezone.utc).isoformat()) == row

    runner, _ = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 1800
    runner._running_agents["agent:main:telegram:dm:1"] = MagicMock()
    assert runner.request_restart(detached=False, via_service=True)
    await asyncio.wait_for(runner._restart_task, timeout=5)
    runner.stop.assert_awaited_once()
    in_band = [r for r in _ledger_rows(named) if r.get("event") == "in_band"]
    assert len(in_band) == 1 and in_band[0]["target_profile"] == "qa-profile"


def test_named_profile_does_not_accept_default_ledger_row(tmp_path, monkeypatch):
    named = tmp_path / "profiles" / "qa-profile"
    (named / "logs").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(named))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    _write_intent(named, target_profile="default")
    assert read_interrupt_restart_intent() is None
    assert read_planned_restart(datetime.now(timezone.utc).isoformat()) is None
