"""Canonical reasoning-effort vocabulary and wire clamping.

Hermes' internal effort ladder (``VALID_REASONING_EFFORTS`` plus ``none``) is wider than any
single provider wire accepts; hand-rolled per-transport maps leaked new levels (``ultra``) to
wires that 400 and inverted the ladder (unknown → weak default). Single source of truth:
:data:`EFFORT_LADDER` (low→high), :func:`clamp_effort` (verbatim if supported, else the
nearest WEAKER level; only when nothing weaker exists the weakest supported), and named
wire-vocabulary constants so call sites declare data. Rules: wire shape stays local, only the
vocabulary math lives here; unset stays unset (never invent an effort); when a provider
rejects a level fix its declared set, never a predicate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

#: Matches ``k3`` as a delimited token (``k3``, ``k3-256k``, ``kimi-k3-cot``), never K2-era names (``kimi-k2.6``).
# From #76427 by @ruizanthony.
_KIMI_K3_SLUG_RE = re.compile(r"(?:^|[^a-z0-9])k3(?:[^a-z0-9]|$)")

# Canonical low→high ordering for nearest-level clamping. Includes "none" so an explicit
# disable can be clamped when a provider publishes it as a level. ``ultra`` is Hermes-internal
# (the Codex product tier): no wire accepts it, every declared set stops at ``max``.
EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: Widest OpenAI-compatible wire vocabulary (OpenRouter, Nous Portal).
OPENAI_COMPAT_WIRE_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: OpenAI/Codex Responses per model generation (live-verified): ``minimal`` is rejected by
#: both (clamps to low); ``max`` is gpt-5.6 / gpt-6-tier only (legacy = 5.5 and older).
CODEX_GPT56_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
CODEX_LEGACY_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh")
# GPT-6 Astra is account-gated and its Responses API accepts no disable/minimal
# wire level; callers normalize those requests to ``low`` at the transport boundary.
CODEX_ASTRA_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
ASTRA_MODEL_IDS: frozenset[str] = frozenset({"gpt-6-astra", "gpt-6-astra-900k"})
#: GPT-6 Sol/Terra/Luna (the 5.6 successors; ``-pro``/``-900k``/dated snapshots share the prefix).
GPT6_TIER_PREFIXES: tuple[str, ...] = ("gpt-6-sol", "gpt-6-luna")
#: GPT-6.1 Sol takes Astra's ``low..max`` ladder (``none`` 400s, live 2026-09-29) without Astra's
#: account gating, so it stays in the static catalogs.
NO_DISABLE_TIER_PREFIXES: tuple[str, ...] = ("gpt-6.1-sol",)
DAYBREAK_MODEL_IDS: frozenset[str] = frozenset(
    {"gpt-daybreak-blue-latest", "gpt-daybreak-blue-latest-900k"}
)

#: Backward-compat alias (pre-#68365-verification name); fork tests import it.
CODEX_RESPONSES_EFFORTS: tuple[str, ...] = CODEX_GPT56_EFFORTS

#: xAI Responses — Grok 4.6+ accepts xhigh; older Grok tops out at high.
XAI_GROK46_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
XAI_LEGACY_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: Actual Computer relays (SGLang/vLLM).
ACTUAL_RELAY_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")

#: Moonshot/Kimi K3 (server default high) vs K2-era models. K3 quirks: ``high`` is K3's
#: positional middle AND server default, so ``medium`` rounds to it rather than down to
#: ``low``; ``xhigh`` rounds up to ``max`` (K3's top tier).
KIMI_K3_EFFORTS: tuple[str, ...] = ("low", "high", "max")
KIMI_K2_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
KIMI_K3_OVERRIDES: dict[str, str] = {"medium": "high", "xhigh": "max"}

#: OpenCode "Ox Alpha" (x-preview-f-free): thinking cannot be disabled and the wire accepts
#: exactly low/high/max (medium/none/xhigh 400); xhigh rounds up.
OX_ALPHA_EFFORTS: tuple[str, ...] = ("low", "high", "max")
OX_ALPHA_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Tencent TokenHub / Nebius Token Factory / Upstage Solar: plain three-level knobs.
TOKENHUB_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
NEBIUS_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
SOLAR_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: GLM-5.2 native knob: exactly ``high`` (its minimum thinking level) and ``max``; GLM-5.3
#: widens it to a graded scale (live-verified, monotonic). ``xhigh`` requests the top tier.
GLM52_EFFORTS: tuple[str, ...] = ("high", "max")
GLM52_OVERRIDES: dict[str, str] = {"xhigh": "max"}
# : GLM-5.3 widens the knob to a graded low/medium/high/max scale — verified : live on
# api.z.ai/api/coding/paas/v4 (issue #91789, 2026-08-21): every : level accepted with monotonic
# reasoning-token scaling (low=4, medium=11, : high=98, max=125 on the probe prompt).
GLM53_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
GLM53_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: DeepSeek V4 OpenAI-compat endpoint; ``xhigh`` requests the top tier.
DEEPSEEK_V4_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
DEEPSEEK_V4_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Ollama Cloud /v1/chat/completions: rejects ``minimal`` with HTTP 400.
OLLAMA_CLOUD_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")
OLLAMA_CLOUD_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Meta Model API (Muse): rejects ``none``.
META_AI_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")


def is_astra_model(model: Optional[str]) -> bool:
    """``gpt-6-astra`` or its Hermes-side ``-900k`` picker alias, with or without a ``vendor/`` prefix.
    The single home for the slug set: picker gating, effort vocabulary and the request sanitizer all
    key off it, so a new Astra alias is one edit."""
    return (model or "").strip().lower().rsplit("/", 1)[-1] in ASTRA_MODEL_IDS


def codex_supported_efforts(model: Optional[str]) -> tuple[str, ...]:
    """Supported effort set for an OpenAI/Codex Responses model."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    if is_astra_model(model) or bare.startswith(NO_DISABLE_TIER_PREFIXES):
        return CODEX_ASTRA_EFFORTS
    return (
        CODEX_GPT56_EFFORTS
        if "gpt-5.6" in bare or bare.startswith(GPT6_TIER_PREFIXES) or bare in DAYBREAK_MODEL_IDS
        else CODEX_LEGACY_EFFORTS
    )


