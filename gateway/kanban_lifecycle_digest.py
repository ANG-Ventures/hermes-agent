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

t_26d7df3d (2026-10-02, alerts r33 G): #logs still carried 296 lifecycle lines in 24 h (one post
each: the knob was never set). Ace's rule is one receipt line per batch / <= 4 h digest, so the
fleet runs ``lifecycle_digest_seconds: 14400``. A 4 h batch held up to 80 lines; at 220 chars a
line that rendered ~17 KB, which the Discord adapter splits into ~9 posts (the default stays 0). ``render`` is now
bounded to ONE message (``MAX_MESSAGE_CHARS``): each line is compacted to its mark, card id and
title, and lines past the budget fold into a "+N more" tally. Nothing is lost: every transition
is a card event on the board (``hermes kanban show <id>``).

t_dcc4ed08 (2026-10-02): Apollo's 17:18 operator batch sent 8 landed-close-gate send-backs, which
posted as 8 lines. A line added with ``fold={"key": ...}`` (the coverage ``batch_id``) is rendered
together with the other held lines that share its key, as ONE line:
``⏳ [board] Kanban 8 cards landed (batch apollo-1700) · close on: t_a <gate>; t_b <gate>; ...``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

FAMILY_TAG = "-# family=digest:kanban-lifecycle"
MAX_HELD = 400  # lines per channel; oldest dropped (and counted) past this
LINE_CHARS = 220
MAX_MESSAGE_CHARS = 1900  # one Discord message (2000) with headroom; the digest never splits
COMPACT_CHARS = 110
FOLD_CHARS = 900  # one folded operator-batch line; cards past it tally as "+N more"
# ``✔ [board] @who Kanban t_x done — title`` -> mark, board, card id, title.
_LINE_RE = re.compile(r"^(\S+)\s+\[([^\]]+)\]\s+@\S+\s+Kanban\s+(t_[0-9a-f]+)\b.*?(?:—|:)\s*(.*)$")


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


def _compact(msg: str) -> str:
    """``✔ t_x [board] title`` (board shown only off default), clipped to COMPACT_CHARS."""
    first = _one_line(msg)
    m = _LINE_RE.match(first)
    if not m:
        out = first
    else:
        mark, board, tid, title = m.groups()
        out = f"{mark} {tid}" + ("" if board == "default" else f" [{board}]") + f" {title.strip()}"
    return out if len(out) <= COMPACT_CHARS else out[: COMPACT_CHARS - 1] + "…"


def _fold_line(folds: list[dict]) -> str:
    """ONE line for an operator batch of landed close-gate send-backs (t_dcc4ed08)."""
    f0 = folds[0]
    head = f"⏳ {f0.get('board_tag') or ''}Kanban {len(folds)} cards landed"
    if f0.get("batch"):
        head += f" (batch {f0['batch']})"
    head += " · close on: "
    parts: list[str] = []
    for i, f in enumerate(folds):
        who = f" @{f['implementer']}" if f.get("implementer") else ""
        part = f"{f['task_id']} {f.get('gate') or ''}{who}".strip()
        if len(head) + len("; ".join(parts + [part])) > FOLD_CHARS:
            parts.append(f"+{len(folds) - i} more")
            break
        parts.append(part)
    return head + "; ".join(parts)


def fold_batches(lines: list[str], folds: Optional[list] = None) -> list[str]:
    """Replace every group of 2+ held lines sharing a fold key by ONE line at the first's position."""
    if not folds or not any(folds):
        return list(lines)
    groups: dict[str, list[dict]] = {}
    for f in folds:
        if f and f.get("key"):
            groups.setdefault(f["key"], []).append(f)
    out: list[str] = []
    done: set[str] = set()
    for msg, f in zip(lines, list(folds) + [None] * (len(lines) - len(folds))):
        key = (f or {}).get("key")
        if not key or len(groups.get(key, ())) < 2:
            out.append(msg)
        elif key not in done:
            done.add(key)
            out.append(_fold_line(groups[key]))
    return out


