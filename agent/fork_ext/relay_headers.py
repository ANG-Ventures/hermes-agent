"""Relay-pool routing headers for fork-owned Claude API proxy lanes."""

from __future__ import annotations

import os
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar


def _pool_lane(agent, aux_task=None) -> str:
    """Classify a pool request into the interactive vs background lane (Phase 2,
    spec 2026-07-05 claude-relay-lanes). The relay reserves headroom / de-prioritizes
    background under contention so a burst of background work can't degrade a live
    interactive turn.

      * A top-level (non-delegated, non-cron/headless) main turn → ``interactive``.
      * A subagent (``_delegate_depth > 0``) or a cron/headless run → ``background``.
      * Auxiliary calls (``aux_task`` set): CRITICAL aux (compaction/title/vision on a
        live top-level turn's critical path — the user turn blocks on it) → ``interactive``
        (B1: never damp the compaction a live turn is waiting on); OFF-PATH aux (for a
        subagent/cron principal) → ``background``.
    """
    delegated = int(getattr(agent, "_delegate_depth", 0) or 0) > 0
    noninteractive = _is_noninteractive_principal(agent)
    if aux_task is not None:
        # critical aux (on a live top-level turn's path) stays interactive (B1);
        # off-path aux (subagent/cron/headless principal) is background.
        return "background" if (delegated or noninteractive) else "interactive"
    if delegated or noninteractive:
        return "background"
    return "interactive"


# The messaging platforms that represent a LIVE, human-facing conversation whose turn a
# person is actively waiting on. A request whose source is NOT one of these (cron,
# headless CLI, systemd/docker service, background job) is non-interactive → background.
# This mirrors the codebase idiom `agent.platform or HERMES_SESSION_SOURCE (default cli)`
# used by background_review / conversation_compression, rather than matching the single
# literal "cron" (which false-negatived every headless run to interactive — Greptile #206).
_INTERACTIVE_PLATFORMS = frozenset({
    "discord", "telegram", "slack", "whatsapp", "imessage", "signal", "sms",
    "messenger", "instagram", "matrix", "teams", "line", "wechat", "webhook",
    "tui", "desktop", "web", "api",
})


def _is_noninteractive_principal(agent) -> bool:
    """True when the request's PRINCIPAL is not a live human-facing conversation — a
    cron/scheduled run, a headless CLI/service run, or anything whose source isn't a
    known interactive messaging surface. Resolved the same way the rest of the codebase
    resolves the session source: ``agent.platform`` first, else ``HERMES_SESSION_SOURCE``
    (default ``cli`` → non-interactive). Empty/unknown → treat as non-interactive
    (background) so scheduled/headless bursts can NOT claim interactive headroom."""
    src = (getattr(agent, "platform", "") or "").strip().lower()
    if not src:
        src = (os.environ.get("HERMES_SESSION_SOURCE", "") or "").strip().lower()
    if not src:
        return True   # no signal at all → non-interactive (safe: don't grant headroom)
    return src not in _INTERACTIVE_PLATFORMS


def _pool_lane_src(agent, aux_task=None) -> str:
    """Compact classifier-inputs header (x-hermes-lane-src) so the relay logs the raw
    signals (platform, delegate_depth, aux_task) alongside the lane verdict — the lane
    must be validatable against its inputs, not against itself. Routing-only, stripped
    upstream (never egresses)."""
    platform = (getattr(agent, "platform", "") or "").strip().lower()
    # reflect the SAME source the classifier used (platform, else HERMES_SESSION_SOURCE)
    # so the logged inputs actually explain the lane verdict, not a partial view.
    if not platform:
        platform = (os.environ.get("HERMES_SESSION_SOURCE", "") or "").strip().lower() or "-"
    dd = int(getattr(agent, "_delegate_depth", 0) or 0)
    task = aux_task if aux_task is not None else "-"
    return f"platform={platform};delegate_depth={dd};aux_task={task}"


