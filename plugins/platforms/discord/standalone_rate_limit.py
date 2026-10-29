"""HTTP 429 handling for the Discord standalone REST send (no live adapter).

The standalone path (cron runner, ``hermes send``) used to POST once and give up on 429: at 26
sends to one channel in 0.8 s, 21 were lost (P0-4, t_bce91ec9). discord.py retries; this is the
small equivalent for the REST path:

* a per-channel lock inside the process, so concurrent sends to one channel queue instead of
  stampeding the ~5-per-5-s bucket. It is a ``threading.Lock`` polled from async code because each
  standalone send runs ``asyncio.run`` on its own thread, so there is no shared event loop.
* a 429 sleeps the server's ``retry_after`` (JSON body, else ``Retry-After``) and re-POSTs. A 429
  means the message was NOT created, so the retry cannot duplicate. ``X-RateLimit-Global`` (or a
  body ``global: true``) pauses every standalone send in the process.
* when a success reports ``X-RateLimit-Remaining: 0``, the next send on that channel waits
  ``X-RateLimit-Reset-After`` first, so a queued burst mostly avoids the 429 entirely.

Bounded: at most ``MAX_429_RETRIES`` retries and ``MAX_WAIT_S`` seconds of rate-limit sleep per
send; past that the 429 is returned as the usual error. Time spent queued on the channel lock is
not rate-limit wait; the caller's own send timeout bounds it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional, Tuple

from agent.retry_utils import parse_retry_after_seconds

MAX_429_RETRIES = 5
MAX_WAIT_S = 30.0
_LOCK_POLL_S = 0.01

_STATE_LOCK = threading.Lock()
_CHANNEL_LOCKS: Dict[str, threading.Lock] = {}
_CHANNEL_RESUME_AT: Dict[str, float] = {}
_GLOBAL_RESUME_AT = [0.0]

# Seams for tests (read at call time).
_sleep = asyncio.sleep
_monotonic = time.monotonic


def _channel_lock(key: str) -> threading.Lock:
    with _STATE_LOCK:
        lock = _CHANNEL_LOCKS.get(key)
        if lock is None:
            lock = _CHANNEL_LOCKS[key] = threading.Lock()
        return lock


async def _acquire(lock: threading.Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(_LOCK_POLL_S)


def _headers(resp: Any) -> Mapping:
    headers = getattr(resp, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _header(headers: Mapping, name: str) -> Optional[str]:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return None if value is None else str(value)


def _float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _note_bucket(key: str, headers: Mapping) -> None:
    """Remember when an exhausted bucket refills, from a non-429 response's headers."""
    if _header(headers, "X-RateLimit-Remaining") != "0":
        return
    reset_after = _float(_header(headers, "X-RateLimit-Reset-After"))
    if reset_after:
        with _STATE_LOCK:
            _CHANNEL_RESUME_AT[key] = _monotonic() + reset_after


def _resume_delay(key: str) -> float:
    with _STATE_LOCK:
        resume_at = max(_CHANNEL_RESUME_AT.get(key, 0.0), _GLOBAL_RESUME_AT[0])
    return max(0.0, resume_at - _monotonic())


def _retry_after(headers: Mapping, body_text: str) -> Tuple[Optional[float], bool]:
    """``(seconds, is_global)`` from a 429: JSON ``retry_after`` first, then ``Retry-After``."""
    body: Dict[str, Any] = {}
    try:
        parsed = json.loads(body_text) if body_text else {}
        body = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    seconds = _float(body.get("retry_after"))
    if seconds is None:
        seconds = parse_retry_after_seconds(headers)
    is_global = body.get("global") is True or (_header(headers, "X-RateLimit-Global") or "").lower() == "true"
    return seconds, is_global


async def post_json_or_error(
    session: Any, url: str, build_kwargs: Callable[[], Dict[str, Any]], error_prefix: str, channel_key: str,
) -> Tuple[Optional[dict], Optional[dict]]:
    """POST honouring Discord 429s; ``(data, None)`` on 200/201, else ``(None, send_error(...))``.

    ``build_kwargs`` is called per attempt so a multipart body is rebuilt for each retry."""
    from plugins.platforms.discord.adapter import (
        _DISCORD_STANDALONE_ERROR_BODY_LIMIT_BYTES,
        _standalone_read_text_limited,
        _standalone_response_json_or_error,
        send_error,
    )

    lock = _channel_lock(channel_key)
    await _acquire(lock)
    try:
        waited = 0.0
        retries = 0
        while True:
            pause = _resume_delay(channel_key)
            if pause and waited + pause <= MAX_WAIT_S:
                await _sleep(pause)
                waited += pause
            async with session.post(url, **build_kwargs()) as resp:
                headers = _headers(resp)
                if resp.status != 429:
                    _note_bucket(channel_key, headers)
                    return await _standalone_response_json_or_error(resp, error_prefix)
                body = await _standalone_read_text_limited(resp, _DISCORD_STANDALONE_ERROR_BODY_LIMIT_BYTES)
            delay, is_global = _retry_after(headers, body)
            if delay is None:
                delay = 1.0
            if is_global:
                with _STATE_LOCK:
                    _GLOBAL_RESUME_AT[0] = max(_GLOBAL_RESUME_AT[0], _monotonic() + delay)
            if retries >= MAX_429_RETRIES or waited + delay > MAX_WAIT_S:
                return None, send_error(f"{error_prefix} (429): {body}")
            retries += 1
            await _sleep(delay)
            waited += delay
    finally:
        lock.release()
