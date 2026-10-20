"""Boomerang v2 P2: ``inherit_mode="fork"`` (spec 2026-10-18 §5.1/§5.2, D-8/9/10/14/16/17/18).

A fork child's request prefix is byte-identical to the parent's (same system prompt,
tools[], thinking config, cache ttl, message array through the parent's last complete
assistant turn) so its first request, pinned to the parent's seat (P1), reads the
parent's warm cache. Anything that would make the prefix diverge downgrades to the v1
fold with ``boomerang.fork_ineligible reason=<...>`` (INV-2): a near-fork writes cold
and still claims a saving, so it is never shipped.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tools.delegate_tool")

# D-16: fork and branch are one mode.
_MODE_ALIASES = {"fork": "fork", "branch": "fork", "summary": "summary"}
INHERIT_MODES = ("fork", "branch", "summary")

_FORK_MAX_PREFIX_TOKENS_DEFAULT = 600_000  # D-9
_WARM_MARGIN_S = 300
_TTL_SECONDS = {"1h": 3600, "5m": 300}

# Above any live registry generation, so a compaction-boundary tool refresh cannot
# rebuild the fork's tools[] and break the prefix (same rule as the review fork).
_FROZEN_TOOL_SNAPSHOT_GENERATION = 2_147_483_647


def normalize_inherit_mode(value: Any) -> Optional[str]:
    """``"fork"|"branch"`` -> ``"fork"``, ``"summary"`` -> ``"summary"``, None/"" -> None.

    Raises ValueError on anything else so a typo never silently runs a fold."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    mode = _MODE_ALIASES.get(str(value).strip().lower())
    if mode is None:
        raise ValueError(f"inherit_mode must be one of {', '.join(INHERIT_MODES)}; got {value!r}")
    return mode


def resolve_inherit_mode(inherit_context: Any, inherit_mode: Any) -> Optional[str]:
    """Effective mode for one task: None = no inheritance (v1 default, INV-1).

    ``inherit_context=true`` with no mode is ``"summary"`` (D-2/INV-1); an explicit
    mode implies inheritance."""
    mode = normalize_inherit_mode(inherit_mode)
    if mode is not None:
        return mode
    return "summary" if inherit_context else None


def _boomerang_cfg(delegation_cfg: Any) -> Dict[str, Any]:
    boom = delegation_cfg.get("boomerang") if isinstance(delegation_cfg, dict) else None
    return boom if isinstance(boom, dict) else {}


def fork_enabled(delegation_cfg: Any) -> bool:
    from utils import is_truthy_value

    return is_truthy_value(_boomerang_cfg(delegation_cfg).get("fork_enabled"), default=True)


def parent_cache_ttl_seconds(parent: Any) -> int:
    """TTL of the parent's warm prefix, from its LAST cache-writing response (P0.4).

    apx bills 1h unless the seat is in overage, whatever the request asked for, so the
    usage split is the truth; ``_cache_ttl`` is only the fallback when no split was seen."""
    split = getattr(parent, "_last_cache_creation_split", None)
    if isinstance(split, dict):
        if (split.get("1h") or 0) > 0:
            return 3600
        if (split.get("5m") or 0) > 0:
            return 300
    return _TTL_SECONDS.get(str(getattr(parent, "_cache_ttl", "") or "").strip().lower(), 300)


def fork_prefix_messages(history: Any) -> Optional[List[Dict[str, Any]]]:
    """The parent transcript through its last COMPLETE assistant turn (D-8), or None.

    Complete = an assistant row with no tool calls (a final answer). Anything after it
    (the in-flight user turn of a delegate_task issued from the parent's own tool call,
    or an unfinished tool round) is dropped: that is exactly the prefix the parent's
    cache already holds."""
    if not isinstance(history, list):
        return None
    for idx in range(len(history) - 1, -1, -1):
        msg = history[idx]
        if isinstance(msg, dict) and msg.get("role") == "assistant" and not msg.get("tool_calls"):
            return history[: idx + 1]
    return None


def _has_orphan_tool_calls(messages: List[Dict[str, Any]]) -> bool:
    answered = {m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("role") == "tool"}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id and tc_id not in answered:
                    return True
    return False


