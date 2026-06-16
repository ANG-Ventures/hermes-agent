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
- PRD-5 net-of-expansion accounting reports saved tokens/dollars NET of realized
  expansions. Gross view-savings are still carried as diagnostics.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

# Reuse the canonical resolver — NOT a second price table.
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
from plugins.native_content_slimmer.breaker import evaluate_expansion_window

# Mirror cost.py's status precedence so a mixed day reconciles identically.
_STATUS_RANK = {"included": 0, "actual": 1, "estimated": 1, "partial": 2, "unknown": 3}
DEFAULT_EXPANSION_EXTRA_TURN_TOKENS_EST = 256


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
    Negative ``saved_tokens`` represent net-negative expansion cost and price as
    a negative lower-bound amount when the model is known.
    """

    tokens = int(saved_tokens or 0)
    if not model:
        return None, "unknown"
    try:
        usage = CanonicalUsage(input_tokens=abs(tokens))
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
    sign = -1.0 if tokens < 0 else 1.0
    return sign * float(amount), status


def dollarize_rollup(
    events: Iterable[Mapping[str, Any]],
    *,
    expansion_extra_turn_tokens_est: int = DEFAULT_EXPANSION_EXTRA_TURN_TOKENS_EST,
) -> dict[str, Any]:
    """Roll up persisted savings rows into a dollarized, mode-split summary.

    Splits by action: ``replace`` (realized "saved") vs ``would_replace``
    (shadow "would have saved") — NEVER summed together. Each side carries
    saved tokens (vs raw + vs status-quo), saved USD (lower bound), and a
    reconciled price status. Dedupe by ``savings_key``.

    PRD-5 net accounting subtracts realized expansion cost per row:
    ``original_tokens_est + expansion_extra_turn_tokens_est`` for every realized
    expansion. The emitted/view tokens are already included in the gross saved
    delta, so only the recovery + extra-turn cost is subtracted here.
    """

    extra_turn = max(0, int(expansion_extra_turn_tokens_est or 0))
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

        gross_sq_tokens = _to_nonnegative_int(ev.get("saved_vs_status_quo_tokens_est"))
        gross_raw_tokens = _to_nonnegative_int(ev.get("saved_vs_raw_tokens_est"))
        gross_sq_bytes = _to_nonnegative_int(ev.get("saved_vs_status_quo_bytes"))
        gross_raw_bytes = _to_nonnegative_int(ev.get("saved_vs_raw_bytes"))
        original_tokens = _tokens_from_event(ev, "original_tokens_est", "original_bytes")
        original_bytes = _to_nonnegative_int(ev.get("original_bytes"))
        expansions = 1 if _to_nonnegative_int(ev.get("expansions_triggered")) > 0 else 0
        expansion_cost_tokens = expansions * (original_tokens + extra_turn)
        expansion_cost_bytes = expansions * (original_bytes + extra_turn * 4)
        net_sq_tokens = gross_sq_tokens - expansion_cost_tokens
        net_raw_tokens = gross_raw_tokens - expansion_cost_tokens
        net_sq_bytes = gross_sq_bytes - expansion_cost_bytes
        net_raw_bytes = gross_raw_bytes - expansion_cost_bytes

        bucket["event_count"] += 1
        bucket["expansion_count"] += expansions
        bucket["gross_saved_vs_status_quo_tokens_est"] += gross_sq_tokens
        bucket["gross_saved_vs_raw_tokens_est"] += gross_raw_tokens
        bucket["gross_saved_vs_status_quo_bytes"] += gross_sq_bytes
        bucket["gross_saved_vs_raw_bytes"] += gross_raw_bytes
        bucket["expansion_cost_tokens_est"] += expansion_cost_tokens
        bucket["expansion_cost_bytes"] += expansion_cost_bytes
        bucket["saved_vs_status_quo_tokens_est"] += net_sq_tokens
        bucket["saved_vs_raw_tokens_est"] += net_raw_tokens
        bucket["saved_vs_status_quo_bytes"] += net_sq_bytes
        bucket["saved_vs_raw_bytes"] += net_raw_bytes

        lane = _lane_bucket(bucket, ev)
        lane["event_count"] += 1
        lane["expansion_count"] += expansions
        lane["samples"].append(bool(expansions))
        lane["gross_saved_vs_status_quo_tokens_est"] += gross_sq_tokens
        lane["saved_vs_status_quo_tokens_est"] += net_sq_tokens
        lane["expansion_cost_tokens_est"] += expansion_cost_tokens

        # Per-row pricing at the row's own model (D-7), now on net tokens.
        usd_sq, status = price_saved_tokens(
            net_sq_tokens,
            model=ev.get("model"),
            provider=ev.get("provider"),
            base_url=ev.get("base_url"),
        )
        usd_raw, _ = price_saved_tokens(
            net_raw_tokens,
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
        "expansion_count": 0,
        "gross_saved_vs_status_quo_tokens_est": 0,
        "gross_saved_vs_raw_tokens_est": 0,
        "gross_saved_vs_status_quo_bytes": 0,
        "gross_saved_vs_raw_bytes": 0,
        "expansion_cost_tokens_est": 0,
        "expansion_cost_bytes": 0,
        "saved_vs_status_quo_tokens_est": 0,
        "saved_vs_raw_tokens_est": 0,
        "saved_vs_status_quo_bytes": 0,
        "saved_vs_raw_bytes": 0,
        "_known_usd_sq": Decimal("0"),
        "_known_usd_raw": Decimal("0"),
        "_known_count": 0,
        "_unpriced": 0,
        "_statuses": [],
        "_lanes": {},
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
        "expansion_count": b["expansion_count"],
        "expansion_rate": (b["expansion_count"] / b["event_count"]) if b["event_count"] else 0.0,
        "gross_saved_vs_status_quo_tokens_est": b["gross_saved_vs_status_quo_tokens_est"],
        "gross_saved_vs_raw_tokens_est": b["gross_saved_vs_raw_tokens_est"],
        "gross_saved_vs_status_quo_bytes": b["gross_saved_vs_status_quo_bytes"],
        "gross_saved_vs_raw_bytes": b["gross_saved_vs_raw_bytes"],
        "expansion_cost_tokens_est": b["expansion_cost_tokens_est"],
        "expansion_cost_bytes": b["expansion_cost_bytes"],
        "saved_vs_status_quo_tokens_est": b["saved_vs_status_quo_tokens_est"],
        "saved_vs_raw_tokens_est": b["saved_vs_raw_tokens_est"],
        "saved_vs_status_quo_bytes": b["saved_vs_status_quo_bytes"],
        "saved_vs_raw_bytes": b["saved_vs_raw_bytes"],
        "saved_usd_vs_status_quo": usd_sq,
        "saved_usd_vs_raw": usd_raw,
        "price_status": price_status,
        "unpriced_count": unpriced,
        "lanes": _finalize_lanes(b["_lanes"]),
    }


def _lane_bucket(bucket: dict[str, Any], ev: Mapping[str, Any]) -> dict[str, Any]:
    tool_name = str(ev.get("tool_name") or "unknown_tool")
    content_class = str(ev.get("content_class") or ev.get("lane") or "unknown")
    strategy = str(ev.get("strategy") or "lossless")
    key = (tool_name, content_class, strategy)
    lanes = bucket["_lanes"]
    if key not in lanes:
        lanes[key] = {
            "tool_name": tool_name,
            "content_class": content_class,
            "strategy": strategy,
            "event_count": 0,
            "expansion_count": 0,
            "samples": [],
            "gross_saved_vs_status_quo_tokens_est": 0,
            "saved_vs_status_quo_tokens_est": 0,
            "expansion_cost_tokens_est": 0,
        }
    return lanes[key]


def _finalize_lanes(lanes: Mapping[tuple[str, str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(lanes):
        lane = lanes[key]
        state = evaluate_expansion_window(
            lane["samples"],
            lane="|".join(key),
        )
        out.append(
            {
                "tool_name": lane["tool_name"],
                "content_class": lane["content_class"],
                "strategy": lane["strategy"],
                "event_count": lane["event_count"],
                "sample_count": state.sample_count,
                "expansion_count": lane["expansion_count"],
                "expansion_rate": state.expansion_rate,
                "gross_saved_vs_status_quo_tokens_est": lane["gross_saved_vs_status_quo_tokens_est"],
                "saved_vs_status_quo_tokens_est": lane["saved_vs_status_quo_tokens_est"],
                "expansion_cost_tokens_est": lane["expansion_cost_tokens_est"],
                "breaker_action": state.action,
                "breaker_tripped": state.tripped,
                "breaker_reason": state.reason,
            }
        )
    return out


def _tokens_from_event(ev: Mapping[str, Any], token_field: str, byte_field: str) -> int:
    if ev.get(token_field) is not None:
        return _to_nonnegative_int(ev.get(token_field))
    return (_to_nonnegative_int(ev.get(byte_field)) + 3) // 4


def _to_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
