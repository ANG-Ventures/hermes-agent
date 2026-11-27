"""Typed /model timing through the real adapter delivery path."""
import asyncio
import logging
import re
from unittest.mock import AsyncMock

import pytest
from gateway import model_cmd_timing as mct
from tests.gateway.test_model_command_context_offload import _event, _runner_with_store
from tests.gateway.test_baseexception_turn_notify import _ProbeAdapter
from gateway.session import build_session_key


def _phase_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("PHASE=model_cmd ")]


def _fields(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    r = _runner_with_store(tmp_path, monkeypatch)
    async def slow_override(*args, **kwargs):
        await asyncio.sleep(0.25)
    r._async_session_store.set_model_override = AsyncMock(side_effect=slow_override)
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["ok", "error", "cancelled"])
async def test_public_delivery_clock_includes_blocked_send(runner, caplog, monkeypatch, result):
    adapter = _ProbeAdapter()
    entered, release = asyncio.Event(), asyncio.Event()
    clock = [100.0]
    # Only the timing module's clock is virtual; adapter and event loop remain real.
    from types import SimpleNamespace
    monkeypatch.setattr(mct, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    real_send = adapter.send

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        if result == "error":
            raise RuntimeError("send fixture failed")
        return await real_send(*args, **kwargs)

    adapter.send = send
    adapter._notify_turn_error = AsyncMock(return_value=None)
    adapter.set_message_handler(runner._handle_model_command)
    event = _event("/model gpt-5.5")
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        task = asyncio.create_task(adapter._process_message_background(event, build_session_key(event.source)))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert not _phase_lines(caplog), "clock ended before the adapter send"
            clock[0] += 30
            if result == "cancelled":
                task.cancel()
            release.set()
            if result == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    lines = _phase_lines(caplog)
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert float(fields["confirm_s"]) >= 30
    assert float(fields["total_s"]) >= 30
    assert fields["outcome"] == result
    print(lines[0])


@pytest.mark.asyncio
async def test_handler_error_is_logged_and_propagates(runner, caplog, monkeypatch):
    def boom(*args):
        raise RuntimeError("normalize failed")
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", boom, raising=False)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        with pytest.raises(RuntimeError, match="normalize failed"):
            await runner._handle_model_command(_event("/model gpt-5.5"))
    assert _fields(_phase_lines(caplog)[0])["outcome"] == "error"


@pytest.mark.asyncio
async def test_typed_switch_logs_one_line_with_every_stage(runner, caplog):
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        reply = await runner._handle_model_command(_event("/model gpt-5.5"))
    assert reply and "gpt-5.5" in reply
    lines = _phase_lines(caplog)
    assert len(lines) == 1, lines
    f = _fields(lines[0])
    assert f["outcome"] == "ok" and f["session"] != "-", f
    for name in mct.PHASES + mct.DURABLE_SPANS + ("record", "other", "total"):
        assert f"{name}_s" in f, (name, lines[0])
    for reached in ("lock_wait", "config", "normalize", "prepare", "resolve", "enrich", "guard",
                    "fallback_clear", "record_override", "announce", "record_durable", "confirm"):
        assert f[f"{reached}_s"] != "-", (reached, lines[0])
    # Not reached in this harness: no picker listing, no cached agent, no session DB.
    assert f["listing_s"] == f["agent_switch_s"] == f["record_update_model_s"] == "-", lines[0]
    # The slow write-through is named twice: as its nested span and inside its phase.
    assert float(f["durable_set_override_s"]) >= 0.25, lines[0]
    assert float(f["record_durable_s"]) >= float(f["durable_set_override_s"]), lines[0]
    assert float(f["record_s"]) >= float(f["record_durable_s"]), lines[0]
    phases = sum(float(f[f"{n}_s"]) for n in mct.PHASES if f[f"{n}_s"] != "-")
    assert abs(phases + float(f["other_s"]) - float(f["total_s"])) < 0.01, lines[0]
    print("model_cmd line:", lines[0])


@pytest.mark.asyncio
async def test_lock_wait_measures_this_sessions_switch_lock(runner, caplog):
    """The switch lock is per session (t_2ab07928): lock_wait_s is the wait for THIS session's
    previous switch or save; another session's held lock is not waited for or counted."""
    event = _event("/model gpt-5.5")
    other = runner._model_switch_lock("another-session")
    await other.acquire()  # a /model in another session holds its own lock throughout
    lock = runner._model_switch_lock(runner._model_switch_lock_key(event.source))
    await lock.acquire()  # this session's previous /model still saving

    async def _release_later():
        await asyncio.sleep(0.3)
        lock.release()

    try:
        with caplog.at_level(logging.INFO, logger="gateway.run"):
            await asyncio.gather(_release_later(), runner._handle_model_command(event))
    finally:
        other.release()
    f = _fields(_phase_lines(caplog)[0])
    assert float(f["lock_wait_s"]) >= 0.25 and float(f["lock_wait_s"]) < 1.0, f
    assert float(f["lock_wait_s"]) < float(f["total_s"]), f


@pytest.mark.asyncio
async def test_handler_error_is_logged_as_error_and_propagates(runner, caplog, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("normalize failed")
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", _boom, raising=False)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        with pytest.raises(RuntimeError, match="normalize failed"):
            await runner._handle_model_command(_event("/model gpt-5.5"))
    f = _fields(_phase_lines(caplog)[0])
    assert f["outcome"] == "error" and f["normalize_s"] != "-" and f["resolve_s"] == "-", f
    assert not runner._model_switch_lock().locked(), "the switch lock must be released on error"


@pytest.mark.asyncio
async def test_broken_telemetry_never_changes_the_reply(runner, monkeypatch):
    def _broken(*_a, **_k):
        raise RuntimeError("telemetry broke")
    monkeypatch.setattr(mct.ModelCmdTiming, "line", _broken)
    monkeypatch.setattr(mct.ModelCmdTiming, "enter", _broken)
    reply = await runner._handle_model_command(_event("/model gpt-5.5"))
    assert reply and "gpt-5.5" in reply


def test_phase_and_span_outside_a_command_are_noops_and_finish_logs_once(caplog):
    mct._current.set(None)
    mct.phase("resolve")  # no timer: nothing to do, nothing raised
    with mct.span("durable_config"):
        pass
    timing = mct.begin()
    mct.phase("resolve")
    with pytest.raises(ValueError):
        with mct.span("durable_config"):
            raise ValueError("body exceptions propagate")
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        mct.finish(timing)
        mct.finish(timing)
    lines = _phase_lines(caplog)
    assert len(lines) == 1 and _fields(lines[0])["durable_config_s"] != "-", lines

def test_span_clock_failure_preserves_body(monkeypatch):
    def boom():
        raise RuntimeError("clock failed")
    monkeypatch.setattr(mct.time, "monotonic", boom)
    with mct.span("durable_config"):
        pass
    with pytest.raises(ValueError, match="body"):
        with mct.span("durable_config"):
            raise ValueError("body")
