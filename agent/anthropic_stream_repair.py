"""Repair split UTF-16 surrogates in Anthropic ``input_json_delta`` events.

Root cause (2026-08-18 incident, recurring since PR #535): the text/thinking
delta lanes were fixed with ``_SurrogateSplicer``, but TOOL-CALL argument JSON
travels on a third lane — ``input_json_delta.partial_json`` — that no splicer
covered.  The Anthropic SDK's ``accumulate_event`` does::

    json_buf += bytes(event.delta.partial_json, "utf-8")

inside ``MessageStream.__stream__``, BEFORE our event loop ever sees the
event.  A lone high surrogate (an emoji split across two SSE deltas by a
relay/proxy JSON re-encode) therefore raises ``UnicodeEncodeError`` deep
inside the SDK iterator, killing the whole stream after partial delivery.
The harness then returned a 0-char stub and announced a dishonest
"connection issue" fallback.

Fix: interpose on the raw SSE event stream *underneath* the SDK's
accumulator and run every ``partial_json`` fragment through a stateful,
per-content-block ``_SurrogateSplicer`` — the same carry-across-chunks
repair the text lane already uses.  Valid pairs split across fragments are
recombined into the real character; orphans are floored to U+FFFD, which
keeps the JSON fragment valid.

Everything here is FAIL-OPEN: if the SDK's private stream shape drifts or a
repair step throws, the original stream/event passes through untouched and
the worst case is the pre-existing behavior.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agent.message_sanitization import _SurrogateSplicer

logger = logging.getLogger(__name__)


class _ToolJsonRepairingRawStream:
    """Proxy for the SDK's raw SSE stream that repairs ``partial_json``.

    Wraps the object stored at ``MessageStream._raw_stream`` (an
    ``anthropic.Stream[RawMessageStreamEvent]``).  Iteration yields the same
    event objects with ``delta.partial_json`` repaired in place; every other
    attribute (``response``, ``close`` …) delegates to the inner stream so
    the SDK's own plumbing keeps working.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        # One carry-splicer per content_block index: fragments of the same
        # tool_use block concatenate into one JSON buffer, so a high
        # surrogate ending fragment N pairs with a low starting fragment N+1
        # of the SAME index only.
        self._splicers: Dict[int, _SurrogateSplicer] = {}

    # -- delegation ------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "_ToolJsonRepairingRawStream":
        enter = getattr(self._inner, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        exit_ = getattr(self._inner, "__exit__", None)
        if callable(exit_):
            return exit_(*exc)
        return None

    # -- iteration -------------------------------------------------------
    def __iter__(self):
        for event in self._inner:
            try:
                self._repair_event(event)
            except Exception:
                # Fail-open: a broken repair must never take down a healthy
                # stream. The event passes through unrepaired.
                logger.debug(
                    "input_json_delta surrogate repair failed; passing "
                    "event through unmodified",
                    exc_info=True,
                )
            yield event

    # -- repair ----------------------------------------------------------
    def _repair_event(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        if event_type == "content_block_delta":
            delta = getattr(event, "delta", None)
            if delta is None or getattr(delta, "type", None) != "input_json_delta":
                return
            fragment = getattr(delta, "partial_json", None)
            if not isinstance(fragment, str) or not fragment:
                return
            index = int(getattr(event, "index", 0) or 0)
            splicer = self._splicers.get(index)
            if splicer is None:
                splicer = self._splicers[index] = _SurrogateSplicer()
            repaired = splicer.feed(fragment)
            if repaired != fragment:
                delta.partial_json = repaired
        elif event_type == "content_block_stop":
            # Discard any trailing orphan high surrogate held by this
            # block's splicer.  Dropping half a character from the end of a
            # JSON string value keeps the buffer valid JSON; there is no
            # later fragment to append a replacement char to.
            index = int(getattr(event, "index", 0) or 0)
            splicer = self._splicers.pop(index, None)
            if splicer is not None:
                tail = splicer.flush()
                if tail:
                    logger.debug(
                        "Dropped orphan trailing surrogate at end of "
                        "tool-input JSON block index=%s",
                        index,
                    )


def install_tool_json_surrogate_repair(message_stream: Any) -> bool:
    """Interpose the repair proxy on an SDK ``MessageStream``.

    Must run after ``messages.stream(...).__enter__()`` and before the first
    event is consumed (``MessageStream.__stream__`` is a generator, so it
    reads ``self._raw_stream`` lazily at iteration time).

    Returns True when installed; False (never raises) when the SDK shape is
    unrecognized — the caller proceeds with the unwrapped stream, i.e. the
    pre-existing behavior.
    """
    try:
        raw = getattr(message_stream, "_raw_stream", None)
        if raw is None or isinstance(raw, _ToolJsonRepairingRawStream):
            return False
        message_stream._raw_stream = _ToolJsonRepairingRawStream(raw)
        return True
    except Exception:
        logger.debug(
            "Could not install tool-json surrogate repair; SDK stream "
            "shape unrecognized",
            exc_info=True,
        )
        return False
