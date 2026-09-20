"""Reaching the boot-resume cap must not destroy the session's recovery context.

Round-3 review finding on PR #761. The cap's skip branch used to call
``clear_resume_pending`` on every *accountable* cap hit, and
``SessionStore._clear_resume_pending_entry`` does not clear one flag — it wipes
the whole recovery record (``resume_reason``, ``resume_kind``,
``resume_handoff``, ``resume_request_id``, ``last_resume_marked_at``).

That mattered because the inbound path gates the user's recovery context on
that same marker: ``_is_resume_pending`` requires ``resume_pending``, and it is
the only thing that supplies the reason-aware resume prompt and the
``build_resume_recovery_note`` safety net. So after a cap hit the user's next
real message arrived on a session that — by construction — still had unfinished
work (the work check voted RESUME on every boot that spent the budget), with no
recovery note and no handoff.

The clear also bought nothing. Both jobs it could have done are already done
elsewhere: ``session_resume_verdict`` denies the unattended replay once
``count >= max`` regardless of the marker, and ``clear_stale_resume_pending``
reaps markers that outlive their usefulness.

The card body originally specified "after the cap, mark resume-pending →
cleared". That instruction was **withdrawn** by the operator once the harm was
measured; the cap counter is what bounds unattended replay, and the marker is
the user's recovery context. This file is the pin so nobody "fixes" it back.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource
from tests.gateway.test_boot_resume_attempt_cap import (
    _INTERRUPTED_TAIL,
    _boot,
    _runner,
    _seed,
    _source,
)

_CAP = 3
_HANDOFF = "finish the discord channel takeover"


def _remark_with_handoff(runner, entry) -> None:
    """Re-mark the way the restart watchdog does for the incident's own shape."""
    assert runner.session_store.mark_resume_pending(
        entry.session_key,
        "restart_interrupted",
        resume_kind="self",
        resume_handoff=_HANDOFF,
    )


def _marker(runner, entry):
    refreshed = runner.session_store._entries[entry.session_key]
    return (
        refreshed.resume_pending,
        refreshed.resume_reason,
        refreshed.resume_kind,
        refreshed.resume_handoff,
    )


@pytest.mark.asyncio
async def test_marker_and_handoff_survive_every_boot_past_the_cap(
    tmp_path, monkeypatch, caplog
):
    """Ten boots: three replays, and the recovery record intact on all ten.

    The bound is the point of the cap; the marker is not part of the bound.
    """
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", str(_CAP))
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    scheduled: list[int] = []
    markers = []
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        for _ in range(10):
            _remark_with_handoff(runner, entry)
            await runner._prepare_auto_resume_decisions()
            scheduled.append(_boot(runner))
            markers.append(_marker(runner, entry))

    assert scheduled == [1] * _CAP + [0] * (10 - _CAP), scheduled
    # The recovery record is byte-identical on every boot, capped or not.
    assert markers == [(True, "restart_interrupted", "self", _HANDOFF)] * 10, markers
    # ...and the cap did fire, so this is not a vacuous pass.
    assert any("cause=attempt_cap" in r.getMessage() for r in caplog.records)
    db.close()


@pytest.mark.asyncio
async def test_capped_session_is_still_resume_pending_for_the_inbound_path(
    tmp_path, monkeypatch
):
    """The gate the recovery note actually reads must still say yes after the cap.

    ``_is_resume_pending`` in ``_run_agent`` is ``resume_pending AND fresh``.
    The freshness half is re-stamped by ``mark_resume_pending`` on the boot
    that hit the cap, so a capped session satisfies the whole predicate — which
    is what makes the next inbound turn carry the reason-aware prompt.
    """
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "1")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    _remark_with_handoff(runner, entry)
    await runner._prepare_auto_resume_decisions()
    assert _boot(runner) == 1

    _remark_with_handoff(runner, entry)
    await runner._prepare_auto_resume_decisions()
    assert _boot(runner) == 0, "budget spent"

    refreshed = runner.session_store._entries[entry.session_key]
    from gateway.run import _auto_continue_freshness_window, _is_fresh_gateway_interruption

    assert refreshed.resume_pending is True
    assert _is_fresh_gateway_interruption(
        refreshed.last_resume_marked_at,
        window_secs=_auto_continue_freshness_window(),
    ) is True
    db.close()


