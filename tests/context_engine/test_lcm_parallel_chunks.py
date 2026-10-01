"""compression.parallel_chunks: map-reduce for large LCM leaf summaries (t_2f8f12a7).

Covers the three contracts the card names:
  * chunk boundaries never split a tool-call / tool-result pair;
  * a chunk failure walks the same rescue ladder as a serial summary;
  * a cancelled commit fence aborts every in-flight chunk stream (extends the
    #1492 fence tests in tests/agent/test_compression_fence_cancels_aux_stream.py).
"""

from __future__ import annotations

import random
import threading
import time
from types import SimpleNamespace

import pytest

import agent.auxiliary_client as aux
from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.conversation_compression import CompressionCommitFence, _SummaryCancelSignal
from plugins.context_engine.lcm import config as lcm_config
from plugins.context_engine.lcm import engine as lcm_engine
from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from plugins.context_engine.lcm.parallel_summary import (
    run_map_reduce,
    safe_boundaries,
    split_pair_safe,
)
from plugins.context_engine.lcm.tokens import count_message_tokens, count_messages_tokens

REDUCE_MARK = "summarized in consecutive parts"


# ── transcript builders ─────────────────────────────────────────────────


def _text(words: int, tag: str) -> str:
    return " ".join(f"{tag}{i}" for i in range(words))


def _transcript(turns: int, *, seed: int = 0, words: int = 120):
    """user / assistant(+tool_calls) / tool... turns with random pair shapes."""
    rng = random.Random(seed)
    out = []
    call = 0
    for t in range(turns):
        out.append({"role": "user", "content": _text(words, f"u{t}_")})
        n_calls = rng.choice([0, 0, 1, 2, 3])
        calls = []
        for _ in range(n_calls):
            call += 1
            calls.append({
                "id": f"call_{call}",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
            })
        msg = {"role": "assistant", "content": _text(words // 2, f"a{t}_")}
        if calls:
            msg["tool_calls"] = calls
        out.append(msg)
        for c in calls:
            out.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": _text(rng.randint(20, words * 2), f"r{c['id']}_"),
            })
        if calls:
            out.append({"role": "assistant", "content": _text(words // 3, f"f{t}_")})
    return out


def _pairs_split(chunks):
    """Tool-call ids whose call and result landed in different chunks."""
    where_call, where_result = {}, {}
    for idx, chunk in enumerate(chunks):
        for m in chunk:
            for tc in m.get("tool_calls") or []:
                where_call[tc["id"]] = idx
            if m.get("role") == "tool":
                where_result[m["tool_call_id"]] = idx
    return {cid for cid in where_call if where_result.get(cid) != where_call[cid]}


# ── splitting ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(25))
def test_split_never_separates_tool_call_from_its_result(seed):
    messages = _transcript(40, seed=seed)
    total = count_messages_tokens(messages)
    target = max(200, total // random.Random(seed).randint(2, 9))
    chunks = split_pair_safe(messages, target)

    assert [m for c in chunks for m in c] == messages, "chunks must re-concatenate to the input"
    assert _pairs_split(chunks) == set()
    for chunk in chunks[1:]:
        assert chunk[0]["role"] != "tool", "a chunk must never start with a tool result"
    assert len(chunks) >= 2


def test_split_grows_past_target_rather_than_cut_an_open_pair():
    # One assistant turn with three calls whose results alone exceed the target.
    calls = [{"id": f"c{i}", "type": "function", "function": {"name": "x", "arguments": "{}"}}
             for i in range(3)]
    messages = [
        {"role": "user", "content": _text(50, "u")},
        {"role": "assistant", "content": "go", "tool_calls": calls},
        *[{"role": "tool", "tool_call_id": f"c{i}", "content": _text(400, f"r{i}")} for i in range(3)],
        {"role": "user", "content": _text(400, "late")},
    ]
    one_result = count_message_tokens(messages[2])
    chunks = split_pair_safe(messages, one_result)
    assert _pairs_split(chunks) == set()
    assert chunks[0][-1] is messages[4], "the open pair must stay whole in the first chunk"


def test_safe_boundaries_marks_interior_of_open_pair_unsafe():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "r"},
        {"role": "user", "content": "q2"},
    ]
    assert safe_boundaries(messages) == [True, True, False, True, True]


def test_orphan_tool_call_does_not_freeze_later_boundaries():
    """A call whose result never arrives must not make the rest unsplittable."""
    messages = [
        {"role": "user", "content": _text(200, "a")},
        {"role": "assistant", "content": "x", "tool_calls": [{"id": "lost", "function": {}}]},
        {"role": "user", "content": _text(200, "b")},
        {"role": "assistant", "content": _text(200, "c")},
        {"role": "user", "content": _text(200, "d")},
    ]
    ok = safe_boundaries(messages)
    assert ok[2] and ok[3] and ok[4]
    assert len(split_pair_safe(messages, count_message_tokens(messages[0]) * 2)) >= 2


def test_reused_call_id_whose_result_came_earlier_does_not_freeze_boundaries():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "dup", "function": {}}]},
        {"role": "tool", "tool_call_id": "dup", "content": "r"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "dup", "function": {}}]},
        {"role": "user", "content": "q2"},
        {"role": "user", "content": "q3"},
    ]
    assert safe_boundaries(messages)[4:] == [True, True, True]


