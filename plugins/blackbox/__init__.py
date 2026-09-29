"""Blackbox telemetry plugin hooks."""

from __future__ import annotations

import json
import math
import time
import uuid
import logging
from threading import Lock
from typing import Any

_PREVIEW_CHARS = 2000


def _preview(value: Any) -> str:
    """Compact string preview of a tool arg/result for the side table.

    The store scrubs secrets and truncates again before persisting; this just
    coerces dict/list/other into a bounded string so the hook stays cheap.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    return text[:_PREVIEW_CHARS]

from plugins.blackbox.card import render_card
from plugins.blackbox.cost import compute_turn_cost
from plugins.blackbox.record import TurnRecord
from plugins.blackbox import routing

logger = logging.getLogger(__name__)


_DEFAULTS = {
    "enabled": False,
    "alerts_enabled": True,
    "cost_alert_threshold_usd": 1.00,
    "always_card": False,
    "store_text": True,
    "record_subagents": True,
    "retention_days": 30,
    # Reserved (SPEC 2026-06-30 §5C / D-4 / OQ1): when a periodic auto-reprice
    # sweep lands, `reprice_enabled: true` will opt it in. NOT read yet — the
    # M2 reprice pass is operator-triggered via `/cost reprice [--apply]` today,
    # so this default is documented-but-inert on purpose (no theater: it becomes
    # load-bearing only when the sweep loop that reads it ships). Default off.
    "reprice_enabled": False,
    # Conversation prefix-stability guard (card t_c07124ab): fingerprint every
    # outbound request and post to #logs once per session when already-sent
    # history / system prompt / tools change between consecutive requests.
    "prefix_guard": True,
}

_lock = Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _field(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _cache_creation_tiers(usage: Any) -> tuple[int | None, int | None]:
    """Keep unreported cache tiers NULL rather than imputing them.

    Two wire shapes carry the split: Anthropic-native ``usage.cache_creation``
    (apx/apr, direct), and the OpenAI-shaped bpx/bpr bridge egress
    ``usage.prompt_tokens_details.cache_creation`` (same inner keys).
    """
    creation = _field(usage, "cache_creation")
    if not creation:
        details = _field(usage, "prompt_tokens_details")
        creation = _field(details, "cache_creation") if details is not None else None
    if not creation or isinstance(creation, (int, float, str)):
        return None, None
    def value(key: str) -> int | None:
        raw = _field(creation, key)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        # NaN/inf in the bridge's untyped nested dict would raise here and the
        # fail-open recorder would drop the WHOLE call row (C6, #978): an
        # unusable tier is unknown (NULL), the rest of the call still lands.
        return int(raw) if math.isfinite(raw) else None
    return value("ephemeral_5m_input_tokens"), value("ephemeral_1h_input_tokens")


def record_api_call(
    *,
    turn_id: str,
    seq: int,
    ts: float,
    provider: str,
    model: str,
    usage: Any,
    api_mode: str,
    sub_key: str | None,
    attribution: str,
    http_status: int | None,
    relay_synthetic: bool,
    route_id: str | None,
    cache_ttl_requested: str | None = None,
    call_id: str | None = None,
    api_kwargs: Any = None,
    session_key: str | None = None,
    prefix_reset: str | None = None,
    prefix_compare_across_turns: bool = True,
) -> None:
    """Persist one completion attempt when Blackbox is enabled.

    This is deliberately a thin, fail-loud plugin boundary. The transport
    caller owns fail-open handling so sequence-allocation or schema errors are
    visible in logs without changing inference behavior.

    ``api_kwargs`` / ``session_key`` / ``prefix_reset`` feed the prefix-
    stability guard AFTER the ledger row is durable; that observer is
    fail-open on its own so a guard defect can never cost a ledger row.
    """
    cfg = _config()
    if cfg is None:
        return
    from agent.usage_pricing import CanonicalUsage, normalize_usage
    from plugins.blackbox import store

    canonical = (
        usage
        if isinstance(usage, CanonicalUsage)
        else normalize_usage(usage, provider=provider, api_mode=api_mode)
        if usage is not None
        else CanonicalUsage(request_count=0)
    )
    tier_5m, tier_1h = _cache_creation_tiers(usage)
    store.insert_api_call(
        turn_id,
        seq,
        ts=ts,
        provider=provider,
        model=model,
        usage=canonical,
        sub_key=sub_key,
        attribution=attribution,
        http_status=http_status,
        relay_synthetic=relay_synthetic,
        route_id=route_id,
        cache_write_5m=tier_5m,
        cache_write_1h=tier_1h,
        cache_ttl_requested=cache_ttl_requested,
        call_id=call_id,
    )
    if turn_id in _provisional_turns:
        _refresh_provisional_turn(turn_id)
    if api_kwargs is not None and session_key:
        observe_request_prefix(
            cfg,
            session_key=session_key,
            turn_id=turn_id,
            seq=seq,
            ts=ts,
            provider=provider,
            model=model,
            api_mode=api_mode,
            api_kwargs=api_kwargs,
            cache_read=(
                canonical.cache_read_tokens
                if usage is not None
                and not canonical.cache_read_tokens_unknown
                else None
            ),
            reset=prefix_reset,
            prompt_tokens=(
                canonical.input_tokens + canonical.cache_read_tokens
                + canonical.cache_write_tokens
                if usage is not None and not (
                    canonical.input_tokens_unknown or canonical.cache_read_tokens_unknown
                    or canonical.cache_write_tokens_unknown)
                else None
            ),
            compare_across_turns=prefix_compare_across_turns,
        )


def record_composite_calls(
    *,
    turn_id: str,
    parent_seq: int,
    sub_harness: str,
    calls: list[dict[str, Any]],
) -> None:
    """Persist a composite (MoA) call's physical children when enabled.

    Thin, fail-loud boundary like ``record_api_call``; the caller
    (``chat_completion_helpers._emit_composite_api_call_records``) owns
    sequence allocation and fail-open handling. ``calls[*]["usage"]`` may be
    a CanonicalUsage or a pricing-call dict (token fields read by name).
    """
    if _config() is None:
        return
    from agent.usage_pricing import CanonicalUsage
    from plugins.blackbox import store

    rows = []
    for call in calls:
        usage = call.get("usage")
        if not isinstance(usage, CanonicalUsage):
            src = usage if isinstance(usage, dict) else call
            usage = CanonicalUsage(
                input_tokens=_int_value(src.get("input_tokens")),
                output_tokens=_int_value(src.get("output_tokens")),
                cache_read_tokens=_int_value(src.get("cache_read_tokens")),
                cache_write_tokens=_int_value(src.get("cache_write_tokens")),
                reasoning_tokens=_int_value(src.get("reasoning_tokens")),
            )
        rows.append({**call, "usage": usage})
    store.insert_composite_calls(
        turn_id, parent_seq, sub_harness=sub_harness, calls=rows,
    )


def _composite_physical_calls(turn_usage: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Physical (model, provider) calls nested under a composite turn's calls."""
    out: list[dict[str, Any]] = []
    for call in (turn_usage or {}).get("calls") or []:
        nested = call.get("pricing_calls") if isinstance(call, dict) else None
        if isinstance(nested, list):
            out.extend(item for item in nested if isinstance(item, dict))
    return out


