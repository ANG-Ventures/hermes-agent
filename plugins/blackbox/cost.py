"""Cost reconciliation for blackbox turn telemetry."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost


_STATUS_RANK = {
    "included": 0,
    "actual": 1,
    "estimated": 1,
    "partial": 2,
    "unknown": 3,
}


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


_PERCLASS_NONE: dict[str, float | None] = {
    "uncached": None, "cache_read": None, "cache_write": None, "output": None,
}


def compute_turn_cost(
    model: str,
    provider: Optional[str],
    base_url: Optional[str],
    calls: list[dict],
) -> tuple[float | None, str, dict[str, float | None]]:
    """Price each provider call and return reconciled turn cost/status/split.

    Returns ``(total_usd, status, perclass)`` where ``perclass`` is a dict
    ``{uncached, cache_read, cache_write, output}`` of floats summing to
    ``total_usd`` for a cleanly-priced turn. For ``partial`` and ``unknown``
    turns the split is deliberately withheld (all None, SPEC-C D-9): a partial
    total omits the unknown call(s), so a four-part split that summed to it
    would present a known-calls-only figure as the whole truth.
    """
    if not calls:
        return 0.0, "included", dict(_PERCLASS_NONE)

    # A logical MoA iteration contains several physical provider calls: one per
    # advisor plus the acting aggregator. The outer turn still carries the
    # virtual preset identity (provider="moa", model="default"), so price nested
    # calls at their own routes when the conversation loop supplies them.
    pricing_calls: list[dict] = []
    for call in calls:
        nested = call.get("pricing_calls") if isinstance(call, dict) else None
        # An explicitly empty list intentionally falls through to the outer
        # call: nonzero usage with missing physical attribution must remain
        # unknown, never be silently reclassified as included/$0.
        if isinstance(nested, list) and nested:
            pricing_calls.extend(item for item in nested if isinstance(item, dict))
        elif isinstance(call, dict):
            pricing_calls.append(call)

    if not pricing_calls:
        return 0.0, "included", dict(_PERCLASS_NONE)

    # M3 (SPEC §5D / INV-7): a turn whose every billed token class is zero is
    # costless, not unpriced. Return a concrete $0 split so the dashboard treats
    # it as priced; ANY nonzero class falls through to normal pricing.
    def _billed_tokens(call: dict) -> int:
        return (
            _int_value(call.get("input_tokens"))
            + _int_value(call.get("output_tokens"))
            + _int_value(call.get("cache_read_tokens"))
            + _int_value(call.get("cache_write_tokens"))
        )

    # UNKNOWN != 0: a call with any unmeasured token bucket has not been
    # shown to be costless, so it can never take the priced_zero fast path (its
    # zeroes are absence of data, not evidence of no spend).
    def _usage_unknown(call: dict) -> bool:
        from agent.usage_pricing import USAGE_UNKNOWN_FIELDS
        return any(bool(call.get(key)) for key in USAGE_UNKNOWN_FIELDS)

    if all(
        _billed_tokens(call) == 0 and not _usage_unknown(call)
        for call in pricing_calls
    ):
        return 0.0, "priced_zero", {
            "uncached": 0.0,
            "cache_read": 0.0,
            "cache_write": 0.0,
            "output": 0.0,
        }

    # A zero-token physical call is concretely $0 even if its route has no
    # pricing entry (for example a failed MoA advisor). It must not make an
    # otherwise fully-priced mixed-model turn look partial.
    # An UNMEASURED-output call is exempt: its zero is missing data, not a
    # measured nothing, so dropping it here would erase the unknown.
    pricing_calls = [
        call for call in pricing_calls
        if _billed_tokens(call) > 0 or _usage_unknown(call)
    ]

    known_total = Decimal("0")
    known_count = 0
    unknown_count = 0
    known_statuses: list[str] = []
    # per-class accumulators (engine vocab: input == uncached/fresh input)
    acc_input = Decimal("0")
    acc_output = Decimal("0")
    acc_cache_read = Decimal("0")
    acc_cache_write = Decimal("0")

    for call in pricing_calls:
        try:
            usage = CanonicalUsage(
                input_tokens=_int_value(call.get("input_tokens")),
                output_tokens=_int_value(call.get("output_tokens")),
                cache_read_tokens=_int_value(call.get("cache_read_tokens")),
                cache_write_tokens=_int_value(call.get("cache_write_tokens")),
                reasoning_tokens=_int_value(call.get("reasoning_tokens")),
                # UNKNOWN != 0: carry the discriminator into pricing so a call
                # whose output the provider never measured is refused, not
                # silently priced as if it produced nothing.
                output_tokens_unknown=bool(call.get("output_tokens_unknown")),
                input_tokens_unknown=bool(call.get("input_tokens_unknown")),
                cache_read_tokens_unknown=bool(call.get("cache_read_tokens_unknown")),
                cache_write_tokens_unknown=bool(call.get("cache_write_tokens_unknown")),
                usage_unknown=bool(call.get("usage_unknown")),
            )
            result = estimate_usage_cost(
                call.get("model") or model,
                usage,
                provider=call.get("provider") or provider,
                base_url=call.get("base_url") or base_url,
            )
        except Exception:
            unknown_count += 1
            continue

        amount = result.amount_usd
        status = result.status or "unknown"
        if amount is None or status == "unknown":
            unknown_count += 1
            continue

        known_total += Decimal(str(amount))
        known_count += 1
        known_statuses.append("estimated" if status == "actual" else status)
        # accumulate the per-class parts (Decimal("0") for an included/zero-cost
        # result; the `or Decimal("0")` guards the genuine None an unknown-route
        # early-return would produce — those calls don't reach here anyway).
        acc_input += result.cost_input_usd or Decimal("0")
        acc_output += result.cost_output_usd or Decimal("0")
        acc_cache_read += result.cost_cache_read_usd or Decimal("0")
        acc_cache_write += result.cost_cache_write_usd or Decimal("0")

    if unknown_count and known_count:
        # partial: total is known-calls-only; withhold the split (D-9).
        return float(known_total), "partial", dict(_PERCLASS_NONE)
    if unknown_count:
        return None, "unknown", dict(_PERCLASS_NONE)

    worst = max(known_statuses or ["included"], key=lambda s: _STATUS_RANK.get(s, 3))
    perclass: dict[str, float | None] = {
        "uncached": float(acc_input),
        "cache_read": float(acc_cache_read),
        "cache_write": float(acc_cache_write),
        "output": float(acc_output),
    }
    return float(known_total), worst, perclass
