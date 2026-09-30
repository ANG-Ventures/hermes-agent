"""Map-reduce for large LCM leaf summaries (``compression.parallel_chunks``).

A single leaf summary of a ~650k-token backlog is one long aux call. This
module splits the leaf source at message boundaries, summarizes the chunks
concurrently, and hands the ordered chunk summaries to one reduce call. The
engine still writes ONE leaf node, so the DAG shape matches the serial path.

Boundary rule: a chunk never ends while an assistant tool call is waiting for
its result, and never starts with a tool result. ``_serialize_messages`` drops
any tool call whose result is not in the same serialized batch, so a split
pair would silently lose the call from the summary.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import threading
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .message_analysis import _tool_call_id
from .tokens import count_message_tokens

logger = logging.getLogger(__name__)

# A trailing chunk smaller than this fraction of the target is merged into the
# previous chunk: one more concurrent call is not worth a sliver of context.
_MIN_TAIL_FRACTION = 0.25
# How often the owner re-checks the host cancel source while chunks run.
_OWNER_POLL_SECONDS = 0.25


def safe_boundaries(messages: Sequence[Dict[str, Any]]) -> List[bool]:
    """``out[i]`` is True when a chunk may start at message ``i`` (0 < i < n).

    Index 0 and ``n`` are always boundaries of the whole batch and are
    reported True. An interior index is safe when no tool call issued before
    it still awaits a result at or after it, and message ``i`` is not a tool
    result.
    """
    n = len(messages)
    out = [True] * (n + 1)
    pending: set[str] = set()
    for i, msg in enumerate(messages):
        if i > 0:
            out[i] = not pending and msg.get("role") != "tool"
        role = msg.get("role")
        if role == "assistant":
            for tool_call in msg.get("tool_calls") or []:
                call_id = _tool_call_id(tool_call)
                if call_id:
                    pending.add(call_id)
        elif role == "tool":
            pending.discard(str(msg.get("tool_call_id") or "").strip())
    return out


def split_pair_safe(
    messages: Sequence[Dict[str, Any]],
    chunk_tokens: int,
    count: Callable[[Dict[str, Any]], int] = count_message_tokens,
) -> List[List[Dict[str, Any]]]:
    """Split ``messages`` into ordered chunks of about ``chunk_tokens`` each.

    A chunk closes before the message that would push it past the target,
    but only at a safe boundary; otherwise it grows until the next one. The
    concatenation of the chunks is always exactly ``messages``.
    """
    target = max(1, int(chunk_tokens))
    ok = safe_boundaries(messages)
    chunks: List[List[Dict[str, Any]]] = []
    sizes: List[int] = []
    current: List[Dict[str, Any]] = []
    used = 0
    for i, msg in enumerate(messages):
        tokens = count(msg)
        if current and used + tokens > target and ok[i]:
            chunks.append(current)
            sizes.append(used)
            current, used = [], 0
        current.append(msg)
        used += tokens
    if current:
        if chunks and used < target * _MIN_TAIL_FRACTION:
            chunks[-1].extend(current)
            sizes[-1] += used
        else:
            chunks.append(current)
            sizes.append(used)
    return chunks


def run_map_reduce(
    chunks: Sequence[List[Dict[str, Any]]],
    *,
    summarize_chunk: Callable[[List[Dict[str, Any]], int], Tuple[str, int]],
    reduce: Callable[[List[str]], Tuple[str, int]],
    max_concurrency: int,
) -> Tuple[str, int]:
    """Summarize ``chunks`` concurrently, then reduce. Returns (text, level).

    ``summarize_chunk(chunk, index)`` and ``reduce(parts)`` run the normal
    escalation ladder; any exception they raise propagates unchanged so the
    caller's rescue path sees it exactly as it would from a serial summary.
    On the first chunk failure, or when the host cancel source fires, every
    sibling chunk is told to abort on its next stream frame and queued chunks
    never start.
    """
    from agent.auxiliary_client import (
        AuxiliaryExplicitCancellation,
        capture_aux_thread_state,
        raise_if_aux_cancel_requested,
    )

    raise_if_aux_cancel_requested()
    abort = threading.Event()
    install = capture_aux_thread_state(extra_cancel_event=abort)

    def _worker(chunk: List[Dict[str, Any]], index: int) -> Tuple[str, int]:
        with install():
            raise_if_aux_cancel_requested()
            return summarize_chunk(chunk, index)

    workers = max(1, min(int(max_concurrency or 1), len(chunks)))
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lcm-parallel-leaf"
    )
    futures: List[concurrent.futures.Future] = []
    try:
        for index, chunk in enumerate(chunks):
            ctx = contextvars.copy_context()
            futures.append(pool.submit(ctx.run, _worker, chunk, index))
        pending = set(futures)
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=_OWNER_POLL_SECONDS,
                return_when=concurrent.futures.FIRST_EXCEPTION,
            )
            if any(f.exception() is not None for f in done):
                break
            raise_if_aux_cancel_requested()
    except BaseException:
        abort.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    failures = [f for f in futures if f.done() and not f.cancelled() and f.exception() is not None]
    if failures:
        abort.set()
        pool.shutdown(wait=False, cancel_futures=True)
        errors = [f.exception() for f in failures]
        # A sibling aborted by our own abort Event reports a cancellation;
        # the chunk that actually failed carries the error the rescue ladder
        # needs to classify.
        primary = next(
            (e for e in errors if not isinstance(e, AuxiliaryExplicitCancellation)),
            errors[0],
        )
        raise primary
    pool.shutdown(wait=False)
    results = [f.result() for f in futures]
    parts = [text for text, _level in results]
    level = max((lvl for _text, lvl in results), default=1)
    raise_if_aux_cancel_requested()
    reduced, reduce_level = reduce(parts)
    return reduced, max(level, reduce_level)


def format_reduce_input(parts: Sequence[str]) -> str:
    """Join ordered chunk summaries for the reduce call."""
    total = len(parts)
    blocks = [
        f"[PART {i}/{total} of one conversation segment, oldest first]\n{text.strip()}"
        for i, text in enumerate(parts, start=1)
    ]
    header = (
        "The conversation segment below was summarized in consecutive parts. "
        "Merge the parts into one summary of the whole segment in time order. "
        "Keep every named person, system, file path, command, identifier, "
        "number, and decision that any part mentions; drop only repetition "
        "between parts.\n\n"
    )
    return header + "\n\n".join(blocks)