# The api-proxy pool, canonical name `claude-apr` (the api-proxy multi-sub relay,
# api_mode `anthropic_messages`). A frozenset (not a bare `==`) so the gate is
# rename-proof — a stale single literal here silently killed affinity/lane
# stamping when the pool was renamed claude-app→claude-apr (caught 2026-07-08,
# #241). The legacy `claude-app` alias was fully retired 2026-07-08 (no live
# config/session/cron references remain), so it is no longer accepted here.
# claude-alr (t_4c1a9bee, Ace 2026-09-30) is the SAME apr relay (:18810) under the
# a-family local-execution name: it must carry the same affinity/lane headers.
_POOL_AFFINITY_PROVIDERS = frozenset({"claude-apr", "claude-alr"})


# Pool relays that speak the error-class-v2 contract (fallback spec 2026-09-25
# D2 / Phase 1b). Both lanes: the affinity helper above is apr-only, but the
# capability header must reach bpr too, so it has its own provider set.
_POOL_CAPABILITY_PROVIDERS = frozenset({"claude-apr", "claude-alr", "claude-bpr"})
POOL_ACCEPTS_HEADER = "x-hermes-accepts"
POOL_ACCEPTS_VALUE = "error-class-v2"


def _pool_capability_headers(agent) -> dict:
    """Per-request capability negotiation for the claude pool relays.

    ``x-hermes-accepts: error-class-v2`` tells a relay running with
    ``error_class_v2=true`` that THIS client handles the v2 error contract:
    ``x-relay-error-class`` / ``relay_error_class`` and a connect failure sent
    as ``503 + conn`` (retried in place, never an immediate fallback). A relay
    without the flag, and every other client, keeps today's bytes. Pool-scoped:
    never sent to a direct pin (claude-bpx-N / claude-apx-N) or any third
    party; the relay reads it as routing-only and never forwards it upstream.
    """
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    if provider not in _POOL_CAPABILITY_PROVIDERS:
        return {}
    return {POOL_ACCEPTS_HEADER: POOL_ACCEPTS_VALUE}


def merge_pool_capability_headers(agent, api_kwargs):
    """Merge :func:`_pool_capability_headers` into ``api_kwargs['extra_headers']``
    (non-destructive; returns the same dict). Non-dict input is returned as is."""
    cap = _pool_capability_headers(agent)
    if cap and isinstance(api_kwargs, dict):
        eh = dict(api_kwargs.get("extra_headers") or {})
        eh.update(cap)
        api_kwargs["extra_headers"] = eh
    return api_kwargs


# Per-request correlation id (cachehop t_26d3993c). The harness stamps a fresh
# ``x-hermes-call-id: <profile>:<16 hex>`` on every HTTP attempt to a claude-bpx
# bridge lane and records the same value on that attempt's ``turn_api_calls``
# row, so the attributor joins a ledger row to the bridge journal by id
# instead of by timestamp. Bridge-scoped: the bridge hands the prompt to a
# spawned CLI, so request headers never egress; direct vendors never get it.
CALL_ID_HEADER = "x-hermes-call-id"
_CALL_ID_PROVIDER_RE = re.compile(r"^claude-bpx-\d+$|^claude-bpr$")
CALL_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}:[0-9a-f]{16}$")


def _call_id_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = str(get_active_profile_name() or "")
    except Exception:
        name = ""
    name = re.sub(r"[^a-z0-9_-]", "-", name.strip().lower())[:32].strip("-")
    return name or "default"


def stamp_call_id(agent, api_kwargs):
    """Stamp a FRESH call id into ``api_kwargs['extra_headers']`` for bridge lanes.

    Call once per HTTP attempt, before the request is sent; the ledger row for
    that attempt reads it back with :func:`call_id_of`. Returns the id, or None
    when the provider is out of scope (header removed so a reused kwargs dict
    never carries a stale id to another lane)."""
    if not isinstance(api_kwargs, dict):
        return None
    provider = getattr(agent, "provider", "")
    provider = provider.strip().lower() if isinstance(provider, str) else ""
    try:
        eh = dict(api_kwargs.get("extra_headers") or {})
    except (TypeError, ValueError):
        return None  # unusual extra_headers shape: leave the request exactly as built
    if not _CALL_ID_PROVIDER_RE.fullmatch(provider):
        if CALL_ID_HEADER in eh:
            eh.pop(CALL_ID_HEADER)
            api_kwargs["extra_headers"] = eh
        return None
    call_id = f"{_call_id_profile()}:{secrets.token_hex(8)}"
    eh[CALL_ID_HEADER] = call_id
    api_kwargs["extra_headers"] = eh
    return call_id


