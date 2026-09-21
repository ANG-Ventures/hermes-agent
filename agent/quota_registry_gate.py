"""Registry-aware pruning of the provider fallback chain.

The fallback walker in ``agent.chat_completion_helpers`` advances one chain
entry per failure, and each attempt costs a full request round-trip plus one
user-visible "switching to fallback provider..." status line. When a whole
pool of Claude subscriptions is quota-exhausted, that is a ~60 s march through
providers whose exhaustion is already recorded on disk by the usage-tracking
system (``var/usage-portal/site/usage.json``).

This module turns that published snapshot into a single, conservative
eligibility verdict per chain entry. The invariants that matter:

* **Only measured exhaustion prunes.** A window must be explicitly ``rejected``
  AND carry a parseable reset time further out than :data:`SKIP_HORIZON_SECONDS`.
* **Absence of data is never evidence.** Missing file, corrupt JSON, unknown
  provider, stale observation, rejection with no reset — all fail OPEN, leaving
  the historical walk exactly as it was.
* **Scoped (per-model) allowances do not gate the subscription.** A rejected
  Fable allowance means "no Fable", not "this sub is dead"; see the
  ``usage-tracking`` skill.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# A sub that comes back inside this window is still worth one request — the
# reset may land mid-turn, and pruning it would forfeit a usable provider.
SKIP_HORIZON_SECONDS = 5 * 60

# An observation older than this tells us about a quota window that has very
# likely already rolled over. Treat it as unknown rather than as exhaustion.
MAX_OBSERVATION_AGE_SECONDS = 6 * 60 * 60

# Subscription-wide windows. Anything else (notably the scoped per-model
# allowances, which carry ``scoped: true``) describes a narrower budget and
# must not gate the whole provider.
_SUBSCRIPTION_WINDOW_KEYS = ("five_hour", "seven_day")

# The usage system's own vocabulary for "the provider refused this request".
_EXHAUSTED_STATUSES = frozenset({"rejected", "exhausted"})

_DEFAULT_SNAPSHOT_RELPATH = Path("var") / "usage-portal" / "site" / "usage.json"


@dataclass(frozen=True)
class QuotaState:
    """Eligibility verdict for one provider slug."""

    eligible: bool
    window: Optional[str] = None
    reset_at: Optional[float] = None


@dataclass
class PruneResult:
    """Outcome of one pruning pass over a fallback chain."""

    eligible: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[tuple] = field(default_factory=list)
    soonest_reset_at: Optional[float] = None

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def summary_line(self) -> Optional[str]:
        """The ONE line that replaces N 'switching to fallback…' lines."""
        n = self.skipped_count
        if not n:
            return None
        noun = "sub" if n == 1 else "subs"
        return (
            f"⚠️ Rate limited — skipping {n} exhausted {noun} "
            f"(quota registry) and switching to fallback provider..."
        )

    @property
    def soonest_reset_text(self) -> str:
        """Human-readable soonest reset, for the fail-fast terminal message."""
        return format_reset_delta(self.soonest_reset_at)


def format_reset_delta(reset_at: Optional[float], *, now: Optional[float] = None) -> str:
    """Render a reset timestamp as a compact relative duration."""
    if reset_at is None:
        return "an unknown time"
    remaining = reset_at - (time.time() if now is None else now)
    if remaining <= 0:
        return "now"
    if remaining < 90 * 60:
        return f"{max(1, int(round(remaining / 60)))}m"
    hours = remaining / 3600.0
    if hours < 48:
        return f"{hours:.1f}h".replace(".0h", "h")
    return f"{hours / 24:.1f}d".replace(".0d", "d")


def _coerce_epoch(value: Any) -> Optional[float]:
    """Parse a registry reset value (epoch seconds or ISO-8601) to epoch."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text) if text.replace(".", "", 1).isdigit() else None
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def provider_quota_state(
    provider: str,
    snapshot: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> QuotaState:
    """Return the registry's verdict on ``provider``.

    Fails OPEN (``eligible=True``) on every form of missing or unusable data —
    the gate may only ever remove a provider the registry positively knows is
    exhausted for longer than the horizon.
    """
    if not snapshot or not isinstance(snapshot, dict):
        return QuotaState(eligible=True)
    account = snapshot.get((provider or "").strip())
    if not isinstance(account, dict):
        return QuotaState(eligible=True)

    now = time.time() if now is None else now

    observed_at = _coerce_epoch(account.get("observed_at"))
    if observed_at is not None and (now - observed_at) > MAX_OBSERVATION_AGE_SECONDS:
        return QuotaState(eligible=True)

    for window in account.get("windows") or []:
        if not isinstance(window, dict) or window.get("scoped"):
            continue
        if window.get("key") not in _SUBSCRIPTION_WINDOW_KEYS:
            continue
        if str(window.get("status") or "").strip().lower() not in _EXHAUSTED_STATUSES:
            continue
        reset_at = _coerce_epoch(window.get("resets_at"))
        if reset_at is None:
            # Exhausted but we don't know when it returns — not actionable.
            continue
        if (reset_at - now) > SKIP_HORIZON_SECONDS:
            return QuotaState(
                eligible=False, window=window.get("key"), reset_at=reset_at
            )
    return QuotaState(eligible=True)


def prune_exhausted_entries(
    chain: Optional[List[Dict[str, Any]]],
    *,
    snapshot: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> PruneResult:
    """Drop chain entries the registry knows are exhausted, in ONE pass.

    Order of the surviving entries is preserved. When every entry is pruned,
    ``soonest_reset_at`` names when the first one returns so the caller can
    fail fast with a useful time instead of walking a dead chain.
    """
    result = PruneResult()
    resets: List[float] = []
    for entry in chain or []:
        if not isinstance(entry, dict):
            result.eligible.append(entry)
            continue
        provider = (entry.get("provider") or "").strip()
        if not provider:
            # Invalid entries keep their existing downstream skip path.
            result.eligible.append(entry)
            continue
        state = provider_quota_state(provider, snapshot, now=now)
        if state.eligible:
            result.eligible.append(entry)
            continue
        result.skipped.append((provider, state))
        if state.reset_at is not None:
            resets.append(state.reset_at)
    if not result.eligible and resets:
        result.soonest_reset_at = min(resets)
    return result


def apply_quota_gate(
    agent,
    *,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[PruneResult]:
    """Prune the agent's UNWALKED fallback entries once per turn.

    Only the tail from ``_fallback_index`` onward is considered: the consumed
    prefix must keep its length or the index stops meaning what the walker
    thinks it means.

    Returns the :class:`PruneResult` on the pass that actually ran, or ``None``
    when the gate already ran this turn (or has nothing to act on). Stamps
    ``_quota_gate_soonest_reset_text`` when the whole tail was pruned so the
    caller can fail fast with a real time.
    """
    if getattr(agent, "_quota_gate_applied", False):
        return None
    agent._quota_gate_applied = True

    chain = getattr(agent, "_fallback_chain", None) or []
    index = int(getattr(agent, "_fallback_index", 0) or 0)
    tail = chain[index:]
    if not tail:
        return None

    if snapshot is None:
        snapshot = getattr(agent, "_quota_registry_snapshot", None)
    if snapshot is None:
        snapshot = load_registry_snapshot()

    result = prune_exhausted_entries(tail, snapshot=snapshot)
    if not result.skipped_count:
        return result

    agent._fallback_chain = list(chain[:index]) + list(result.eligible)
    agent._quota_gate_summary_line = result.summary_line
    agent._quota_gate_skipped_count = result.skipped_count
    if result.soonest_reset_at is not None:
        agent._quota_gate_soonest_reset_text = result.soonest_reset_text
    logger.info(
        "Quota gate: pruned %d exhausted fallback entries (%s)",
        result.skipped_count,
        ", ".join(p for p, _ in result.skipped),
    )
    return result


def rate_limited_status_line(agent) -> str:
    """Return the ONE status line for a quota failover, applying the gate.

    Replaces the bare ``"⚠️ Rate limited — switching to fallback provider..."``
    at the conversation loop's quota-failover site. When the registry pruned
    entries this turn, the line names the count instead of the walker emitting
    one line per dead sub.
    """
    default = "⚠️ Rate limited — switching to fallback provider..."
    try:
        result = apply_quota_gate(agent)
    except Exception:
        logger.debug("quota registry gate failed open", exc_info=True)
        return default
    if result is None or not result.skipped_count:
        return default
    return result.summary_line or default


def quota_exhausted_chain_message(agent) -> Optional[str]:
    """Name when the soonest pruned sub returns, for a fail-fast terminal."""
    text = getattr(agent, "_quota_gate_soonest_reset_text", None)
    if not text:
        return None
    return (
        f"Every fallback subscription is quota-exhausted per the usage "
        f"registry; the soonest resets in {text}."
    )


def default_snapshot_path() -> Path:
    """Path to the usage system's published snapshot under the Hermes home."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / _DEFAULT_SNAPSHOT_RELPATH
    except Exception:  # pragma: no cover - defensive
        return Path.home() / ".hermes" / _DEFAULT_SNAPSHOT_RELPATH


def load_registry_snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load ``provider_slug -> account`` from the published usage payload.

    Returns an empty mapping on any read/parse problem: the gate must degrade
    to the historical walk rather than to a wrong skip.
    """
    target = Path(path) if path is not None else default_snapshot_path()
    try:
        raw = json.loads(target.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("usage registry snapshot unreadable: %s", target, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for provider in raw.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        for account in provider.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            slug = (account.get("provider_slug") or "").strip()
            if slug:
                out[slug] = account
    return out