def _parent_history(parent: Any) -> Any:
    history = getattr(parent, "_session_messages", None)
    return getattr(parent, "conversation_history", None) if history is None else history


def _estimate_prefix_tokens(parent: Any, messages: List[Dict[str, Any]]) -> int:
    from agent.model_metadata import estimate_messages_tokens_rough

    system = (getattr(parent, "_cached_system_prompt", None) or "") + (
        getattr(parent, "ephemeral_system_prompt", None) or ""
    )
    tools_chars = len(json.dumps(getattr(parent, "tools", None) or [], default=str))
    return estimate_messages_tokens_rough(messages) + (len(system) + tools_chars) // 4


def fork_eligible(
    parent: Any, rt: Dict[str, Any], delegation_cfg: Any, *, now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Pre-construction eligibility (spec §5.1). Returns ``(ok, reason)``; every predicate
    has its own reason. ``same_request_params`` is checked after construction
    (``request_fingerprint``) because only the built child carries the wire attributes."""
    from agent.chat_completion_helpers import _POOL_SUB_KEY_RE
    from agent.fork_ext.relay_headers import _POOL_AFFINITY_PROVIDERS

    if not fork_enabled(delegation_cfg):
        return False, "fork_enabled"
    # D-17: a fork (any delegated child) may not fork again; it holds the parent's full schema.
    if (getattr(parent, "_delegate_depth", 0) or 0) > 0 or getattr(parent, "side_agent", False) is True:
        return False, "fork_depth"
    provider = (getattr(parent, "provider", "") or "").strip().lower()
    if provider not in _POOL_AFFINITY_PROVIDERS or (rt.get("provider") or "").strip().lower() != provider:
        return False, "same_lane"
    # An assertion, not a choice: build_fork_runtime forced the parent's model.
    if rt.get("model") != getattr(parent, "model", None):
        return False, "same_model"
    seat = getattr(parent, "last_served_by", None)
    if not (isinstance(seat, str) and _POOL_SUB_KEY_RE.fullmatch(seat)):
        return False, "seat_known"
    served_at = getattr(parent, "last_served_at", None)
    # warm: now - last_served_at < ttl - 300. A 5m parent gets a negative window and NEVER
    # forks. INTENTIONAL (spec pass-2 RC-2): forks are for human-paced 1h sessions;
    # machine-paced 5m sources fold. Do not "fix" the margin.
    ttl_s = parent_cache_ttl_seconds(parent)
    now = time.time() if now is None else now
    if not isinstance(served_at, (int, float)) or now - served_at >= ttl_s - _WARM_MARGIN_S:
        return False, "warm"
    prefix = fork_prefix_messages(_parent_history(parent))
    if not prefix or _has_orphan_tool_calls(prefix):
        return False, "turn_complete"
    if not isinstance(getattr(parent, "_cached_system_prompt", None), str):
        return False, "same_request_params"
    cap = int(_boomerang_cfg(delegation_cfg).get("fork_max_prefix_tokens") or _FORK_MAX_PREFIX_TOKENS_DEFAULT)
    try:
        from agent.model_metadata import get_model_context_length

        window = get_model_context_length(rt.get("model"))
        if window and window > 0:
            cap = min(cap, int(window * 0.6))
    except Exception:
        logger.debug("fork: child window lookup failed; using the configured cap", exc_info=True)
    if _estimate_prefix_tokens(parent, prefix) > cap:
        return False, "prefix_le_cap"
    return True, ""


def build_fork_runtime(rt: Dict[str, Any], parent: Any) -> Dict[str, Any]:
    """Force the child runtime to the PARENT's lane/model/thinking config (spec RC-1),
    ignoring delegation.model/provider/reasoning_effort: anything else rewrites bytes."""
    forced = dict(rt)
    forced["model"] = getattr(parent, "model", None)
    forced["reasoning_config"] = copy.deepcopy(getattr(parent, "reasoning_config", None))
    if isinstance(getattr(parent, "max_tokens", None), int):
        forced["max_tokens"] = parent.max_tokens
    return forced


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def request_fingerprint(agent: Any) -> Dict[str, str]:
    """Every agent attribute that lands in the cached request prefix (spec §5.1
    ``same_request_params``). Compared parent vs child after construction."""
    from agent.fast_mode import effective_request_overrides

    fields = {
        "model": getattr(agent, "model", None),
        "provider": getattr(agent, "provider", None),
        "base_url": getattr(agent, "base_url", None),
        "api_mode": getattr(agent, "api_mode", None),
        "reasoning_config": getattr(agent, "reasoning_config", None),
        "request_overrides": effective_request_overrides(agent),
        "cache_ttl": getattr(agent, "_cache_ttl", None),
        "prompt_caching": getattr(agent, "_use_prompt_caching", None),
        "native_cache_layout": getattr(agent, "_use_native_cache_layout", None),
        "max_tokens": getattr(agent, "max_tokens", None),
        "tools": getattr(agent, "tools", None) or [],
        "system_prompt": getattr(agent, "_cached_system_prompt", None),
        "system_static": getattr(agent, "_cached_system_prompt_static", None),
        "ephemeral_system_prompt": getattr(agent, "ephemeral_system_prompt", None),
        "prefill": getattr(agent, "prefill_messages", None) or [],
    }
    return {k: _json(v) for k, v in fields.items()}


def fork_constructor_overrides(parent: Any) -> Dict[str, Any]:
    """AIAgent kwargs a fork child is built with instead of the slim child prompt build."""
    return {
        "ephemeral_system_prompt": getattr(parent, "ephemeral_system_prompt", None),
        "prefill_messages": copy.deepcopy(getattr(parent, "prefill_messages", None)) or None,
        "request_overrides": dict(getattr(parent, "request_overrides", None) or {}),
    }


def apply_fork_overlay(child: Any, parent: Any) -> Dict[str, Any]:
    """Make the built child's prefix the parent's: exact system prompt bytes, tools[],
    cache ttl, transcript. Returns the undo snapshot for ``revert_fork_overlay``.

    ``_regate_inherited_child_tier`` / ``_apply_child_cache_ttl`` are NOT run for a fork
    (both rewrite request bytes); the caller skips them."""
    undo = {
        "tools": getattr(child, "tools", None),
        "valid_tool_names": getattr(child, "valid_tool_names", None),
        "_tool_snapshot_generation": getattr(child, "_tool_snapshot_generation", 0),
        "_cache_ttl": getattr(child, "_cache_ttl", None),
    }
    child._cached_system_prompt = parent._cached_system_prompt
    child._cached_system_prompt_static = getattr(parent, "_cached_system_prompt_static", None)
    child.session_start = getattr(parent, "session_start", getattr(child, "session_start", None))
    child._cache_ttl = getattr(parent, "_cache_ttl", None)
    child.tools = copy.deepcopy(getattr(parent, "tools", None) or [])
    child.valid_tool_names = {t["function"]["name"] for t in child.tools if isinstance(t, dict) and "function" in t}
    child._tool_snapshot_generation = _FROZEN_TOOL_SNAPSHOT_GENERATION
    # Runs as conversation_history through the live send path (canonicalize_replay_history ->
    # ... -> build_prompt_cache_plan), the same chain the parent's request took (P0.3).
    child._fork_history = copy.deepcopy(fork_prefix_messages(_parent_history(parent)))
    child._fork_parent_session_id = getattr(parent, "session_id", None)
    child._inherit_mode = "fork"
    return undo


def revert_fork_overlay(child: Any, undo: Dict[str, Any]) -> None:
    for attr, value in undo.items():
        setattr(child, attr, value)
    child._cached_system_prompt = None
    child._cached_system_prompt_static = None
    child._fork_history = None
    child._inherit_mode = "summary"


def log_ineligible(reason: str, parent: Any) -> None:
    logger.info(
        "boomerang.fork_ineligible reason=%s parent_session=%s",
        reason, str(getattr(parent, "session_id", "") or "-").replace(" ", "_"),
    )


# Spec §5.2 + the v1 autonomous-execution contract (boomerang skill); this exact framing
# passed the INV-4 gate (AC-5, docs/boomerang-v2/evidence/inv4-probes, 19/20, 0/20).
def build_fork_frame(goal: str, parent_session_id: Any, delegation_id: Any) -> str:
    return (
        "[BOOMERANG FORK — you are a forked continuation of this same conversation, running in the\n"
        f"background. The turns above are yours. Parent session: {parent_session_id or '-'}. "
        f"Delegation ID: {delegation_id or '-'}.\n"
        f"Your task now: {goal} Execute\n"
        "autonomously: make reasonable assumptions, do NOT stop to ask questions. If you hit a genuine\n"
        "blocker, complete what you can and report the blocker in your summary.\n"
        "End with the structured handoff: Outcome / Changed / Commands-validation / Blockers.]"
    )


def fork_run_inputs(child: Any, goal: Any) -> Tuple[Any, Optional[List[Dict[str, Any]]]]:
    """``(user_message, conversation_history)`` for the child's run: the frame turn over
    the inherited transcript for a fork, the goal alone otherwise."""
    history = getattr(child, "_fork_history", None)
    if not isinstance(history, list) or not isinstance(goal, str):
        return goal, None
    frame = build_fork_frame(goal, getattr(child, "_fork_parent_session_id", None), getattr(child, "_delegation_id", None))
    return frame, copy.deepcopy(history)


def _tool_call_ids(msg: Any) -> List[str]:
    if not isinstance(msg, dict):
        return []
    ids = [msg.get("tool_call_id")] if msg.get("role") == "tool" else []
    for tc in msg.get("tool_calls") or []:
        ids.append(tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None))
    return [i for i in ids if i]


def child_generated_messages(messages: Any, history: Any, boundary: Any = None) -> Any:
    """``messages`` minus the inherited fork prefix (Prism f5ff1cc07dca, bec64a4205fe).

    ``run_conversation(conversation_history=history)`` returns history + the child's own
    rows, but the parent's old tool rounds are not the child's execution evidence: they
    must not reach ``tool_trace``, ``output_tail``, the interrupted-partial pick or the
    ``subagent_stop`` hooks. The model request still carries the history; only the result
    is trimmed. Ownership is decided by position, never by content (a child answer equal
    to an old parent answer is still the child's):

    1. ``boundary`` = the turn's exported ``current_turn_user_idx``, computed by the loop
       on the final list after any compression: the turn's own rows start there.
    2. Else the prefix is intact (same roles + tool_call ids, then a user row): slice at
       ``len(history)``.
    3. Else drop only rows carrying an inherited tool_call id (ids are unique per call),
       so no inherited tool round can surface as child evidence and no child row is lost.
    """
    if not isinstance(messages, list) or not isinstance(history, list) or not history:
        return messages
    if (
        isinstance(boundary, int) and not isinstance(boundary, bool) and 0 <= boundary < len(messages)
        and isinstance(messages[boundary], dict) and messages[boundary].get("role") == "user"
    ):
        return messages[boundary:]
    n = len(history)

    def _key(m: Any) -> Tuple[Any, Tuple[str, ...]]:
        return (m.get("role") if isinstance(m, dict) else None, tuple(_tool_call_ids(m)))

    if (
        len(messages) > n
        and isinstance(messages[n], dict) and messages[n].get("role") == "user"
        and all(_key(a) == _key(b) for a, b in zip(messages[:n], history))
    ):
        return messages[n:]
    inherited_ids = {i for m in history for i in _tool_call_ids(m)}
    return [m for m in messages if not (set(_tool_call_ids(m)) & inherited_ids)]


def strip_inherited_from_result(result: Any, history: Any) -> Any:
    """A child-turn result whose ``messages`` holds only that turn's own rows, for EVERY turn
    of a fork child (the first turn over ``history`` and any continuation, e.g. the
    schema-correction retry); a non-fork result (``history`` not a list) is unchanged."""
    if not isinstance(result, dict) or not isinstance(history, list) or not history:
        return result
    messages = result.get("messages")
    trimmed = child_generated_messages(messages, history, result.get("current_turn_user_idx"))
    if trimmed is messages:
        return result
    out = dict(result)
    out["messages"] = trimmed
    return out