def call_id_of(api_kwargs):
    """The call id stamped on ``api_kwargs`` (validated), else None."""
    if not isinstance(api_kwargs, dict):
        return None
    eh = api_kwargs.get("extra_headers")
    value = eh.get(CALL_ID_HEADER) if isinstance(eh, dict) else None
    return value if isinstance(value, str) and CALL_ID_RE.fullmatch(value) else None


# S7 D1 correlation id (t_ebbae2c8; plans/subs-ace/S7-token-ledger-spec.md).
# ``route_id`` is THE correlation id between a Blackbox ``turn_api_calls`` row
# and the boundary record (box wirelog, bridge JSONL). The pool relay mints it
# on pooled lanes (32 hex, returned as ``x-pool-route-id`` and forwarded to the
# box as ``x-hermes-route-id``). Where no relay is in the path -- pinned
# ``claude-apx-N`` / ``claude-bpx-N`` main calls and every auxiliary call to a
# fleet lane -- the harness mints ``'h' + 32 hex`` and sends it itself. The
# prefix IS ``route_id_origin`` (none=relay, h=harness, c=cli shim), so every
# recorder derives the origin from the id with one grammar. Loopback hops only:
# the box proxy drops every x-hermes-* header before egress (I1).
ROUTE_ID_HEADER = "x-hermes-route-id"
LANE_SRC_HEADER = "x-hermes-lane-src"
BRIDGE_LANE_HEADER = "x-hermes-lane"
_BRIDGE_LANE_PROVIDER_RE = re.compile(r"^claude-bpr$|^claude-bpx-\d+$")


def _bridge_lane_enabled() -> bool:
    """Opt-in per profile; read at request time so absent config changes no bytes."""
    from hermes_cli.config import load_config_readonly
    return (load_config_readonly().get("agent") or {}).get("bridge_background_lane") is True


def stamp_bridge_lane(agent, api_kwargs):
    """Tag only background bridge legs; never label a human-facing gateway leg."""
    if not isinstance(api_kwargs, dict):
        return
    provider = _provider_of(agent)
    if not _BRIDGE_LANE_PROVIDER_RE.fullmatch(provider) or not _bridge_lane_enabled():
        return
    eh = dict(api_kwargs.get("extra_headers") or {})
    eh.pop(BRIDGE_LANE_HEADER, None)
    if _pool_lane(agent) == "background":
        eh[BRIDGE_LANE_HEADER] = "background"
    if eh or "extra_headers" in api_kwargs:
        api_kwargs["extra_headers"] = eh
ROUTE_ID_RE = re.compile(r"^[hc]?[0-9a-f]{32}$")
_ROUTE_ID_ORIGINS = {"h": "harness", "c": "cli"}
_PINNED_ROUTE_PROVIDER_RE = re.compile(r"^claude-[ab]px-\d+$")
# lane-src is observational at the relay (pick.lane_inputs + counters, never a
# routing input) and at the box (wirelog field). apr already gets it with the
# affinity set; bpr and the pinned lanes get it here (S7 P2a / §10 Q2).
_LANE_SRC_PROVIDER_RE = re.compile(r"^claude-bpr$|^claude-[ab]px-\d+$")
# Fleet-owned loopback lanes an AUXILIARY call may carry the header to. The
# pooled relays log it as ``client_route_id`` and forward their own id; the
# pinned boxes and gemini-bridge record it directly. Never a third party.
_AUX_ROUTE_PROVIDER_RE = re.compile(
    r"^claude-apr$|^claude-bpr$|^claude-[ab]px-\d+$|^gemini-bridge$|^gemini-ultra$|^antigravity$"
)


def mint_harness_route_id() -> str:
    return "h" + secrets.token_hex(16)


