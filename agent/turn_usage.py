"""Per-response usage accounting for the conversation turn loop.

After every successful model API call, ``record_response_usage`` folds ``response.usage``
into: the context compressor (``update_from_response`` + the compression-budget rearm
latch), the usage anchor for display/compression math, per-session token/cost counters,
the state.db token-delta queue, and the observability log line. MoA sessions additionally
fold advisor fan-out usage into the reported counts and price the aggregator at its REAL
model/provider. Logger name stays ``agent.conversation_loop`` for caplog parity.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.image_token_cost import calibrate_from_usage
from agent.usage_anchor import set_usage_anchor
from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS, cache_stats_line, estimate_usage_cost, prompt_tokens_unknown,
    verbose_token_usage_log_args, with_served_service_tier,
)

logger = logging.getLogger("agent.conversation_loop")


def _agent_session_source(agent: Any) -> str:
    """The surface the agent's own row create would stamp (``_ensure_db_session``), so an
    accounting guard that wins the row-creation race never mints an anonymous session."""
    from run_agent import _session_source_for_agent  # late: run_agent imports this module
    return _session_source_for_agent(getattr(agent, "platform", None))


@dataclass
class ResponseUsageOutcome:
    """``compression_attempts`` is the (possibly rearmed-to-zero) budget counter;
    ``rearmed`` tells the loop to also clear its preflight-block latch."""

    compression_attempts: int
    rearmed: bool = False


def _loop_mod():
    """Lazy ``agent.conversation_loop`` import (avoids an import cycle)."""
    import agent.conversation_loop as _cl

    return _cl


def _fold_moa_usage(agent, canonical_usage):
    """MoA: fold advisor fan-out usage into REPORTED token counts (only aggregator usage is
    returned, so advisor spend would be invisible) and flush the full-turn trace when
    ``moa.save_traces`` is on. Returns ``(client, canonical_usage, advisor_cost, advisor_pricing_calls)``;
    the pricing calls ledger each physical advisor call under the composite row (card t_02323499)."""
    _moa_ref_cost = None
    _moa_ref_pricing_calls: list[dict[str, Any]] = []
    _moa_client = getattr(agent, "client", None)
    if _moa_client is not None and hasattr(_moa_client, "consume_reference_usage"):
        try:
            _ref_usage, _moa_ref_cost = _moa_client.consume_reference_usage()
            if _ref_usage is not None:
                canonical_usage = canonical_usage + _ref_usage
        except Exception as _moa_acct_exc:  # pragma: no cover - defensive
            logger.debug("MoA reference usage accounting failed: %s", _moa_acct_exc)
    if _moa_client is not None and hasattr(_moa_client, "consume_reference_pricing_calls"):
        try:
            _moa_ref_pricing_calls = _moa_client.consume_reference_pricing_calls() or []
        except Exception as _moa_pricing_exc:  # pragma: no cover - defensive
            logger.debug("MoA reference pricing-call accounting failed: %s", _moa_pricing_exc)
    if _moa_client is not None and hasattr(_moa_client, "consume_and_save_trace"):
        try:
            # Streaming path: pass the streamed acting text so the trace is self-contained.
            _agg_streamed_text = getattr(agent, "_current_streamed_assistant_text", "") or ""
            _moa_client.consume_and_save_trace(
                agent.session_id, aggregator_output_fallback=_agg_streamed_text or None
            )
        except Exception as _moa_trace_exc:  # pragma: no cover - defensive
            logger.debug("MoA trace flush failed: %s", _moa_trace_exc)
    return _moa_client, canonical_usage, _moa_ref_cost, _moa_ref_pricing_calls


def record_response_usage(
    agent: Any, response: Any, *, messages: List[Dict[str, Any]], api_call_count: int,
    api_duration: float, compression_attempts: int, max_compression_attempts: int,
    turn_calls: Optional[List[Dict[str, Any]]] = None, call_route: Optional[Dict[str, str]] = None,
    call_composition: Any = None, turn_id: Any = None,
) -> ResponseUsageOutcome:
    """Fold ``response.usage`` into compressor, anchors, session counters, state.db
    and the API-call log line (see module docstring). No-usage responses only
    consume a pending compaction verdict. Returns the loop-visible outcome.

    Fork (Blackbox): ``turn_calls`` is the loop's per-turn accumulator (``_LoopState._turn_calls``);
    every successful usage commit appends one dict priced at ``call_route`` — the route captured at
    DISPATCH (turn_api_call), so a mid-turn switch cannot misprice the call (t_0c5c3822).
    ``call_composition`` is the per-call request breakdown from turn_request_assembly. UNKNOWN != 0:
    the ``*_unknown`` flags ride beside the canonical ints so no persistence/display site presents an
    unmeasured 0 as a measurement (token-stats honesty)."""
    rearmed = False
    compressor = agent.context_compressor
    _loop = _loop_mod()
    _route = dict(call_route) if isinstance(call_route, dict) else {}
    _route.setdefault("provider", str(agent.provider or ""))
    _route.setdefault("model", str(agent.model or ""))
    _route.setdefault("base_url", str(agent.base_url or ""))
    # Taken BEFORE the increment: ``merge_session_cost_status`` must tell an UNSTARTED session (fresh
    # agent, zero calls) from an INCOMPLETE one, else the first priced call pins it to ``partial``.
    _prior_api_calls = agent.session_api_calls
    # Count every completed provider attempt, including providers that omit usage.
    agent.session_api_calls += 1
    # Consume this response's billed-response entry (accounted right below) and settle any OLDER
    # entry from this attempt the loop did not accept (a refused/malformed 200 is still spend).
    _loop._settle_unaccepted_billed_responses(agent, turn_calls, turn_id, committing=True)
    has_usage = bool(getattr(response, "usage", None))
    if not has_usage:
        if getattr(compressor, "awaiting_real_usage_after_compression", False):
            # No usage -> cannot adjudicate the prior compaction; consume the
            # pending verdict so later readings aren't charged to it and
            # preflight deferral isn't latched indefinitely.
            compressor.update_from_response({})
        _note_usage_less = getattr(compressor, "note_usage_less_response", None)
        if callable(_note_usage_less):
            _note_usage_less()

    # Commit per-call accounting for EVERY successful response, including one without a usage
    # payload: ``_canonical_usage_from_response`` yields an aggregate UNKNOWN (zeros + every flag),
    # so counters / ledger / persistence record that the call happened without presenting the zeros
    # as measurements. Compressor + anchor math below stays gated on REAL usage.
    canonical_usage = _loop._canonical_usage_from_response(
        response, provider=agent.provider, api_mode=agent.api_mode)
    if has_usage:
        canonical_usage = with_served_service_tier(canonical_usage, response)
    # Aggregator-only usage kept for pricing: advisor tokens are priced at each advisor's
    # OWN model rate and added as dollars below.
    aggregator_usage = canonical_usage
    _moa_client, canonical_usage, _moa_ref_cost, _moa_ref_pricing_calls = _fold_moa_usage(agent, canonical_usage)
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = canonical_usage.total_tokens
    # UNKNOWN != 0: the provider explicitly declined to measure (CanonicalUsage.*_unknown). The ints
    # stay 0 so arithmetic consumers keep working; the flags ride alongside.
    output_unknown = bool(canonical_usage.output_tokens_unknown)
    usage_flags = {key: bool(getattr(canonical_usage, key)) for key in USAGE_UNKNOWN_FIELDS}
    # Canonical token + cache buckets for context engines; legacy keys stay for back-compat.
    usage_dict = {
        **usage_flags,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": canonical_usage.reasoning_tokens,
    }
    # Capture the boundary latch before update_from_response() consumes it: only the real
    # prompt count right after a compaction rearms the budget.
    _completed_compaction_pending = bool(
        getattr(compressor, "_verify_compaction_cleared_threshold", False)
    )
    if has_usage:
        compressor.update_from_response(usage_dict)
    # Usage-anchored accounting: snapshot exact provider usage against the durable
    # transcript (main-loop ONLY; MoA uses pre-fold aggregator usage). The display meter
    # anchors on the turn's FIRST response: later same-turn responses inflate
    # prompt_tokens with replayed thinking. Display-only; compression math uses real usage.
    # The provider just priced this request exactly: if the delta since the previous anchor
    # introduced images, the residual is their real per-image cost (learned before re-anchoring).
    if has_usage:
        calibrate_from_usage(agent, messages, aggregator_usage.prompt_tokens)
    # An anchor is exact only when BOTH prompt and completion are measured: installing an UNKNOWN
    # placeholder as zero would make anchored_context_tokens skip the reply as "already counted".
    _new_anchor = _loop._capture_measured_usage_anchor(aggregator_usage, messages)
    if _new_anchor is not None:
        set_usage_anchor(agent, _new_anchor, turn_base=api_call_count == 1)
    _compression_threshold = int(getattr(compressor, "threshold_tokens", 0) or 0)
    if has_usage and _loop._should_rearm_compression_budget(
        compression_attempts, completed_compaction_pending=_completed_compaction_pending,
        prompt_tokens=prompt_tokens, threshold_tokens=_compression_threshold,
    ):
        logger.info(
            "Compression budget rearmed after provider-confirmed "
            "recovery: prompt=%s < threshold=%s (attempts were %s/%s)",
            f"{prompt_tokens:,}",
            f"{_compression_threshold:,}",
            compression_attempts,
            max_compression_attempts,
        )
        compression_attempts = 0
        # Confirmed recovery also clears the loop's stale insufficient-progress verdict
        # (``_preflight_compression_blocked``), else a later pressure spike grows unchecked.
        rearmed = True

    # Stash canonical usage for on_turn_complete(); keep the latest call's.
    agent._last_turn_usage = dict(usage_dict)
    # The parent's CURRENT prompt size for headroom math (delegate summary budgets): the
    # aggregator's own prompt, never the MoA-folded total (advisor prompts are not in this context).
    agent._last_prompt_size_tokens = int(aggregator_usage.prompt_tokens or 0)

    # Persist only provider-confirmed context lengths, not probe tiers.
    if has_usage and getattr(compressor, "_context_probed", False):
        ctx = compressor.context_length
        if getattr(compressor, "_context_probe_persistable", False):
            from agent.model_metadata import save_provider_context_length

            save_provider_context_length(agent.model, agent.base_url, ctx, agent.provider)
            agent._safe_print(f"{agent.log_prefix}💾 Cached context length: {ctx:,} tokens for {agent.model}")
        compressor._context_probed = False
        compressor._context_probe_persistable = False

    _loop._record_served_service_tier(agent, response)
    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical_usage.input_tokens
    agent.session_output_tokens += canonical_usage.output_tokens
    agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
    agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
    agent.session_reasoning_tokens += canonical_usage.reasoning_tokens
    # ABSORBING per-bucket session unknown latches, in lockstep with the counters above: a plain int
    # total leaves no trace of an unmeasured call; once a session has missed a measurement no later
    # call restores it (cleared with the counters in reset_session_state).
    for _flag, _set in usage_flags.items():
        if _set:
            setattr(agent, f"session_{_flag}", True)
    # Last successful provider-call snapshot for /context, /usage (cumulative counters above).
    agent.last_turn_usage = {
        **usage_dict,
        "last_composition": call_composition,
        "output_tokens_unknown": output_unknown,
    }
    # Blackbox per-TURN accumulator. INVARIANT: this append lives INSIDE the successful-usage commit
    # block — the same place that mutates session_*_tokens — never in a retry/exception branch, or a
    # retried 5xx double-counts. ``turn_calls`` is the loop's LOCAL list, never an agent attribute,
    # so concurrent subagent frames cannot stomp each other.
    _turn_call = None
    if turn_calls is not None:
        try:
            turn_calls.append({
                **usage_dict,
                "output_tokens_unknown": output_unknown,
                "latency_s": api_duration,
                "composition": call_composition,
                "provider": _route["provider"],
                "model": _route["model"],
                "base_url": _route["base_url"],
            })
            _turn_call = turn_calls[-1]
        except Exception:
            pass  # telemetry must never break the conversation loop
    # Rolling history for status-bar averages (last 10). An unmeasured output is not 0 tok/s: the two
    # deques are divided sum/sum with no UNKNOWN discriminator, so skip the pair for an unmeasured call.
    with suppress(Exception):
        if not (output_unknown or canonical_usage.usage_unknown):
            hist = getattr(agent, "_api_latency_history", None)
            if hist is not None:
                hist.append(float(api_duration))
            ohist = getattr(agent, "_api_output_history", None)
            if ohist is not None:
                ohist.append(int(canonical_usage.output_tokens or 0))

    _prompt_unknown = prompt_tokens_unknown(canonical_usage)
    _cache_pct = ""
    if canonical_usage.cache_read_tokens and prompt_tokens and not _prompt_unknown:
        _cache_pct = f" cache={canonical_usage.cache_read_tokens}/{prompt_tokens} ({100*canonical_usage.cache_read_tokens/prompt_tokens:.0f}%)"
    # write= is the money (cache writes cost 50x a read); id= is what a provider needs to look the
    # request up; upstream= is who actually served it when the route reports that (OpenRouter's
    # `provider`). Diagnosing the 1,393-agent run's cache misses took a DB join and a live probe
    # because none of the three were on this line.
    if canonical_usage.cache_write_tokens:
        _cache_pct += f" write={canonical_usage.cache_write_tokens}"
    _rid = getattr(response, "id", None)
    _ident = f" id={_rid}" if isinstance(_rid, str) and _rid else ""
    _upstream = getattr(response, "provider", None)
    if isinstance(_upstream, str) and _upstream:
        _ident += f" upstream={_upstream}"
    # UNKNOWN != 0: ``out=0`` reads as a dead round-trip; say ``out=unknown``.
    logger.info(
        "API call #%d: model=%s provider=%s in=%s out=%s total=%s latency=%.1fs%s%s",
        agent.session_api_calls, _route["model"], _route["provider"] or "unknown",
        "unknown" if _prompt_unknown else prompt_tokens,
        "unknown" if output_unknown else completion_tokens,
        "unknown" if canonical_usage.total_tokens_unknown else total_tokens,
        api_duration, _cache_pct, _ident,
    )
    # nous.anthropic_wire=auto: the session's wire is decided once, from this first response.
    if agent.session_api_calls == 1 and (agent.provider or "") == "nous":
        with suppress(Exception):
            from agent.nous_wire import maybe_switch_wire_after_first_response
            maybe_switch_wire_after_first_response(agent, response, agent.session_api_calls)

    # MoA: agent.model/provider are the virtual preset/"moa" with no pricing entry, silently
    # dropping aggregator spend. Price at the REAL model/provider from the aggregator slot.
    _agg_cost_model = _route["model"]
    _agg_cost_provider = _route["provider"] or None
    _agg_cost_base_url = _route["base_url"] or None
    # Only the real MoA client carries a resolved aggregator slot (a plain dict); a MagicMock
    # synthesizes every attribute, so gate on the slot actually being a dict.
    _agg_slot_raw = getattr(_moa_client, "last_aggregator_slot", None) if _moa_client is not None else None
    _agg_slot = _agg_slot_raw if isinstance(_agg_slot_raw, dict) else None
    if _agg_slot and _agg_slot.get("model"):
        _agg_cost_model = _agg_slot["model"]
        _agg_cost_provider = _agg_slot.get("provider") or _route["provider"] or None
        _agg_cost_base_url = _agg_slot.get("base_url") or _route["base_url"] or None
        try:
            if _turn_call is not None:
                _turn_call["pricing_calls"] = _loop._build_moa_pricing_calls(
                    _moa_ref_pricing_calls, aggregator_usage, aggregator_model=_agg_cost_model,
                    aggregator_provider=_agg_cost_provider, aggregator_base_url=_agg_cost_base_url,
                )
                # Ledger each physical advisor/aggregator call as a child of this composite call's
                # virtual turn_api_calls row (card t_02323499).
                from agent.chat_completion_helpers import _emit_composite_api_call_records

                _moa_preset = getattr(
                    getattr(getattr(_moa_client, "chat", None), "completions", None), "preset_name", None,
                )
                _emit_composite_api_call_records(
                    agent, _turn_call["pricing_calls"],
                    sub_harness=f"moa:{_moa_preset if isinstance(_moa_preset, str) and _moa_preset else _route['model']}",
                )
        except Exception:
            pass  # telemetry must never break the conversation loop
    cost_result = estimate_usage_cost(
        _agg_cost_model, aggregator_usage, provider=_agg_cost_provider,
        base_url=_agg_cost_base_url, api_key=getattr(agent, "api_key", ""),
    )
    _cost_status = _loop._session_cost_status_with_known_spend(
        _loop._moa_session_cost_status(cost_result, _moa_ref_pricing_calls, _moa_ref_cost),
        session_cost_usd=agent.session_estimated_cost_usd,
    )
    # Cost delta = aggregator + MoA advisor cost (already priced per-advisor at each
    # advisor's own model rate), so state.db's estimated_cost_usd matches the folded
    # token counts.
    _cost_delta = None
    if cost_result.amount_usd is not None:
        _cost_delta = float(cost_result.amount_usd)
        agent.session_estimated_cost_usd += _cost_delta
    if _moa_ref_cost is not None:
        try:
            _moa_cost = float(_moa_ref_cost)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            _moa_cost = None
        if _moa_cost is not None:
            agent.session_estimated_cost_usd += _moa_cost
            _cost_delta = (_cost_delta or 0.0) + _moa_cost
    agent.session_cost_status = _loop.merge_session_cost_status(
        getattr(agent, "session_cost_status", None), _cost_status, prior_api_calls=_prior_api_calls,
    )
    agent.session_cost_source = cost_result.source

    # Persist per-call token deltas for any session_id so non-CLI runs can't lose
    # accounting; gateway/session-store writes use absolute totals and safely overwrite
    # these deltas. Enqueued, not written (a cold state.db UPDATE here stalled the tool
    # loop); drained at finalize via _persist_session.
    if agent._session_db and agent.session_id:
        try:
            # Ensure the row exists: under concurrent SQLite load the initial
            # _ensure_db_session() may fail, and UPDATE on a missing row affects 0 rows.
            if not agent._session_db_created:
                agent._ensure_db_session()
            agent._session_db.queue_token_counts(
                agent.session_id,
                source=_agent_session_source(agent),
                input_tokens=canonical_usage.input_tokens,
                output_tokens=canonical_usage.output_tokens,
                cache_read_tokens=canonical_usage.cache_read_tokens,
                cache_write_tokens=canonical_usage.cache_write_tokens,
                reasoning_tokens=canonical_usage.reasoning_tokens,
                estimated_cost_usd=_cost_delta,
                cost_status=_cost_status,
                cost_source=cost_result.source,
                billing_provider=_route["provider"] or None,
                billing_base_url=_route["base_url"] or None,
                billing_mode="subscription_included"
                if cost_result.status == "included" else None,
                model=_route["model"],
                api_call_count=1,
                **_loop._last_turn_snapshot_kwargs(canonical_usage),
                # UNKNOWN != 0: the cumulative flags are ABSORBING in the store; the last_turn_* ones
                # move WITH the snapshot they qualify (withheld -> None -> COALESCE keeps the prior split).
                **usage_flags,
                **{
                    f"last_turn_{key}": (None if bool(canonical_usage.total_tokens_unknown) else value)
                    for key, value in usage_flags.items()
                },
            )
        except Exception as e:  # silent loss here undercounts analytics
            logger.debug(
                "Token persistence failed (session=%s, tokens=%d): %s",
                agent.session_id, total_tokens, e,
            )

    if agent.verbose_logging:
        logging.debug(
            "Token usage: prompt=%s, completion=%s, total=%s",
            *verbose_token_usage_log_args(canonical_usage, prompt_tokens, completion_tokens, total_tokens),
        )

    # Report cache stats for any provider that returns ``prompt_tokens_details.cached_tokens``,
    # not only when we inject cache_control markers (UNKNOWN-aware rendering).
    cache_line = cache_stats_line(canonical_usage, usage_dict["prompt_tokens"])
    if cache_line and not agent.quiet_mode:
        agent._vprint(f"{agent.log_prefix}   {cache_line}")
    return ResponseUsageOutcome(compression_attempts=compression_attempts, rearmed=rearmed)