def kimi_supported_efforts(model: Optional[str]) -> tuple[str, ...]:
    """Supported effort set for a Moonshot/Kimi slug (bare ``k3``, ``k3-256k``, ``kimi-k3*`` → K3).

    K3 is served as the bare slug ``k3``, plan variants like ``k3-256k``, and the ``kimi-k3*`` aliases; its
    documented set is low/high/max. Everything earlier speaks low/medium/high. Boundary-matched so K2-era
    names (``kimi-k2.6``) never match (detection regex from #76427 by @ruizanthony).
    """
    m = (model or "").strip().lower().split("/")[-1]
    return KIMI_K3_EFFORTS if _KIMI_K3_SLUG_RE.search(m) else KIMI_K2_EFFORTS


def clamp_effort(
    effort: Optional[str], supported: Optional[Sequence[str]], overrides: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Clamp a requested reasoning effort onto a wire's supported levels.

    ``overrides`` (a declared vendor mapping, e.g. Kimi K3 ``medium → high``) is consulted
    first. Otherwise the request passes through unchanged when it is supported, when the
    supported set is unknown/empty, or when it isn't a recognized ladder level (custom
    providers may use bespoke names). Else the **nearest weaker** supported level is returned
    so a clamp never escalates cost; when nothing weaker exists, the weakest supported level
    (the provider's floor is the closest honest match). Monotonic: a stronger request never
    resolves weaker than a weaker request would.
    """
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    supported_norm = [lvl for lvl in (str(s).strip().lower() for s in supported) if lvl in EFFORT_LADDER]
    if not supported_norm or requested in supported_norm:
        return effort
    if overrides and overrides.get(requested) in supported_norm:
        return overrides[requested]
    if requested not in EFFORT_LADDER:
        return effort
    # "none" disables reasoning — never a degradation target for an enabled ask
    # (clamping "minimal" to "none" would silently switch thinking off).
    candidates = [level for level in supported_norm if level != "none"]
    if not candidates:
        return effort
    requested_idx = EFFORT_LADDER.index(requested)
    below = [level for level in candidates if EFFORT_LADDER.index(level) < requested_idx]
    return max(below, key=EFFORT_LADDER.index) if below else min(candidates, key=EFFORT_LADDER.index)


def route_supported_efforts(provider: Optional[str], model: Optional[str]) -> tuple[str, ...]:
    """Levels the (provider, model) route's ENTRY clamp accepts: the Codex/OpenAI Responses set per
    model generation, else the widest OpenAI-compatible vocabulary (narrower providers clamp again
    downstream, never upward)."""
    if (provider or "").strip().lower() == "openai-codex":
        return codex_supported_efforts(model)
    return OPENAI_COMPAT_WIRE_EFFORTS


def effort_display_label(effort: Optional[str], provider: Optional[str] = None, model: Optional[str] = None) -> str:
    """Picker / ``/reasoning`` status label for a ladder level: the level itself when the route sends
    it verbatim, else ``"<level> (sends <clamped> on this route)"`` so a Hermes-internal step such as
    ``ultra`` (#61634) is never presented as a distinct wire level the route does not have."""
    requested = str(effort or "").strip().lower()
    clamped = clamp_effort(requested, route_supported_efforts(provider, model))
    return requested if not requested or clamped == requested else f"{requested} (sends {clamped} on this route)"


def requested_effort(reasoning_config: Optional[dict]) -> Optional[str]:
    """The user's explicit effort, or None (absent/malformed config, no effort, or reasoning
    disabled) — callers then omit the wire field."""
    if not isinstance(reasoning_config, dict) or reasoning_config.get("enabled") is False:
        return None
    return str(reasoning_config.get("effort") or "").strip().lower() or None


# (route label, requested, sent) triples already announced this process: the clamp notice is
# one line per route and level, not one per request.
_CLAMP_NOTICED: set[tuple[str, str, str]] = set()


@dataclass(frozen=True)
class EffortRoute:
    """How one chat-completions route takes OpenAI's top-level ``reasoning_effort``.

    ``supported`` is the wire vocabulary (``()`` = the route takes no effort field), ``overrides``
    a declared vendor mapping consulted before the nearest-weaker clamp, ``default`` what an unset
    effort sends (None = omit, the route's own default applies), ``label`` names the route in the
    clamp notice, and ``thinking_toggle`` adds Moonshot's ``extra_body.thinking`` on/off beside it.
    """

    supported: tuple[str, ...]
    overrides: Optional[dict[str, str]] = None
    default: Optional[str] = None
    label: str = ""
    thinking_toggle: bool = False


def resolve_wire_effort(
    reasoning_config: Optional[dict],
    supported: Sequence[str],
    overrides: Optional[dict[str, str]] = None,
    *,
    default: Optional[str] = None,
    route: str = "",
) -> Optional[str]:
    """The ``reasoning_effort`` value a top-level-knob wire gets for ``reasoning_config``, or None = omit.

    One resolver for every emitter of OpenAI's top-level ``reasoning_effort`` (config
    ``agent.reasoning_effort``, ``--reasoning``, per-task ``auxiliary.<task>.reasoning_effort`` all
    arrive here as a reasoning config): disabled → ``none`` when the route lists it, else omitted;
    unset → ``default`` (None keeps the field off); a ladder level → :func:`clamp_effort` onto
    ``supported`` (``overrides`` first), and when an explicit request is sent as a different level
    one log line names the route, the requested level and what was sent (never a silent
    downgrade). A bespoke non-ladder level passes through verbatim (custom relays publish their
    own tiers). An empty ``supported`` means the route takes no effort field: always None.
    """
    if not supported:
        return None
    if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
        return "none" if "none" in supported else None
    requested = requested_effort(reasoning_config)
    explicit = requested is not None
    if requested is None:
        requested = default
    if requested is None:
        return None
    if requested == "none":
        return "none" if "none" in supported else None
    sent = clamp_effort(requested, supported, overrides)
    if explicit and sent != requested:
        key = (route, requested, str(sent))
        if key not in _CLAMP_NOTICED:
            _CLAMP_NOTICED.add(key)
            logger.warning(
                "reasoning_effort: %s accepts %s; '%s' sent as '%s'",
                route or "this route", "/".join(str(s) for s in supported), requested, sent,
            )
    return sent


def resolve_route_effort(reasoning_config: Optional[dict], route: EffortRoute) -> Optional[str]:
    """:func:`resolve_wire_effort` for an :class:`EffortRoute`."""
    return resolve_wire_effort(
        reasoning_config, route.supported, route.overrides, default=route.default, route=route.label,
    )


def kimi_effort_route(model: Optional[str]) -> EffortRoute:
    """Moonshot/Kimi direct hosts: K3 = low/high/max (server default high), K2-era = low/medium/high."""
    supported = kimi_supported_efforts(model)
    is_k3 = supported is KIMI_K3_EFFORTS
    return EffortRoute(
        supported, KIMI_K3_OVERRIDES if is_k3 else None, default="high" if is_k3 else "medium",
        label=f"kimi/{(model or '').rsplit('/', 1)[-1] or 'model'}", thinking_toggle=True,
    )


def tokenhub_effort_route() -> EffortRoute:
    """Tencent TokenHub: low/medium/high, server default high."""
    return EffortRoute(TOKENHUB_EFFORTS, default="high", label="tencent-tokenhub")


def profile_effort_route(
    declared: Optional[Sequence[str]], label: str, overrides: Optional[dict[str, str]] = None,
) -> EffortRoute:
    """A profile's ``supported_reasoning_efforts`` as a route: None = the widest OpenAI-compatible
    vocabulary (narrower upstreams clamp again), ``()`` = no effort field."""
    supported = OPENAI_COMPAT_WIRE_EFFORTS if declared is None else tuple(declared)
    return EffortRoute(supported, overrides, label=label)


def profile_route_for(profile, model: Optional[str]) -> EffortRoute:
    """The :class:`EffortRoute` a ``supports_reasoning_effort`` profile declares for *model*."""
    overrides_fn = getattr(profile, "reasoning_effort_overrides", None)
    overrides = overrides_fn(model) if callable(overrides_fn) else None
    return profile_effort_route(
        profile.supported_reasoning_efforts(model), f"{profile.name}/{model}",
        overrides if isinstance(overrides, dict) else None,
    )


#: Request-body keys that carry a reasoning/thinking control on some chat-completions wire.
REASONING_CONTROL_KEYS: frozenset[str] = frozenset({
    "reasoning", "reasoning_effort", "thinking", "thinking_config", "thinkingconfig",
    "thinking_budget", "thinkingbudget", "enable_thinking", "think", "verbosity",
})


def has_reasoning_control(value: object) -> bool:
    """Whether a request payload (recursively) already carries a reasoning wire control."""
    if not isinstance(value, dict):
        return False
    return any(
        str(key).strip().lower() in REASONING_CONTROL_KEYS or has_reasoning_control(nested)
        for key, nested in value.items()
    )


def clamp_reasoning_config(reasoning_config: Optional[dict], supported: Sequence[str] = OPENAI_COMPAT_WIRE_EFFORTS) -> Optional[dict]:
    """Return ``reasoning_config`` with its ``effort`` clamped onto ``supported`` (non-dicts and
    configs without an effort pass through untouched).

    The entry clamp for an OpenAI-compatible chat-completions request builder: Hermes-internal
    ``ultra`` never reaches a wire (#89503 main transport, #112010 aux/MoA), while provider
    profiles with narrower vocabularies clamp again downstream. Unset stays unset.
    """
    if not isinstance(reasoning_config, dict):
        return reasoning_config
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    clamped = clamp_effort(effort, supported) if effort else effort
    return {**reasoning_config, "effort": clamped} if clamped != effort else reasoning_config


def thinking_toggle_extras(
    reasoning_config: Optional[dict],
    efforts: Sequence[str],
    overrides: Optional[dict[str, str]] = None,
    *,
    always_emit_toggle: bool = False,
) -> tuple[dict, dict]:
    """Translate a reasoning config onto the Moonshot/DeepSeek chat_completions wire:
    ``extra_body.thinking`` toggle and top-level ``reasoning_effort``.

    Moonshot 400s when both are sent, so by default the effort (when it lands in
    ``efforts``) replaces the toggle. DeepSeek instead requires the toggle on every
    request (an omitted toggle defaults thinking on and then demands
    ``reasoning_content`` echoes), hence ``always_emit_toggle``. A requested effort of
    ``none`` is not a level on these wires; it falls back to the plain toggle.
    """
    if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
        return {"thinking": {"type": "disabled"}}, {}
    effort = requested_effort(reasoning_config)
    clamped = clamp_effort(None if effort == "none" else effort, efforts, overrides)
    if clamped in efforts:
        return ({"thinking": {"type": "enabled"}} if always_emit_toggle else {}), {"reasoning_effort": clamped}
    return {"thinking": {"type": "enabled"}}, {}


def ox_alpha_reasoning_extras(reasoning_config: Optional[dict], model: Optional[str]) -> tuple[dict, dict]:
    """Ox Alpha (``x-preview-f-free``) ``reasoning_effort`` translation for the
    opencode-zen profile (low/high/max only; anything else 400s)."""
    if (model or "").strip().rsplit("/", 1)[-1].lower() != "x-preview-f-free":
        return {}, {}
    effort = requested_effort(reasoning_config)
    clamped = clamp_effort(None if effort == "none" else effort, OX_ALPHA_EFFORTS, OX_ALPHA_OVERRIDES)
    return ({}, {"reasoning_effort": clamped}) if clamped in OX_ALPHA_EFFORTS else ({}, {})
