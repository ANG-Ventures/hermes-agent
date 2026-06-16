"""Dollarization + multi-model reconciliation for native-slimmer savings (PRD #1.5 Phase 3).

NET-NEW code (pass-2 #3): the existing ``rollup_native_slimmer_events`` sums
bytes/tokens only — it has no pricing. This module prices each persisted row at
its OWN ``(model, provider, base_url)`` via ``agent.usage_pricing.estimate_usage_cost``
(the same resolver ``blackbox/cost.py`` uses — no second price table), and
reconciles a multi-model day with the same ``partial``/``unknown`` semantics as
``cost.py::compute_turn_cost``.

Honesty contract (D-2c, C-2):
- ``saved_usd`` is a per-submission LOWER BOUND priced at the uncached input rate;
  realized savings are typically larger. It is display-grade ONLY, never a decision
  input.
- Rows dedupe by ``savings_key`` (defense-in-depth atop the storage-layer UNIQUE).
- A row whose model is off-table prices to ``unknown`` and is counted as unpriced;
  the aggregate is ``partial`` (sum the known, note "+N unpriced"), never the whole
  day rendered "—".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

# Reuse the canonical resolver — NOT a second price table.
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

# Mirror cost.py's status precedence so a mixed day reconciles identically.
_STATUS_RANK = {"included": 0, "actual": 1, "estimated": 1, "partial": 2, "unknown": 3}


def price_saved_tokens(
    saved_tokens: int,
    *,
    model: str | None,
    provider: str | None = None,
    base_url: str | None = None,
) -> tuple[float | None, str]:
    """Price saved tokens as uncached INPUT tokens at the row's model rate.

    Returns ``(amount_usd_or_None, status)`` where status is one of
    ``included``/``estimated``/``unknown`` (mirrors the resolver). No model ⇒
    ``unknown`` (renders "—"); subscription model ⇒ ``0.0`` + ``included``.
    """

    tokens = max(0, int(saved_tokens or 0))
    if not model:
        return None, "unknown"
    try:
        usage = CanonicalUsage(input_tokens=tokens)
        result = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)
    except Exception:
        return None, "unknown"
    amount = result.amount_usd
    status = result.status or "unknown"
    if amount is None or status == "unknown":
        return None, "unknown"
    # An input-only synthetic usage is an ESTIMATE, never billed "actual".
    if status == "actual":
        status = "estimated"
    return float(amount), status


def dollarize_rollup(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll up persisted savings rows into a dollarized, mode-split summary.

    Splits by action: ``replace`` (realized "saved") vs ``would_replace``
    (shadow "would have saved") — NEVER summed together. Each side carries
    saved tokens (vs raw + vs status-quo), saved USD (lower bound), and a
    reconciled price status. Dedupe by ``savings_key``.
    """

    seen: set[str] = set()
    buckets = {
        "replace": _new_bucket(),
        "would_replace": _new_bucket(),
    }

    for ev in events:
        key = str(ev.get("savings_key") or "")
        if not key:
            key = "|".join(
                str(ev.get(p) or "")
                for p in ("session_id", "tool_call_id", "raw_sha256", "artifact_id")
            )
        if key in seen:
            continue
        seen.add(key)

        action = str(ev.get("action") or "")
        bucket = buckets.get(action)
        if bucket is None:
            continue

        saved_sq_tokens = max(0, int(ev.get("saved_vs_status_quo_tokens_est") or 0))
        saved_raw_tokens = max(0, int(ev.get("saved_vs_raw_tokens_est") or 0))
        bucket["event_count"] += 1
        bucket["saved_vs_status_quo_tokens_est"] += saved_sq_tokens
        bucket["saved_vs_raw_tokens_est"] += saved_raw_tokens
        bucket["saved_vs_status_quo_bytes"] += max(0, int(ev.get("saved_vs_status_quo_bytes") or 0))
        bucket["saved_vs_raw_bytes"] += max(0, int(ev.get("saved_vs_raw_bytes") or 0))

        # Per-row pricing at the row's own model (D-7).
        usd_sq, status = price_saved_tokens(
            saved_sq_tokens,
            model=ev.get("model"),
            provider=ev.get("provider"),
            base_url=ev.get("base_url"),
        )
        usd_raw, _ = price_saved_tokens(
            saved_raw_tokens,
            model=ev.get("model"),
            provider=ev.get("provider"),
            base_url=ev.get("base_url"),
        )
        if usd_sq is None:
            bucket["_unpriced"] += 1
        else:
            bucket["_known_usd_sq"] += Decimal(str(usd_sq))
            bucket["_known_usd_raw"] += Decimal(str(usd_raw or 0))
            bucket["_known_count"] += 1
            bucket["_statuses"].append(status)

    return {
        "saved": _finalize_bucket(buckets["replace"]),
        "would_save": _finalize_bucket(buckets["would_replace"]),
    }


def _new_bucket() -> dict[str, Any]:
    return {
        "event_count": 0,
        "saved_vs_status_quo_tokens_est": 0,
        "saved_vs_raw_tokens_est": 0,
        "saved_vs_status_quo_bytes": 0,
        "saved_vs_raw_bytes": 0,
        "_known_usd_sq": Decimal("0"),
        "_known_usd_raw": Decimal("0"),
        "_known_count": 0,
        "_unpriced": 0,
        "_statuses": [],
    }


def _finalize_bucket(b: dict[str, Any]) -> dict[str, Any]:
    unpriced = b["_unpriced"]
    known = b["_known_count"]
    statuses = b["_statuses"]

    if known == 0 and unpriced == 0:
        usd_sq = 0.0
        usd_raw = 0.0
        price_status = "included"  # nothing to price ⇒ no cash
    elif known == 0:
        usd_sq = None  # everything unpriced
        usd_raw = None
        price_status = "unknown"
    else:
        usd_sq = float(b["_known_usd_sq"])
        usd_raw = float(b["_known_usd_raw"])
        if unpriced:
            price_status = "partial"
        else:
            price_status = max(statuses, key=lambda s: _STATUS_RANK.get(s, 3))

    return {
        "event_count": b["event_count"],
        "saved_vs_status_quo_tokens_est": b["saved_vs_status_quo_tokens_est"],
        "saved_vs_raw_tokens_est": b["saved_vs_raw_tokens_est"],
        "saved_vs_status_quo_bytes": b["saved_vs_status_quo_bytes"],
        "saved_vs_raw_bytes": b["saved_vs_raw_bytes"],
        "saved_usd_vs_status_quo": usd_sq,
        "saved_usd_vs_raw": usd_raw,
        "price_status": price_status,
        "unpriced_count": unpriced,
    }