def _observe_pricing_sentinel(record: Any, turn_usage: dict[str, Any] | None,
                              base_url: Any) -> None:
    """Feed the new-model sentinel the turn's REAL model identities.

    A composite (MoA) turn is recorded under a virtual preset identity
    (provider 'moa', model 'moa/<preset>') that no price table will ever
    know, so observing it would page #alerts for a model that does not
    exist. Observe each physical call's route instead, and only when the
    turn could not be fully priced (an unpriced physical call is what makes
    the composite 'unknown' or 'partial'); ``is_known_model`` filters routes
    that do have a rate.
    """
    from plugins.blackbox import sentinel

    if str(record.provider or "").strip().lower() != "moa":
        sentinel.observe_turn(
            record.model, record.provider, record.cost_status, record.cost_usd,
            base_url=base_url,
        )
        return
    if str(record.cost_status or "").strip().lower() not in ("unknown", "partial"):
        return
    seen: set[tuple[str, str]] = set()
    for call in _composite_physical_calls(turn_usage):
        key = (str(call.get("model") or ""), str(call.get("provider") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        sentinel.observe_turn(
            key[0], key[1], "unknown", None, base_url=call.get("base_url"),
        )


def record_fallback_event(row: dict[str, Any]) -> None:
    """Persist one harness route-change ledger row when Blackbox is enabled.

    Thin boundary like ``record_api_call``: the caller
    (``agent.fallback_events.record``) owns the fail-open handling.
    """
    if _config() is None:
        return
    from plugins.blackbox import store

    store.insert_fallback_event(row)


def observe_request_prefix(
    cfg: dict[str, Any],
    *,
    session_key: str,
    turn_id: str,
    seq: int,
    ts: float,
    provider: str,
    model: str,
    api_mode: str,
    api_kwargs: Any,
    cache_read: int | None,
    reset: str | None = None,
    alert_fn: Any = None,
    prompt_tokens: int | None = None,
    compare_across_turns: bool = True,
) -> dict[str, Any] | None:
    """Prefix-stability guard entrypoint (card t_c07124ab). NEVER raises.

    Fingerprints the outbound request, compares it with the session's
    previous request in the store, and on the session's first unexplained
    mutation dispatches ONE #logs notice on a daemon thread. Returns the
    store result (diagnostic; the caller ignores it) or None when disabled
    or failed. ``blackbox.prefix_guard: false`` switches the guard off.
    """
    try:
        if not bool(cfg.get("prefix_guard", True)):
            return None
        from plugins.blackbox import prefix_guard, store

        fingerprint = prefix_guard.fingerprint_request(api_kwargs)
        if fingerprint is None:
            return None
        import os

        result = store.record_prefix_check(
            session_key=session_key,
            turn_id=turn_id,
            seq=seq,
            ts=ts,
            pid=os.getpid(),
            provider=provider,
            model=model,
            api_mode=api_mode,
            fingerprint=fingerprint,
            cache_read=cache_read,
            reset=reset,
            allowlist=cfg.get("prefix_guard_allowlist"),
            prompt_tokens=prompt_tokens,
            compare_across_turns=compare_across_turns,
        )
        violations = result.get("violations") or []
        if violations:
            logger.warning(
                "blackbox prefix guard: session=%s turn=%s seq=%s %s (context=%s)",
                session_key, turn_id, seq,
                "; ".join(prefix_guard._describe(v) for v in violations),
                violations[0].get("context"),
            )
        if result.get("alert"):
            previous = result.get("previous") or {}
            body = prefix_guard.render_alert(
                profile=_profile_name(),
                provider=provider,
                model=model,
                session_key=session_key,
                turn_id=turn_id,
                seq=seq,
                violations=violations,
                messages_before=int(previous.get("messages") or 0),
                messages_after=len(fingerprint.get("messages") or []),
                cache_read_before=previous.get("cache_read"),
                cache_read_after=cache_read,
                suppressed=int(result.get("suppressed") or 0),
            )
            prefix_guard.dispatch_alert(body, alert_fn)
        return result
    except Exception:
        logger.warning("blackbox prefix guard failed", exc_info=True)
        return None



def _turn_id() -> str:
    return "turn_" + uuid.uuid4().hex


def _recover_provider_from_model(model: str, provider: str) -> tuple[str, str]:
    """Split a leaked composite ``<provider>/<model>`` model id when the provider
    column is empty, so the recorded ``(provider, model)`` pair matches how every
    correctly-recorded turn stores it.

    The fleet keys turns as a composite ``<provider>/<model>`` (config default
    ``claude-app/claude-opus-4-8``). Correctly-recorded turns arrive split — the
    ``model`` column bare, the lane in ``provider``. But a turn that reaches the
    recorder with the WHOLE composite in ``model`` AND an empty ``provider``
    (observed 2026-07-05: a $0/0-token desktop turn recorded as
    ``claude-api-proxy-f6/claude-haiku-4-5`` with no provider) becomes a phantom
    "Spend by model" row that never rolls up with its bare ``claude-haiku-4-5``
    form.

    This runs at the RECORDING boundary — it rewrites only the telemetry row, not
    any live inference state — so it is inference-safe by construction (unlike a
    fix in the model-resolution path, where a slash-bearing model on a custom or
    aggregator endpoint carries prompt-caching / routing meaning).

    Guarded tightly, recover ONLY when:
      * the provider column is empty (a correctly-split turn is untouched), AND
      * the model carries a ``<prefix>/<rest>`` shape, AND
      * ``prefix`` is a REAL, non-aggregator provider id (``PROVIDER_REGISTRY``
        minus ``_AGGREGATOR_PROVIDERS``).
    So a genuine aggregator/vendor slug that legitimately keeps its vendor prefix
    (``meta-llama/llama-3.1``, ``nous/hermes-4``) is left verbatim — those always
    record WITH a non-empty provider anyway (openrouter/nous/claude-pool).
    """
    prov = (provider or "").strip()
    mdl = (model or "").strip()
    if prov or "/" not in mdl:
        return provider, model
    prefix, _sep, rest = mdl.partition("/")
    prefix_norm = prefix.strip().lower()
    rest = rest.strip()
    if not prefix_norm or not rest:
        return provider, model
    try:
        from hermes_cli.model_normalize import _AGGREGATOR_PROVIDERS
        from hermes_cli.auth import PROVIDER_REGISTRY

        if prefix_norm in PROVIDER_REGISTRY and prefix_norm not in _AGGREGATOR_PROVIDERS:
            return prefix_norm, rest
    except Exception:
        pass
    return provider, model



def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _config() -> dict[str, Any] | None:
    try:
        # fork-parity: read through the canonical loader, not raw yaml. Upstream
        # added tests/hermes_cli/test_config_read_guard.py, which forbids raw
        # yaml.safe_load of config.yaml outside the allowlisted owner modules —
        # a raw read also misses the managed-scope overlay the loader applies.
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly() or {}
        block = data.get("blackbox")
        if not isinstance(block, dict):
            return None
        cfg = dict(_DEFAULTS)
        cfg.update(block)
        if not bool(cfg.get("enabled")):
            return None
        return cfg
    except Exception:
        return None


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _int_or_none_value(value: Any) -> int | None:
    """Like _int_value but preserves None (absent last-call split → SQL NULL)."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _session_state(session_id: str) -> dict[str, Any]:
    with _lock:
        return _sessions.setdefault(
            session_id or "", {"ts_start": time.time(), "tools": [], "tool_calls": []}
        )


def _on_session_start(session_id: str = "", **_: Any) -> None:
    try:
        if _config() is None:
            return
        with _lock:
            _sessions[session_id or ""] = {
                "ts_start": time.time(),
                "tools": [],
                "tool_calls": [],
            }
    except Exception:
        return


def _on_post_tool_call(
    tool_name: str = "",
    name: str = "",
    session_id: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> None:
    try:
        cfg = _config()
        if cfg is None:
            return
        tool = tool_name or name
        if not tool:
            return
        state = _session_state(session_id)
        with _lock:
            state.setdefault("tools", []).append(str(tool))
            if bool(cfg.get("store_text", True)):
                state.setdefault("tool_calls", []).append(
                    {
                        "name": str(tool),
                        "args_preview": _preview(args),
                        "result_preview": _preview(result),
                    }
                )
    except Exception:
        return


def _comp_get(usage: dict[str, Any], key: str) -> int | None:
    """Read one bucket from the final-call request composition (or None).

    ``usage["last_composition"]`` is the char/4 fixed-vs-non-fixed breakdown of
    the last API call (see agent.model_metadata.compose_request_breakdown).
    Returns None when composition is absent (old turns / capture failed) so the
    column stays NULL and renderers fall back instead of showing a fake 0.
    """
    comp = usage.get("last_composition")
    if not isinstance(comp, dict):
        return None
    val = comp.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _comp_calls_json(usage: dict[str, Any]) -> str | None:
    """Serialize per-call composition/output history to a compact JSON blob."""
    calls = usage.get("composition_calls")
    if not calls:
        return None
    try:
        cleaned = [c for c in calls if isinstance(c, dict)]
        if not cleaned:
            return None
        return json.dumps(cleaned, separators=(",", ":"))
    except Exception:
        return None


def _build_record(
    *,
    session_id: str,
    interrupted: bool,
    model: str,
    platform: str,
    provider: str,
    user_message: str,
    final_response: str,
    turn_usage: dict[str, Any] | None,
    cfg: dict[str, Any],
    kwargs: dict[str, Any],
) -> TurnRecord | None:
    usage = turn_usage or {}
    is_subagent = bool(usage.get("is_subagent"))
    if is_subagent and not bool(cfg.get("record_subagents", True)):
        return None

    # Repair a leaked composite ``<provider>/<model>`` model id that arrived with
    # an empty provider column (Ace's 2026-07-05 phantom "Spend by model" row).
    # Done here at the recording boundary so it's telemetry-only and can never
    # perturb live inference/prompt-caching; also feeds the corrected provider to
    # cost pricing below so the row prices on the right route.
    provider, model = _recover_provider_from_model(model, provider)
    # MoA's model is a virtual PRESET name, not an unnamed physical model. Keep
    # the raw provider column and make that identity explicit in Spend-by-model.
    if str(provider or "").strip().lower() == "moa" and model:
        model_text = str(model).strip()
        if model_text and not model_text.lower().startswith("moa/"):
            model = f"moa/{model_text}"

    now = time.time()
    state = _session_state(session_id)
    with _lock:
        if kwargs.get("provisional"):
            # on_turn_abandoned: the turn is still live; record it but leave
            # its state (tools, ts_start) for the real emit that may follow.
            state = dict(_sessions.get(session_id or "", state))
            state["tools"] = list(state.get("tools") or [])
            state["tool_calls"] = list(state.get("tool_calls") or [])
        else:
            state = _sessions.pop(session_id or "", state)
    ts_start = _float_value(state.get("ts_start")) or now - _float_value(usage.get("latency_s"))
    ts_end = now
    tool_calls = list(state.get("tool_calls") or [])

    cost_usd, cost_status, cost_perclass = compute_turn_cost(
        model,
        provider,
        kwargs.get("base_url") or usage.get("base_url"),
        usage.get("calls") or [],
    )

    store_text = bool(cfg.get("store_text", True))

    # Depth tracking (B1 + D3): read depth from the agent attribute set at
    # construction time. Enforce invariant: if not a subagent, depth must be 0
    # (D3 — never trust the attribute for a parent agent).
    raw_depth = _int_or_none_value(usage.get("depth"))
    if not is_subagent:
        # D3: parents always depth 0 regardless of any attribute
        depth: int | None = 0
    elif raw_depth is not None:
        depth = raw_depth
    else:
        # No depth recorded — preserve NULL for historical/unknown
        depth = None
    chat_id = (
        kwargs.get("chat_id")
        or usage.get("chat_id")
        or usage.get("parent_chat_id")
        or ""
    )
    chat_name = (
        kwargs.get("chat_name")
        or usage.get("chat_name")
        or usage.get("parent_chat_name")
        or ""
    )

    return TurnRecord(
        turn_id=str(kwargs.get("turn_id") or _turn_id()),
        parent_turn_id=usage.get("parent_turn_id"),
        is_subagent=is_subagent,
        depth=depth,
        ts_start=ts_start,
        ts_end=ts_end,
        profile=str(kwargs.get("profile") or _profile_name()),
        provider=str(provider or ""),
        model=str(model or ""),
        platform=str(platform or usage.get("parent_platform") or ""),
        chat_id=str(chat_id or ""),
        chat_name=str(chat_name or ""),
        api_calls=_int_value(usage.get("api_calls")),
        tools=list(state.get("tools") or []),
        input_tokens=_int_value(usage.get("input_tokens")),
        output_tokens=_int_value(usage.get("output_tokens")),
        output_tokens_unknown=bool(usage.get("output_tokens_unknown")),
        input_tokens_unknown=bool(usage.get("input_tokens_unknown")),
        cache_read_tokens_unknown=bool(usage.get("cache_read_tokens_unknown")),
        cache_write_tokens_unknown=bool(usage.get("cache_write_tokens_unknown")),
        usage_unknown=bool(usage.get("usage_unknown")),
        cache_read_tokens=_int_value(usage.get("cache_read_tokens")),
        cache_write_tokens=_int_value(usage.get("cache_write_tokens")),
        idle_compaction_fired=usage.get("idle_compaction_fired"),
        compaction_tokens_before=_int_or_none_value(usage.get("compaction_tokens_before")),
        compaction_tokens_after=_int_or_none_value(usage.get("compaction_tokens_after")),
        compaction_cost_usd=usage.get("compaction_cost_usd"),
        reasoning_tokens=_int_value(usage.get("reasoning_tokens")),
        context_used=_int_value(usage.get("context_used")),
        context_length=_int_value(usage.get("context_length")),
        last_cache_read_tokens=_int_or_none_value(usage.get("last_cache_read_tokens")),
        last_cache_write_tokens=_int_or_none_value(usage.get("last_cache_write_tokens")),
        last_uncached_tokens=_int_or_none_value(usage.get("last_uncached_tokens")),
        last_call_prompt_unknown=bool(usage.get("last_call_prompt_unknown")),
        comp_sys_tokens=_comp_get(usage, "sys_tokens"),
        comp_tool_schema_tokens=_comp_get(usage, "tool_schema_tokens"),
        comp_history_tokens=_comp_get(usage, "history_tokens"),
        comp_history_message_count=_comp_get(usage, "history_message_count"),
        comp_tool_result_tokens=_comp_get(usage, "tool_result_tokens"),
        comp_tool_arg_tokens=_comp_get(usage, "tool_arg_tokens"),
        comp_tool_result_count=_comp_get(usage, "tool_result_count"),
        comp_skills_tokens=_comp_get(usage, "skills_tokens"),
        comp_skills_count=_comp_get(usage, "skills_count"),
        comp_framing_tokens=_comp_get(usage, "framing_tokens"),
        comp_calls_json=_comp_calls_json(usage),
        cost_usd=cost_usd,
        cost_status=cost_status,
        cost_uncached_usd=cost_perclass.get("uncached"),
        cost_cache_read_usd=cost_perclass.get("cache_read"),
        cost_cache_write_usd=cost_perclass.get("cache_write"),
        cost_output_usd=cost_perclass.get("output"),
        interrupted=bool(interrupted),
        user_text=str(user_message or "") if store_text else "",
        final_text=str(final_response or "") if store_text else "",
        tool_calls=tool_calls if store_text else [],
        cli_invocation_id=kwargs.get("cli_invocation_id"),
        # An explicit marker names how a turn that never finished was closed
        # (``signal_15``: the kanban dispatcher killed the worker;
        # ``orphan_repair``: synthesized by ``orphans --repair``). Otherwise a
        # failed turn carries its exit reason and a finished one stays NULL.
        terminal_error=(
            str(kwargs["terminal_error"])
            if kwargs.get("terminal_error")
            else str(kwargs.get("turn_exit_reason") or "failed")
            if kwargs.get("failed")
            else None
        ),
    )


# Turns written provisionally by on_turn_abandoned in THIS process, keyed by
# turn_id -> the hook kwargs. A call that lands after the provisional write
# refreshes the row; the real on_session_end drops the entry. All writes for
# such a turn happen under _provisional_lock so the real row always wins.
_provisional_turns: dict[str, dict[str, Any]] = {}
_provisional_lock = Lock()


def _abandoned_record(args: dict[str, Any], cfg: dict[str, Any]) -> TurnRecord | None:
    from plugins.blackbox import store

    turn_id = str(args.get("turn_id") or "")
    usage = store.ledger_turn_usage(turn_id) or {}
    for key in ("parent_turn_id", "parent_platform", "parent_chat_id",
                "parent_chat_name", "is_subagent", "depth"):
        if key in args:
            usage[key] = args[key]
    return _build_record(
        session_id=str(args.get("session_id") or ""),
        interrupted=True,
        model=str(args.get("model") or ""),
        platform=str(args.get("platform") or ""),
        provider=str(args.get("provider") or ""),
        user_message=args.get("user_message") or "",
        final_response="",
        turn_usage=usage,
        cfg=cfg,
        kwargs={
            "turn_id": turn_id,
            "chat_id": args.get("chat_id"),
            "chat_name": args.get("chat_name"),
            "turn_exit_reason": args.get("reason"),
            "provisional": True,
        },
    )


def _on_turn_abandoned(turn_id: str = "", **kwargs: Any) -> None:
    """Provisional interrupted row for a turn the host abandons mid-flight.

    The gateway fires this at shutdown for turns that will never reach
    ``on_session_end``. Usage comes from the turn's own main-lane
    ``turn_api_calls`` (authoritative for every billed call); the row never
    replaces an existing one nor the channel's latest-turn pointer, and a real
    row written later supersedes it.
    """
    try:
        cfg = _config()
        if cfg is None or not turn_id:
            return
        from plugins.blackbox import store

        args = {**kwargs, "turn_id": str(turn_id)}
        with _provisional_lock:
            record = _abandoned_record(args, cfg)
            if record is not None and store.insert_turn(record, provisional=True):
                _provisional_turns[str(turn_id)] = args
    except Exception:
        logger.warning("blackbox on_turn_abandoned failed", exc_info=True)


def _refresh_provisional_turn(turn_id: str) -> None:
    """Re-roll a provisional row after a call for its turn landed late."""
    try:
        cfg = _config()
        if cfg is None:
            return
        from plugins.blackbox import store

        with _provisional_lock:
            args = _provisional_turns.get(turn_id)
            if args is None:
                return
            record = _abandoned_record(args, cfg)
            if record is not None:
                store.insert_turn(record, move_last_turn=False)
    except Exception:
        logger.warning("blackbox provisional refresh failed", exc_info=True)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    model: str = "",
    platform: str = "",
    provider: str = "",
    user_message: str = "",
    final_response: str = "",
    turn_usage: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    try:
        cfg = _config()
        if cfg is None:
            # Disabled / no config block: ensure we never leak a session entry
            # that on_session_start/post_tool_call may have created.
            try:
                with _lock:
                    _sessions.pop(session_id or "", None)
            except Exception:
                pass
            return
        from plugins.blackbox import store

        if turn_usage is None and interrupted and kwargs.get("turn_id"):
            # A turn the process is abandoning (SIGTERM'd kanban worker,
            # Ctrl-C) never folded its accumulator, but every billed call is
            # already in turn_api_calls: price the row from the ledger rather
            # than recording a 0-token turn under real spend. The row must be
            # written regardless (the process is exiting), so a ledger that
            # could not be read yields UNKNOWN buckets, never measured zeros.
            try:
                turn_usage = store.ledger_turn_usage(
                    str(kwargs["turn_id"]), raise_on_error=True
                )
            except Exception:
                logger.warning("blackbox ledger usage read failed", exc_info=True)
                turn_usage = dict(store.LEDGER_USAGE_UNREADABLE)
        record = _build_record(
            session_id=session_id,
            interrupted=interrupted,
            model=model,
            platform=platform,
            provider=provider,
            user_message=user_message,
            final_response=final_response,
            turn_usage=turn_usage,
            cfg=cfg,
            kwargs=kwargs,
        )
        if record is None:
            return

        # Serialized with provisional writes/refreshes for the same process:
        # dropping the entry and writing the real row under the lock means a
        # late refresh can never land after (and over) the real row.
        with _provisional_lock:
            _provisional_turns.pop(record.turn_id, None)
            store.insert_turn(record)

        # New-model pricing sentinel (card t_2e382a4b). Runs AFTER the turn is
        # durably stored and is wrapped end-to-end in its own try/except with a
        # fire-and-forget alert thread, so it can neither lose telemetry nor add
        # latency to the turn. The extra guard here is belt-and-braces: even an
        # ImportError (module missing on a partially-deployed tree) must not
        # reach the retention sweep / alert path below.
        try:
            _observe_pricing_sentinel(record, turn_usage, kwargs.get("base_url"))
        except Exception:
            logger.warning("blackbox pricing sentinel dispatch failed", exc_info=True)

        # Retention sweep — prune turns older than retention_days. sweep() is
        # self-throttling (a 'last_sweep_date' sentinel makes it a no-op after
        # the first call each UTC day), so calling it every turn costs one
        # indexed SELECT/day and keeps the store from growing unbounded. Guard
        # it so a sweep failure never blocks recording/alerting.
        try:
            store.sweep(int(cfg.get("retention_days", 30) or 30))
        except Exception:
            logger.warning("blackbox retention sweep failed", exc_info=True)

        threshold = float(cfg.get("cost_alert_threshold_usd", 1.0) or 1.0)
        # The turn is always recorded above (visible to /cost and /context);
        # alerts_enabled only gates the proactive card PUSH to the channel.
        if not bool(cfg.get("alerts_enabled", True)):
            return
        should_alert = (
            bool(cfg.get("always_card"))
            or (record.cost_usd is not None and record.cost_usd >= threshold and not record.interrupted)
        )
        if not should_alert:
            return
        if record.interrupted and not bool(cfg.get("always_card")):
            return
        if store.mark_alerted(record.turn_id):
            record.alerted = True
            routing.send_card(
                render_card(record, threshold),
                record.platform,
                record.chat_id,
                record.profile,
            )
    except Exception:
        # Telemetry must never break a user turn, but a silent failure defeats
        # the purpose of telemetry — log it (the gateway loop still proceeds).
        logger.warning("blackbox on_session_end failed", exc_info=True)
        return


def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_turn_abandoned", _on_turn_abandoned)
    # The slash command lives in commands.py; the loader only calls this
    # package-level register(), so delegate explicitly or /cost never wires in.
    try:
        from plugins.blackbox import commands

        commands.register(ctx)
    except Exception:
        logger.warning("blackbox: failed to register /cost command", exc_info=True)
