"""A cancelled commit fence must stop the in-flight summary stream (t_139733d1).

Gateway session hygiene abandons its inline wait after the turn-hold budget and
cancels the commit fence. Before this fix the detached compressor kept running
the summary model to completion (21-22 min on a ~650k-token transcript), only
to be refused at ``begin_commit``. These tests drive a fake streaming aux
provider through the REAL cancel plumbing and count chunks consumed after the
fence is cancelled.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as aux
from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.conversation_compression import CompressionCommitFence, _SummaryCancelSignal


def _chunk(text: str):
    delta = SimpleNamespace(content=text, reasoning_content=None, reasoning=None, tool_calls=None)
    return SimpleNamespace(
        id="c", model="m", choices=[SimpleNamespace(delta=delta, finish_reason=None, index=0)],
        usage=None,
    )


class _EndlessStream:
    """A long summary stream (bounded so a regression FAILS, never hangs)."""

    MAX_CHUNKS = 400  # ~0.8s; a regression drains all of them

    def __init__(self, on_chunk=None, delay: float = 0.002):
        self.yielded = 0
        self.closed = threading.Event()
        self._on_chunk = on_chunk
        self._delay = delay

    def __iter__(self):
        while not self.closed.is_set() and self.yielded < self.MAX_CHUNKS:
            self.yielded += 1
            if self._on_chunk is not None:
                self._on_chunk(self.yielded)
            time.sleep(self._delay)
            yield _chunk("x")

    def close(self):
        self.closed.set()


def test_chat_stream_aborts_on_next_chunk_after_fence_cancel():
    fence = CompressionCommitFence()
    cancelled_at = {}

    def on_chunk(n):
        if n == 5:
            assert fence.try_cancel_before_commit() is True
            cancelled_at["n"] = n

    stream = _EndlessStream(on_chunk)
    signal = _SummaryCancelSignal(threading.Event(), fence)
    with aux.aux_interrupt_protection(cancel_event=signal):
        with pytest.raises(AuxiliaryExplicitCancellation):
            aux._aggregate_chat_stream(stream)
    assert cancelled_at["n"] == 5
    # The chunk yielded in the cancel window is fed and rejected; nothing more.
    assert stream.yielded - cancelled_at["n"] <= 1
    assert stream.closed.is_set(), "cancelled stream must be closed (stops provider generation)"


def test_protected_provider_worker_stops_consuming_after_fence_cancel():
    """Daemon-worker path: the owner unwinds AND the worker stops the stream.

    Previously the owner raised on cancel but the worker kept draining the
    stream until the provider finished — that was the 22-minute burn.
    """
    fence = CompressionCommitFence()
    stream = _EndlessStream()

    def callback(_kwargs):
        return aux._aggregate_chat_stream(stream)

    def cancel_later():
        while stream.yielded < 10:
            time.sleep(0.001)
        fence.try_cancel_before_commit()

    signal = _SummaryCancelSignal(threading.Event(), fence)
    threading.Thread(target=cancel_later, daemon=True).start()
    with aux.aux_interrupt_protection(cancel_event=signal):
        with pytest.raises(AuxiliaryExplicitCancellation):
            aux._run_protected_sync_provider_call(callback, {})
    assert stream.closed.wait(2.0), "provider worker kept the cancelled stream open"
    at_close = stream.yielded
    time.sleep(0.1)
    assert stream.yielded == at_close, "chunks still consumed after cancel"
    assert at_close <= 12


def test_anthropic_stream_event_hook_aborts_after_cancel():
    fence = CompressionCommitFence()
    event = SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="t"))
    signal = _SummaryCancelSignal(None, fence)
    with aux.aux_interrupt_protection(cancel_event=signal):
        aux._on_aux_anthropic_stream_event(event)  # live fence: no raise
        fence.try_cancel_before_commit()
        with pytest.raises(AuxiliaryExplicitCancellation):
            aux._on_aux_anthropic_stream_event(event)


def test_signal_without_cancellation_is_not_set():
    hard = threading.Event()
    fence = CompressionCommitFence()
    signal = _SummaryCancelSignal(hard, fence)
    assert not signal.is_set()
    hard.set()
    assert signal.is_set()
    assert _SummaryCancelSignal(None, CompressionCommitFence()).is_set() is False


def test_compress_context_fence_cancel_mid_summary_aborts_stream_and_releases_lock(caplog):
    """End to end through AIAgent._compress_context with a real SessionDB.

    The engine's compress() consumes a streamed summary through the real
    protected aux provider seam; the host cancels the fence mid-stream (what
    gateway hygiene does on turn-hold expiry). The summary must stop within a
    chunk, the transcript must come back unchanged, the durable lock must be
    free, and telemetry must say commit_fence_cancelled.
    """
    from hermes_state import SessionDB

    fence = CompressionCommitFence()
    stream = _EndlessStream()

    def compress(_messages, **_kwargs):
        def cancel_later():
            while stream.yielded < 10:
                time.sleep(0.001)
            fence.try_cancel_before_commit()

        threading.Thread(target=cancel_later, daemon=True).start()
        aux._run_protected_sync_provider_call(
            lambda _kw: aux._aggregate_chat_stream(stream), {}
        )
        return [{"role": "user", "content": "should-not-commit"}]

    with tempfile.TemporaryDirectory() as td:
        db = SessionDB(db_path=Path(td) / "state.db")
        session_id = "T139733D1_FENCE_STREAM"
        db.create_session(session_id, source="cli")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
            )
        compressor = MagicMock()
        compressor.compress.side_effect = compress
        compressor._last_summary_error = None
        compressor._last_compress_aborted = False
        compressor._last_aux_model_failure_model = None
        compressor._last_aux_model_failure_error = None
        agent.context_compressor = compressor
        agent._cached_system_prompt = "sys"

        messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        snapshot = [dict(m) for m in messages]
        with caplog.at_level("INFO"):
            returned, _sp = agent._compress_context(
                messages, "sys", approx_tokens=120_000, commit_fence=fence
            )

        assert compressor.compress.called
        assert returned == snapshot
        assert stream.closed.wait(2.0), "summary stream still open after fence cancel"
        at_close = stream.yielded
        time.sleep(0.1)
        assert stream.yielded == at_close
        assert at_close <= 12, f"{at_close} chunks consumed; cancel landed at 10"
        assert db.get_compression_lock_holder(session_id) is None
        assert "commit fence cancelled by host" in caplog.text
        assert '"failure_class":"commit_fence_cancelled"' in caplog.text
