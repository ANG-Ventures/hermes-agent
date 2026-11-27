"""Per-step wall attribution and per-tick memos for the dispatcher tick (t_97a78e19).

Measured 2026-11-10 on the Studio (default gateway, load1 p50 124): tick wall p50 151 s, and the gate-ticks
record explained ~35 s of it. A *scope* is a ``{step: wall ms}`` dict bound to the ticking thread. Scopes nest
(each board's ``dispatch_once`` inside the gateway tick); a closing scope adds its steps into its parent, so the
gateway's ``gate-ticks.jsonl`` line carries every board's steps summed.

A step key with no ``/`` is a top-level slice of the tick (a :class:`TickLockWindow` phase, or a step outside the
board lock); top-level keys partition the tick, and ``unattributed`` is the wall left over. ``<phase>/<step>`` is
a sub-step inside that phase: every step that can spawn a process or walk the filesystem.

:func:`tick_memo` caches a value for the rest of the OUTERMOST open scope (the gateway tick) or, with
``inner=True``, the innermost one (one board's ``dispatch_once``). Outside a tick it calls through, so nothing
outside the dispatcher sees a cached answer.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Callable, Hashable, Iterator

_LOCAL = threading.local()


def _memos() -> list:
    memos = getattr(_LOCAL, "memos", None)
    if memos is None:
        memos = _LOCAL.memos = []
    return memos


def _stack() -> list:
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = _LOCAL.stack = []
    return stack


@contextlib.contextmanager
def scope() -> Iterator[dict]:
    """Open a step scope on this thread; yields its ``{step: ms}``, folded into the enclosing scope on exit."""
    stack = _stack()
    memos = _memos()
    steps: dict = {}
    stack.append(steps)
    memos.append({})
    try:
        yield steps
    finally:
        stack.pop()
        memos.pop()
        if stack:
            parent = stack[-1]
            for k, v in steps.items():
                parent[k] = parent.get(k, 0.0) + v


@contextlib.contextmanager
def step(name: str) -> Iterator[None]:
    """Add this block's wall ms to the open scope's ``name`` (no scope: timing only), raise or not."""
    stack = _stack()
    steps = stack[-1] if stack else None
    t0 = time.monotonic()
    try:
        yield
    finally:
        if steps is not None:
            steps[name] = steps.get(name, 0.0) + (time.monotonic() - t0) * 1000.0


def add(name: str, ms: float) -> None:
    """Add ``ms`` to the open scope's ``name`` (no scope: a no-op)."""
    stack = _stack()
    if stack and ms > 0:
        stack[-1][name] = stack[-1].get(name, 0.0) + ms


def active() -> bool:
    """True while this thread is inside a dispatcher tick."""
    return bool(getattr(_LOCAL, "stack", None))


def tick_memo(key: Hashable, compute: Callable[[], Any], *, inner: bool = False) -> Any:
    """``compute()`` once per tick scope for ``key`` (outermost, or innermost with ``inner``); outside a tick,
    every call computes. An exception is not cached (the next call computes again)."""
    memos = _memos()
    if not memos:
        return compute()
    memo = memos[-1] if inner else memos[0]
    try:
        return memo[key]
    except KeyError:
        value = memo[key] = compute()
        return value


def record(steps: dict, wall_ms: float) -> dict:
    """The ``steps_ms`` field of a gate-ticks line: rounded steps plus ``unattributed`` (tick wall minus the sum
    of the top-level steps, floored at 0)."""
    top = sum(v for k, v in steps.items() if "/" not in k)
    out = {k: round(v, 3) for k, v in sorted(steps.items())}
    out["unattributed"] = round(max(0.0, wall_ms - top), 3)
    return out