def test_small_trailing_chunk_merges_into_previous():
    messages = [{"role": "user", "content": _text(300, f"m{i}_")} for i in range(4)]
    per = count_message_tokens(messages[0])
    messages.append({"role": "user", "content": "tiny"})
    chunks = split_pair_safe(messages, per * 2)
    assert len(chunks) == 2
    assert chunks[-1][-1]["content"] == "tiny"


# ── config ───────────────────────────────────────────────────────────────


def test_parallel_chunks_default_off_and_config_yaml_parse(monkeypatch):
    assert LCMConfig().parallel_chunks_enabled is False
    monkeypatch.setattr(lcm_config, "_hermes_config_yaml", lambda: {
        "compression": {"parallel_chunks": {"enabled": True, "chunk_tokens": 90000, "max_concurrency": 3}}
    })
    assert lcm_config._hermes_parallel_chunks(False, 120000, 4) == (True, 90000, 3)
    # Malformed values keep defaults; a truthy string never enables it.
    monkeypatch.setattr(lcm_config, "_hermes_config_yaml", lambda: {
        "compression": {"parallel_chunks": {"enabled": "yes", "chunk_tokens": 0, "max_concurrency": "x"}}
    })
    assert lcm_config._hermes_parallel_chunks(False, 120000, 4) == (False, 120000, 4)


def test_from_env_reads_compression_parallel_chunks(monkeypatch):
    monkeypatch.setattr(lcm_config, "_hermes_config_yaml", lambda: {
        "compression": {"parallel_chunks": {"enabled": True, "chunk_tokens": 100000}}
    })
    cfg = LCMConfig.from_env()
    assert (cfg.parallel_chunks_enabled, cfg.parallel_chunk_tokens, cfg.parallel_max_concurrency) == (
        True, 100000, 4,
    )


# ── engine map-reduce ────────────────────────────────────────────────────


def _engine(tmp_path, **overrides):
    config = LCMConfig(
        database_path=str(tmp_path / "lcm.db"),
        fresh_tail_count=1,
        leaf_chunk_tokens=1,
        context_threshold=0.01,
        fresh_tail_token_budget_enabled=False,
        **overrides,
    )
    engine = LCMEngine(config=config, hermes_home=str(tmp_path))
    engine.update_model("unit-test-model", 1_000_000, provider="unit-test")
    engine.on_session_start(
        "session-1", hermes_home=str(tmp_path), model="unit-test-model",
        provider="unit-test", context_length=1_000_000, platform="pytest",
    )
    return engine