def render(lines: list[str], dropped: int = 0, folds: Optional[list] = None) -> str:
    """One held line posts unchanged; two or more become ONE digest message that fits one post."""
    lines = fold_batches(lines, folds)
    if len(lines) == 1 and not dropped:
        return lines[0]
    counts: dict[str, int] = {}
    for m in lines:
        mark = (m.strip()[:1] or "?")
        counts[mark] = counts.get(mark, 0) + 1
    tally = " · ".join(f"{k} {v}" for k, v in counts.items())
    head = f"🗂 **kanban lifecycle** — {len(lines)} transition(s) ({tally})"
    tail = []
    if dropped:
        tail.append(f"-# {dropped} older line(s) dropped while this channel was unreachable")
    tail.append(FAMILY_TAG)
    full = [m if m.startswith("⏳") and " cards landed" in m else _one_line(m) for m in lines]
    if len("\n".join([head] + full + tail)) <= MAX_MESSAGE_CHARS:
        return "\n".join([head] + full + tail)
    budget = MAX_MESSAGE_CHARS - len(head) - sum(len(t) + 1 for t in tail) - 80
    body: list[str] = []
    for i, m in enumerate(lines):
        c = _compact(m)
        if budget - (len(c) + 1) < 0:
            body.append(f"… +{len(lines) - i} more (card events: `hermes kanban show <id>`)")
            break
        body.append(c)
        budget -= len(c) + 1
    return "\n".join([head] + body + tail)


class LifecycleDigest:
    """Per-target buffer: ``(platform, chat_id) -> held lines``. Not thread-safe; the notifier is
    a single coroutine."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, str], dict] = {}

    def __len__(self) -> int:
        return sum(len(b["lines"]) for b in self._held.values())

    def add(self, target: tuple[str, str], adapter: Any, msg: str, window: int, now: float,
            metadata: Optional[dict] = None, fallback: Optional[tuple] = None,
            fold: Optional[dict] = None) -> None:
        """``fallback=((platform, chat_id), adapter, window)``: where the batch goes, each line
        tagged ``[home-unreachable:<reason>]``, if the send to ``target`` fails (t_808bc8e6)."""
        b = self._held.setdefault(target, {"lines": [], "first": now, "adapter": adapter,
                                           "window": window, "dropped": 0, "folds": []})
        b["adapter"] = adapter
        b["window"] = window
        b["metadata"] = dict(metadata or {})
        b["fallback"] = fallback
        b["lines"].append(msg)
        b.setdefault("folds", []).append(fold)
        if len(b["lines"]) > MAX_HELD:
            over = len(b["lines"]) - MAX_HELD
            del b["lines"][:over]
            del b["folds"][:over]
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
            from gateway.kanban_home_route import format_for_platform, tag_line, unreachable_tag

            text = format_for_platform(target[0], render(b["lines"], b["dropped"], b.get("folds")))
            reason = None
            try:
                res = await b["adapter"].send(target[1], text, metadata=dict(b.get("metadata") or {}))
                if getattr(res, "success", True) is False:
                    reason = getattr(res, "error_kind", None) or "send-failed"
                    raise RuntimeError(getattr(res, "error", None) or "send reported failure")
            except Exception as exc:
                fb = b.get("fallback")
                if fb is not None:
                    # Home unreachable: the batch moves to the fallback (#logs), tagged, and
                    # posts there on its own window; the home is not retried for these lines.
                    fb_target, fb_adapter, fb_window = fb
                    tag = unreachable_tag(reason or type(exc).__name__)
                    logger.warning("kanban lifecycle digest: home %s:%s unreachable (%s); %d line(s)"
                                   " -> %s:%s", target[0], target[1], exc, len(b["lines"]),
                                   fb_target[0], fb_target[1])
                    self._held.pop(target, None)
                    for line, fold in zip(b["lines"], b.get("folds") or [None] * len(b["lines"])):
                        self.add(fb_target, fb_adapter, tag_line(line, tag), fb_window, now, fold=fold)
                    continue
                logger.warning("kanban lifecycle digest: send to %s:%s failed (%d line(s) kept): %s",
                               target[0], target[1], len(b["lines"]), exc)
                continue
            self._held.pop(target, None)
            sent += 1
        return sent
