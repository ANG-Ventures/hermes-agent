"""Batch kanban lifecycle lines bound for ``kanban.lifecycle_channel`` into one digest post.

t_d62bd921 (2026-10-02): with ``kanban.lifecycle_channel`` pointing at #logs, every done /
ready-for-review / blocked transition was its own post: 36 lines in 2 h (16:33-18:31Z), 23 of
them in one hour. A log channel needs the record, not a stream. With
``kanban.lifecycle_digest_seconds: N`` (default 0 = off, one post per line as before), routed
lines are held and posted as ONE message once the oldest held line is N seconds old. Replayed
over that 2 h window, N=900 turns 36 posts into 7.

Only the passive log line is batched. The subscriber's wake, its cursor and its failure counter
behave exactly as for a delivered line; failure lines and anything that stays in the origin chat
never come here. A held line lives in memory: a gateway stop inside the window loses at most one
window of #logs lines (the card events stay on the board). A failed send keeps the batch for the
next tick; the buffer is capped so a dead channel cannot grow it without bound.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

FAMILY_TAG = "-# family=digest:kanban-lifecycle"
MAX_HELD = 400  # lines per channel; oldest dropped (and counted) past this
LINE_CHARS = 220


def parse_digest_seconds(value: Any) -> int:
    """``kanban.lifecycle_digest_seconds`` -> int >= 0; anything unparseable is 0 (off)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def resolve_digest_seconds() -> int:
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        return parse_digest_seconds(kcfg.get("lifecycle_digest_seconds") if isinstance(kcfg, dict) else None)
    except Exception:
        return 0


def _one_line(msg: str) -> str:
    """Header line of a lifecycle message (``✔ [board] @who Kanban t_x done — title``)."""
    first = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    return first if len(first) <= LINE_CHARS else first[: LINE_CHARS - 1] + "…"


def render(lines: list[str], dropped: int = 0) -> str:
    """One held line posts unchanged; two or more become one digest message."""
    if len(lines) == 1 and not dropped:
        return lines[0]
    counts: dict[str, int] = {}
    for m in lines:
        mark = (m.strip()[:1] or "?")
        counts[mark] = counts.get(mark, 0) + 1
    tally = " · ".join(f"{k} {v}" for k, v in counts.items())
    body = [f"🗂 **kanban lifecycle** — {len(lines)} transition(s) ({tally})"]
    body += [_one_line(m) for m in lines]
    if dropped:
        body.append(f"-# {dropped} older line(s) dropped while this channel was unreachable")
    body.append(FAMILY_TAG)
    return "\n".join(body)


class LifecycleDigest:
    """Per-target buffer: ``(platform, chat_id) -> held lines``. Not thread-safe; the notifier is
    a single coroutine."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, str], dict] = {}

    def __len__(self) -> int:
        return sum(len(b["lines"]) for b in self._held.values())

    def add(self, target: tuple[str, str], adapter: Any, msg: str, window: int, now: float) -> None:
        b = self._held.setdefault(target, {"lines": [], "first": now, "adapter": adapter,
                                           "window": window, "dropped": 0})
        b["adapter"] = adapter
        b["window"] = window
        b["lines"].append(msg)
        if len(b["lines"]) > MAX_HELD:
            over = len(b["lines"]) - MAX_HELD
            del b["lines"][:over]
            b["dropped"] += over

    def due(self, now: float) -> list[tuple[str, str]]:
        return [t for t, b in self._held.items() if b["lines"] and now - b["first"] >= b["window"]]

    async def flush(self, now: float, force: bool = False) -> int:
        """Post every due batch. Returns the number of messages sent."""
        sent = 0
        for target in list(self._held) if force else self.due(now):
            b = self._held.get(target)
            if not b or not b["lines"]:
                self._held.pop(target, None)
                continue
            text = render(b["lines"], b["dropped"])
            try:
                res = await b["adapter"].send(target[1], text, metadata={})
                if getattr(res, "success", True) is False:
                    raise RuntimeError(getattr(res, "error", None) or "send reported failure")
            except Exception as exc:
                logger.warning("kanban lifecycle digest: send to %s:%s failed (%d line(s) kept): %s",
                               target[0], target[1], len(b["lines"]), exc)
                continue
            self._held.pop(target, None)
            sent += 1
        return sent
