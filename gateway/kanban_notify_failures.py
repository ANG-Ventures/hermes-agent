"""Worker-failure notices for the kanban notifier: one message per failure event.

A spawn failure used to cost a chat two lines per card — ``crashed`` ("will
retry") and, in the same dispatcher tick, ``gave_up`` — and a lane-wide cause
(every card on one profile dying the same way) multiplied that by N. On
2026-09-25 four cards on one Codex lane produced 40+ messages in 20 minutes.

This module decides, per notifier tick, which failure events speak:

* a ``crashed`` event is dropped when the same claim also carries a later
  ``gave_up`` for that card — the retry decision is already known, so the
  "will retry" line is false;
* failures that share (chat, lane, classification) collapse into ONE notice
  naming the lane and the cards, and the same key stays quiet for
  ``LANE_WINDOW_SECONDS`` afterwards.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

LANE_WINDOW_SECONDS = 300
FAILURE_KINDS = frozenset({"crashed", "gave_up"})
_RUN_BOUNDARY_PREFIX = "[hermes-kanban-run-boundary "


def failure_classification(payload: Optional[dict]) -> str:
    """The failing run's last words: the final non-noise line of its log tail.

    The worker log is append-mode across runs, so only the text after the
    last run boundary belongs to THIS failure. Falls back to the dispatcher's
    own error text when no tail was captured.
    """
    payload = payload or {}
    tail = str(payload.get("stderr_tail") or "")
    idx = tail.rfind(_RUN_BOUNDARY_PREFIX)
    if idx >= 0:
        newline = tail.find("\n", idx)
        tail = tail[newline + 1:] if newline >= 0 else ""
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line and not line.startswith("Warning:"):
            return line[:160]
    return str(payload.get("error") or "").strip()[:160]


def collapse_retry_pairs(events: list) -> list:
    """Drop each ``crashed`` event that a later ``gave_up`` in the batch decides."""
    last_gave_up = max(
        (i for i, ev in enumerate(events) if ev.kind == "gave_up"), default=-1,
    )
    return [
        ev for i, ev in enumerate(events)
        if not (ev.kind == "crashed" and i < last_gave_up)
    ]


def _lane_key(delivery: dict, kind: str, classification: str) -> tuple:
    # ``kind`` is part of the key so a ``gave_up`` (a real state change) is
    # never hidden behind an earlier "will retry" notice for the same cause.
    sub = delivery["sub"]
    task = delivery.get("task")
    return (
        (sub.get("platform") or "").lower(),
        str(sub.get("chat_id") or ""),
        str(sub.get("thread_id") or ""),
        getattr(task, "assignee", None) or "",
        kind,
        classification,
    )


class LaneFailureDedupe:
    """Plans failure notices for one notifier tick; remembers sent lane keys."""

    def __init__(self, window_seconds: float = LANE_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._sent: dict[tuple, float] = {}

    def plan(self, deliveries: Iterable[dict], now: Optional[float] = None) -> None:
        """Collapse retry pairs and attach ``failure_directives`` to each delivery.

        ``failure_directives`` maps a failure event id to ``None`` (stay
        silent) or ``{"cards": [...], "classification": str, "lane_key": key}``
        (speak; ``cards`` lists every card the notice covers).
        """
        now = time.time() if now is None else now
        self._sent = {
            k: t for k, t in self._sent.items() if now - t < self.window_seconds
        }
        groups: dict[tuple, list[tuple[dict, Any]]] = {}
        for d in deliveries:
            d["events"] = collapse_retry_pairs(list(d["events"]))
            d["failure_directives"] = {}
            for ev in d["events"]:
                if ev.kind not in FAILURE_KINDS:
                    continue
                if (ev.payload or {}).get("stopped_early"):
                    continue  # reproduced no-op: its own needs-input notice
                classification = failure_classification(ev.payload)
                key = _lane_key(d, ev.kind, classification) if classification else None
                if key is None:
                    d["failure_directives"][ev.id] = {
                        "cards": [d["sub"]["task_id"]],
                        "classification": "",
                        "lane_key": None,
                    }
                    continue
                groups.setdefault(key, []).append((d, ev))
        for key, members in groups.items():
            if key in self._sent:
                for d, ev in members:
                    d["failure_directives"][ev.id] = None
                continue
            cards: list[str] = []
            for d, _ev in members:
                if d["sub"]["task_id"] not in cards:
                    cards.append(d["sub"]["task_id"])
            carrier_d, carrier_ev = members[0]
            carrier_d["failure_directives"][carrier_ev.id] = {
                "cards": cards,
                "classification": key[-1],
                "lane_key": key,
            }
            for d, ev in members[1:]:
                d["failure_directives"][ev.id] = None

    def mark_sent(self, key: Optional[tuple], now: Optional[float] = None) -> None:
        """Open the quiet window once a lane notice was actually delivered."""
        if key is not None:
            self._sent[key] = time.time() if now is None else now


def format_failure_notice(
    kind: str,
    payload: Optional[dict],
    *,
    task_id: str,
    board_tag: str,
    tag: str,
    assignee: str,
    directive: dict,
) -> str:
    """Render the one message a planned failure directive speaks."""
    payload = payload or {}
    classification = directive.get("classification") or ""
    cards = directive.get("cards") or [task_id]
    if len(cards) > 1:
        lane = assignee or "unassigned"
        shown = ", ".join(cards[:8]) + (f" (+{len(cards) - 8} more)" if len(cards) > 8 else "")
        return (
            f"✖ {board_tag}{lane}: {classification} — "
            f"{len(cards)} cards failing: {shown}"
        )
    suffix = f": {classification}" if classification else ""
    if kind == "gave_up":
        failures = payload.get("failures")
        count = f"{int(failures)} " if isinstance(failures, int) and failures > 0 else "repeated "
        return f"✖ {board_tag}{tag}Kanban {task_id} gave up after {count}spawn failures{suffix}"
    return (
        f"✖ {board_tag}{tag}Kanban {task_id} worker crashed (pid gone); "
        f"dispatcher will retry{suffix}"
    )