def route_id_origin(route_id):
    """``relay`` | ``harness`` | ``cli`` from the id's prefix; None if invalid."""
    if not isinstance(route_id, str) or not ROUTE_ID_RE.fullmatch(route_id):
        return None
    return _ROUTE_ID_ORIGINS[route_id[0]] if len(route_id) == 33 else "relay"


def _provider_of(agent) -> str:
    provider = getattr(agent, "provider", "")
    return provider.strip().lower() if isinstance(provider, str) else ""


def stamp_correlation_headers(agent, api_kwargs):
    """Per HTTP attempt: a FRESH harness route id on pinned claude lanes, and
    the lane-src classifier string on bpr + pinned lanes.

    A pooled call never gets a harness id (the relay mints, and its id wins).
    Out-of-scope providers have both headers removed so a reused kwargs dict
    never carries a stale id or lane-src to another lane (apr keeps the
    lane-src its affinity set stamped). Returns the harness id, else None.
    """
    if not isinstance(api_kwargs, dict):
        return None
    provider = _provider_of(agent)
    try:
        eh = dict(api_kwargs.get("extra_headers") or {})
    except (TypeError, ValueError):
        return None
    route_id = None
    if _PINNED_ROUTE_PROVIDER_RE.fullmatch(provider):
        route_id = mint_harness_route_id()
        eh[ROUTE_ID_HEADER] = route_id
    else:
        eh.pop(ROUTE_ID_HEADER, None)
    if _LANE_SRC_PROVIDER_RE.fullmatch(provider):
        eh[LANE_SRC_HEADER] = _pool_lane_src(agent)
    elif provider not in _POOL_AFFINITY_PROVIDERS:
        eh.pop(LANE_SRC_HEADER, None)
    if eh or "extra_headers" in api_kwargs:
        api_kwargs["extra_headers"] = eh
    return route_id


def route_id_of(api_kwargs):
    """The harness route id stamped on ``api_kwargs`` (validated), else None."""
    if not isinstance(api_kwargs, dict):
        return None
    eh = api_kwargs.get("extra_headers")
    value = eh.get(ROUTE_ID_HEADER) if isinstance(eh, dict) else None
    return value if isinstance(value, str) and ROUTE_ID_RE.fullmatch(value) else None


class _AuxRoute:
    """One auxiliary call's harness route id and what the WIRE saw.

    ``offered_to`` is the fleet providers whose kwargs were built with the id.
    That is intent, not evidence: an adapter can drop ``extra_headers`` (the
    Anthropic Messages aux adapter did, t_d5f71d8e). ``wire`` is the evidence,
    set from the served HTTP response by :func:`note_aux_http_response`:
    ``(sent, relay_id)`` -- did the request carry our id, and did a pooled
    relay answer with its own ``x-pool-route-id``.
    """

    __slots__ = ("route_id", "offered_to", "wire")

    def __init__(self):
        self.route_id = mint_harness_route_id()
        self.offered_to = set()
        self.wire = None

    def id_for(self, provider):
        """The id the served route's boundary record carries, else None.

        Pooled relays (claude-apr / claude-bpr) mint and forward their OWN id,
        so their ``x-pool-route-id`` wins (same rule as the main path,
        chat_completion_helpers). Otherwise the harness id, but only when the
        served request actually carried it. No wire evidence -> None: an id
        that never reached the box cannot be joined, and a wrong join is worse
        than a NULL.
        """
        p = provider.strip().lower() if isinstance(provider, str) else ""
        if not p or p not in self.offered_to or not self.wire:
            return None
        sent, relay_id = self.wire
        if relay_id:
            return relay_id
        return self.route_id if sent else None


_AUX_ROUTE: "ContextVar[_AuxRoute | None]" = ContextVar("aux_route_id", default=None)
_POOL_ROUTE_ID_HEADER = "x-pool-route-id"
_RELAY_ROUTE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@contextmanager
def aux_route_scope():
    """Bind one harness route id to one auxiliary call (every retry/fallback
    attempt inside it builds kwargs under the same id)."""
    route = _AuxRoute()
    token = _AUX_ROUTE.set(route)
    try:
        yield route
    finally:
        _AUX_ROUTE.reset(token)