# --------------------------------------------------------------------------
# The consequence, through the real inbound turn: the next user message must
# still arrive wrapped in the recovery note.
# --------------------------------------------------------------------------


class _CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="m-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _echo_agent_module(seen: list):
    class _EchoAgent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **_kwargs):
            seen.append(message)
            return {"final_response": "ok", "messages": [], "api_calls": 1}

    module = types.ModuleType("run_agent")
    module.AIAgent = _EchoAgent
    return module


@pytest.mark.asyncio
async def test_next_inbound_turn_after_the_cap_still_carries_the_recovery_note(
    tmp_path, monkeypatch
):
    """The user-visible consequence, driven through the real ``_run_agent``.

    Not a mirror of the injection logic — the agent is faked, the gateway path
    is real, and the assertion is on the exact text the model would receive.
    """
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "1")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    adapter = _CaptureAdapter()
    adapter.set_message_handler(lambda *_a, **_k: None)
    runner.adapters = {Platform.TELEGRAM: adapter}

    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    # Spend the budget, then hit the cap.
    for expected in (1, 0):
        _remark_with_handoff(runner, entry)
        await runner._prepare_auto_resume_decisions()
        assert _boot(runner) == expected
    assert runner.session_store._entries[entry.session_key].resume_pending is True

    seen: list[str] = []
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setitem(sys.modules, "run_agent", _echo_agent_module(seen))
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)

    result = await runner._run_agent(
        message="are you still on the discord task?",
        context_prompt="",
        history=[],
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="u1", chat_type="dm", user_id="u1"
        ),
        session_id=entry.session_id,
        session_key=entry.session_key,
    )

    assert result["final_response"] == "ok"
    assert seen, "the agent never ran"
    delivered = seen[0]
    # The reason-aware recovery note — the thing the clear used to destroy.
    assert "[System note:" in delivered
    assert "interrupt" in delivered.lower()
    # ...wrapped around the user's actual words, not replacing them.
    assert "are you still on the discord task?" in delivered
    db.close()


# --------------------------------------------------------------------------
# Finding B: reset ordering at the forward-progress site.
# --------------------------------------------------------------------------


class _SimulatedCrash(BaseException):
    """Escapes ``except Exception`` the way a real process death would."""


@pytest.mark.asyncio
async def test_a_crash_between_the_two_resets_never_grants_an_extra_budget(
    tmp_path, monkeypatch
):
    """``_apply_post_turn_resume_gate`` clears the marker BEFORE the counter.

    The forward-progress branch resets two pieces of state: the recovery marker
    and the per-session cap counter. If the process dies between them, only one
    of the two partial states is safe:

    * counter first (the shipped order) → marker still set, counter zeroed =
      a whole fresh budget of unattended replays for a session that has not
      proven anything. This is the incident's shape, re-armed.
    * marker first → marker cleared, counter left = at most one skipped
      resume, the same fail-towards-fewer-replays direction the rest of this
      code already takes.

    Injected at the counter reset so the crash lands exactly in the window.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "2")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    store = runner._get_auto_resume_attempt_store()

    for _ in range(2):
        _remark_with_handoff(runner, entry)
        await runner._prepare_auto_resume_decisions()
        assert _boot(runner) == 1
    assert store.session_cap_reached(entry.session_key, 2) is True

    def _die(*_args, **_kwargs):
        raise _SimulatedCrash("process died mid-reset")

    monkeypatch.setattr(store, "clear_session_attempts", _die)
    with pytest.raises(_SimulatedCrash):
        runner._apply_post_turn_resume_gate(entry.session_key)

    # The safe partial state: marker gone, budget NOT refunded.
    assert runner.session_store._entries[entry.session_key].resume_pending is False
    assert store.session_attempt_count(entry.session_key) == 2
    assert store.session_cap_reached(entry.session_key, 2) is True

    # ...and the session is not resumed again on the next boot, which is the
    # consequence the ordering exists to protect.
    _remark_with_handoff(runner, entry)
    await runner._prepare_auto_resume_decisions()
    assert _boot(runner) == 0
    db.close()