class _Recorder:
    def __init__(self, chunk_fn=None, reduce_fn=None):
        self.lock = threading.Lock()
        self.chunk_calls = []
        self.reduce_calls = []
        self.chunk_fn = chunk_fn
        self.reduce_fn = reduce_fn

    def __call__(self, *, text, source_tokens, token_budget, **kwargs):
        if REDUCE_MARK in text:
            with self.lock:
                self.reduce_calls.append((text, source_tokens, token_budget))
            if self.reduce_fn:
                return self.reduce_fn(text)
            return "REDUCED", 1
        with self.lock:
            self.chunk_calls.append((text, source_tokens, token_budget))
        if self.chunk_fn:
            return self.chunk_fn(text)
        return f"part-summary-{len(text)}", 1


def test_disabled_is_one_serial_call(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    msgs = _transcript(30, seed=1)
    out = engine._summarize_leaf_chunk_with_rescue(msgs)
    assert len(rec.chunk_calls) == 1 and rec.reduce_calls == []
    assert out[0] == msgs


def test_enabled_below_one_chunk_stays_serial(tmp_path, monkeypatch):
    engine = _engine(tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=10_000_000)
    rec = _Recorder()
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    engine._summarize_leaf_chunk_with_rescue(_transcript(30, seed=2))
    assert len(rec.chunk_calls) == 1 and rec.reduce_calls == []


def test_enabled_maps_concurrently_then_reduces_once(tmp_path, monkeypatch):
    msgs = _transcript(60, seed=3)
    total = count_messages_tokens(msgs)
    engine = _engine(
        tmp_path, parallel_chunks_enabled=True,
        parallel_chunk_tokens=total // 4, parallel_max_concurrency=4,
    )
    expected = split_pair_safe(msgs, total // 4)
    assert len(expected) >= 3
    barrier = threading.Barrier(min(4, len(expected)), timeout=5)

    def chunk_fn(text):
        barrier.wait()  # proves >= min(4, n) chunk calls are in flight together
        return f"S[{text[:40]}]", 2

    rec = _Recorder(chunk_fn=chunk_fn, reduce_fn=lambda text: ("FINAL", 1))
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    chunk, source_tokens, summary, level, attempts = engine._summarize_leaf_chunk_with_rescue(msgs)

    assert (chunk, summary, level, attempts) == (msgs, "FINAL", 2, 1)
    assert source_tokens == total
    assert len(rec.chunk_calls) == len(expected)
    assert len(rec.reduce_calls) == 1
    reduce_text, reduce_source, reduce_budget = rec.reduce_calls[0]
    # The reduce keeps the serial call's budget and source size.
    assert reduce_source == total
    assert reduce_budget == engine._leaf_summary_token_budget(total)
    # Parts are in transcript order.
    positions = [reduce_text.index(f"[PART {i}/{len(expected)}") for i in range(1, len(expected) + 1)]
    assert positions == sorted(positions)


def test_chunk_failure_walks_the_rescue_ladder(tmp_path, monkeypatch):
    """A retry-worthy chunk error shrinks the leaf and retries, like serial."""
    msgs = _transcript(60, seed=4)
    total = count_messages_tokens(msgs)
    engine = _engine(
        tmp_path, parallel_chunks_enabled=True,
        parallel_chunk_tokens=total // 3, parallel_max_concurrency=4,
    )
    state = {"failed": False}

    def chunk_fn(text):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("400: prompt is too long")
        return "ok-part", 1

    rec = _Recorder(chunk_fn=chunk_fn)
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    chunk, _src, summary, _lvl, attempts = engine._summarize_leaf_chunk_with_rescue(msgs)
    assert attempts == 2, "the failing chunk must trigger the same shrink-and-retry rescue"
    assert len(chunk) < len(msgs)
    assert summary == "REDUCED"


def test_non_retry_worthy_chunk_error_propagates_unchanged(tmp_path, monkeypatch):
    msgs = _transcript(40, seed=5)
    total = count_messages_tokens(msgs)
    engine = _engine(tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=total // 3)

    class Boom(Exception):
        pass

    def chunk_fn(text):
        raise Boom("auth failed")

    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", _Recorder(chunk_fn=chunk_fn))
    with pytest.raises(Boom):
        engine._summarize_leaf_chunk_with_rescue(msgs)


def test_compress_writes_one_leaf_node_from_the_reduce(tmp_path, monkeypatch):
    msgs = [{"role": "system", "content": "sys"}] + _transcript(40, seed=6) + [
        {"role": "user", "content": "fresh"}
    ]
    total = count_messages_tokens(msgs)
    engine = _engine(tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=total // 4)
    rec = _Recorder(reduce_fn=lambda text: ("ONE-LEAF-SUMMARY Expand for details about: x", 1))
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    engine.compress(msgs)
    nodes = engine._dag.get_session_nodes(engine._session_id)
    leaves = [n for n in nodes if n.depth == 0]
    assert len(leaves) == 1
    assert leaves[0].summary.startswith("ONE-LEAF-SUMMARY")
    assert len(rec.chunk_calls) >= 2 and len(rec.reduce_calls) == 1


def test_workers_never_serialize_engine_state(tmp_path, monkeypatch):
    """Prism P1: serialization (session id, profile home, externalization) runs on the owner."""
    msgs = _transcript(40, seed=8)
    total = count_messages_tokens(msgs)
    engine = _engine(tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=total // 3)
    threads = []
    real = engine._serialize_messages

    def spy(messages):
        threads.append(threading.current_thread().name)
        return real(messages)

    monkeypatch.setattr(engine, "_serialize_messages", spy)
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", _Recorder())
    engine._summarize_leaf_chunk_with_rescue(msgs)
    assert threads and all(not name.startswith("lcm-parallel-leaf") for name in threads)


def test_reduce_input_is_bounded_by_chunk_tokens(tmp_path, monkeypatch):
    """Prism P1: many parts must not build one reducer prompt past a chunk's size."""
    msgs = _transcript(120, seed=9)
    total = count_messages_tokens(msgs)
    cap = total // 16
    engine = _engine(
        tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=cap,
        summary_spend_max_calls=0,
    )
    from plugins.context_engine.lcm.tokens import count_tokens

    # Each map summary returns exactly its budget in tokens (worst case).
    def chunk_fn_factory():
        def chunk_fn(text):
            return "w " * 3000, 1
        return chunk_fn

    rec = _Recorder(chunk_fn=chunk_fn_factory(), reduce_fn=lambda text: ("R " * 200, 1))
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    engine._summarize_leaf_chunk_with_rescue(msgs)
    assert len(rec.chunk_calls) >= 12
    per_part = max(1000, cap // len(rec.chunk_calls))
    assert all(budget <= per_part for _t, _s, budget in rec.chunk_calls)
    assert len(rec.reduce_calls) >= 2, "oversized part set must reduce in groups"
    for text, _src, _budget in rec.reduce_calls:
        # header + part markers are small; the parts themselves respect the cap
        assert count_tokens(text) <= cap + 3000 * 2 + 500


def test_spend_guard_without_room_for_fanout_stays_serial(tmp_path, monkeypatch):
    """Prism P1: never let the guard trip mid-map and push the reduce to L3."""
    msgs = _transcript(60, seed=10)
    total = count_messages_tokens(msgs)
    engine = _engine(
        tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=total // 4,
        summary_spend_max_calls=24,
    )
    for _ in range(22):
        engine._summary_spend_guard.record_call()
    rec = _Recorder()
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    engine._summarize_leaf_chunk_with_rescue(msgs)
    assert len(rec.chunk_calls) == 1 and rec.reduce_calls == []


def test_reduce_truncation_keeps_the_part_summaries(tmp_path, monkeypatch):
    msgs = _transcript(60, seed=11)
    total = count_messages_tokens(msgs)
    engine = _engine(tmp_path, parallel_chunks_enabled=True, parallel_chunk_tokens=total // 3)
    rec = _Recorder(
        chunk_fn=lambda text: (f"PART<{text[:12]}>", 1),
        reduce_fn=lambda text: ("truncated head...tail", 3),
    )
    monkeypatch.setattr(lcm_engine, "summarize_with_escalation", rec)
    _chunk, _src, summary, level, _a = engine._summarize_leaf_chunk_with_rescue(msgs)
    assert summary.count("PART<") == len(rec.chunk_calls) >= 2
    assert level == 2


# ── cancellation (extends #1492) ─────────────────────────────────────────


def _stream_chunk(text: str):
    delta = SimpleNamespace(content=text, reasoning_content=None, reasoning=None, tool_calls=None)
    return SimpleNamespace(
        id="c", model="m", choices=[SimpleNamespace(delta=delta, finish_reason=None, index=0)],
        usage=None,
    )


class _EndlessStream:
    MAX_CHUNKS = 600  # bounded so a regression FAILS, never hangs

    def __init__(self):
        self.yielded = 0
        self.closed = threading.Event()

    def __iter__(self):
        while not self.closed.is_set() and self.yielded < self.MAX_CHUNKS:
            self.yielded += 1
            time.sleep(0.002)
            yield _stream_chunk("x")

    def close(self):
        self.closed.set()


def _run_streaming_map(n_chunks, trigger):
    streams = [_EndlessStream() for _ in range(n_chunks)]
    started = threading.Barrier(n_chunks + 1, timeout=5)
    reduce_called = threading.Event()

    def summarize_chunk(_chunk, index):
        started.wait()
        aux._aggregate_chat_stream(streams[index])
        return "done", 1

    def reduce(_parts):
        reduce_called.set()
        return "reduced", 1

    def _later():
        started.wait()
        while min(s.yielded for s in streams) < 5:
            time.sleep(0.001)
        trigger()

    threading.Thread(target=_later, daemon=True).start()
    return streams, summarize_chunk, reduce, reduce_called


def test_fence_cancel_aborts_every_in_flight_chunk():
    fence = CompressionCommitFence()
    n = 4
    streams, summarize_chunk, reduce, reduce_called = _run_streaming_map(
        n, lambda: fence.try_cancel_before_commit()
    )
    signal = _SummaryCancelSignal(threading.Event(), fence)
    with aux.aux_interrupt_protection(cancel_event=signal):
        with pytest.raises(AuxiliaryExplicitCancellation):
            run_map_reduce(
                [[{}]] * n, summarize_chunk=summarize_chunk, reduce=reduce, max_concurrency=n,
            )
    deadline = time.monotonic() + 2
    while not all(s.closed.is_set() for s in streams) and time.monotonic() < deadline:
        time.sleep(0.01)
    for s in streams:
        assert s.closed.is_set(), "every in-flight chunk stream must be closed"
        assert s.yielded < _EndlessStream.MAX_CHUNKS // 4, s.yielded
    assert not reduce_called.is_set()


def test_one_chunk_error_aborts_sibling_streams_and_raises_the_real_error():
    n = 3
    streams = [_EndlessStream() for _ in range(n)]
    started = threading.Barrier(n, timeout=5)

    def summarize_chunk(_chunk, index):
        started.wait()
        if index == 1:
            while min(streams[0].yielded, streams[2].yielded) < 5:
                time.sleep(0.001)
            raise RuntimeError("prompt is too long")
        aux._aggregate_chat_stream(streams[index])
        return "done", 1

    with pytest.raises(RuntimeError, match="prompt is too long"):
        run_map_reduce(
            [[{}]] * n, summarize_chunk=summarize_chunk,
            reduce=lambda parts: ("r", 1), max_concurrency=n,
        )
    deadline = time.monotonic() + 2
    while not (streams[0].closed.is_set() and streams[2].closed.is_set()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert streams[0].closed.is_set() and streams[2].closed.is_set()
    assert streams[0].yielded < _EndlessStream.MAX_CHUNKS // 4


def test_workers_inherit_progress_hook():
    ticks = []
    lock = threading.Lock()

    def hook():
        with lock:
            ticks.append(threading.current_thread().name)

    def summarize_chunk(_chunk, _index):
        aux._notify_aux_provider_response()
        return "p", 1

    with aux.aux_progress_hook(hook):
        run_map_reduce(
            [[{}]] * 3, summarize_chunk=summarize_chunk,
            reduce=lambda parts: ("r", 1), max_concurrency=3,
        )
    assert len(ticks) == 3
    assert all(name.startswith("lcm-parallel-leaf") for name in ticks)