def aux_route_headers(provider) -> dict:
    """``{x-hermes-route-id: <id>}`` for an aux request to a fleet lane inside
    an :func:`aux_route_scope`, else ``{}``. Never raises.

    Called once per attempt as its kwargs are built, so it also clears the
    previous attempt's wire evidence: a fallback attempt whose client reports
    no response must not inherit the failed attempt's ids.
    """
    try:
        route = _AUX_ROUTE.get()
        if route is None:
            return {}
        route.wire = None
        p = provider.strip().lower() if isinstance(provider, str) else ""
        if not _AUX_ROUTE_PROVIDER_RE.fullmatch(p):
            return {}
        route.offered_to.add(p)
        return {ROUTE_ID_HEADER: route.route_id}
    except Exception:
        return {}


def note_aux_http_response(response) -> None:
    """Record what the wire saw for the current aux attempt (httpx response).

    Inert outside an :func:`aux_route_scope`. Reads the REQUEST's headers (was
    our id actually sent?) and the response's ``x-pool-route-id`` (a pooled
    relay's own id). The last response in the scope wins: SDK retries and
    fallbacks end on the one that served. Never raises.
    """
    try:
        route = _AUX_ROUTE.get()
        if route is None or response is None:
            return
        request = getattr(response, "request", None)
        req_headers = getattr(request, "headers", None) or {}
        sent = req_headers.get(ROUTE_ID_HEADER) == route.route_id
        relay_id = (getattr(response, "headers", None) or {}).get(_POOL_ROUTE_ID_HEADER)
        if not (isinstance(relay_id, str) and _RELAY_ROUTE_ID_RE.fullmatch(relay_id)):
            relay_id = None
        route.wire = (sent, relay_id)
    except Exception:
        return


async def anote_aux_http_response(response) -> None:
    """Async httpx event-hook form of :func:`note_aux_http_response`."""
    note_aux_http_response(response)


def _pool_affinity_headers(agent, aux_task=None) -> dict:
    """Return the routing-only headers for the claude relay POOL: the x-hermes-session
    affinity id AND the x-hermes-lane / x-hermes-lane-src lane classification.

    The pool uses the session id to pin a conversation to one subscription for
    prompt-cache preservation, and the lane to reserve headroom for interactive turns
    under contention (reset-weighted router + lanes, spec 2026-07-05). All are:
      * PER-REQUEST — read off the live ``agent`` at call-build time, so the session id
        rotates correctly when compaction mints a child id (NOT a static default_header,
        NOT the HERMES_SESSION_ID ContextVar which could go stale across the httpx
        worker-thread boundary → cross-conversation key bleed).
      * POOL-SCOPED — only stamped for ``claude-apr`` (the api-proxy pool, api_mode
        ``anthropic_messages``; the legacy ``claude-app`` alias was retired 2026-07-08), so they are never sent to a direct Anthropic endpoint
        or any third party. The relay strips them before dispatching upstream (routing
        metadata on a loopback hop, no egress, no telemetry — satisfies the
        no-outbound-attribution rubric).

    SCOPE NOTE (Greptile #205): ``claude-bpr`` (the bridge pool, formerly ``claude-bpp``) resolves to api_mode
    ``chat_completions`` — a DIFFERENT branch of ``build_api_kwargs`` — and is a
    secondary failover surface with its own separate daemon that agents rarely route
    to as primary. It is deliberately OUT of scope here so this helper only claims what
    the anthropic_messages wiring actually stamps. Wiring the bpp path is a documented
    follow-up, not a silent gap.

    ``aux_task`` (when this is called from the auxiliary-client path) drives the lane
    criticality split; a main turn passes ``aux_task=None``.
    """
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    if provider not in _POOL_AFFINITY_PROVIDERS:
        return {}
    sid = getattr(agent, "session_id", None)
    out = {}
    if sid and isinstance(sid, str):
        out["x-hermes-session"] = sid
    out["x-hermes-lane"] = _pool_lane(agent, aux_task)
    out["x-hermes-lane-src"] = _pool_lane_src(agent, aux_task)
    return out
