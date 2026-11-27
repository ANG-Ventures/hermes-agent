"""Which provider names are Claude relay-backed lanes (t_d780729d).

Every Claude lane reaches the same subscriptions through a relay that already
knows each seat's state (Ace 2026-11-12 11:35: "a unified system that they all
report to and update"). What the harness learns about a seat's remaining usage
on one lane name therefore must not be kept as that lane's own verdict: a
stored per-lane "exhausted" disagrees with every other lane name of the same
pool the moment the relay rotates. Per-lane facts stay instrumentation (turns
db, logs); a routing decision on these lanes comes from the relay's answer.

The names follow the lane grammar (hermes-home ``lane-names.json``, v2):
``claude-<a|b|c|d>[t|h|s|a]<p|l><x|r>[f|s][-N]``. An ``r`` face is a pooled
relay door (no ``-N``); an ``x`` face with ``-N`` is a pin onto one seat.
"""

from __future__ import annotations

import re
from typing import Optional

_LANE_RE = re.compile(r"^claude-([abcd])([thsa]?)([pl])([xr])([fs]?)(?:-(\d+))?$")
# Pooled faces whose spelling predates the grammar.
_LEGACY_POOLED = frozenset({"claude-bpr-tui"})


def _parse(provider: Optional[str]):
    return _LANE_RE.fullmatch(str(provider or "").strip().lower())


def is_relay_pooled(provider: Optional[str]) -> bool:
    """A pooled relay door (``claude-apr``, ``claude-alr``, ``claude-dtlr`` ...)."""
    p = str(provider or "").strip().lower()
    if p in _LEGACY_POOLED:
        return True
    m = _parse(p)
    return bool(m) and m.group(4) == "r" and m.group(6) is None


def is_relay_pinned(provider: Optional[str]) -> bool:
    """A pin onto one seat of the relay's pool (``claude-apx-7``, ``claude-bpx-12``)."""
    m = _parse(provider)
    return bool(m) and m.group(4) == "x" and m.group(6) is not None


def is_relay_backed(provider: Optional[str]) -> bool:
    """Any Claude lane whose seat state the relay owns: pooled door or seat pin."""
    return is_relay_pooled(provider) or is_relay_pinned(provider)
