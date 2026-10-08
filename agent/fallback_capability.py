"""Capability-gated fallback hops (t_1ed37625).

A fallback chain entry is a LANE (relay face + delivery mode), and some lanes
cannot serve a request SHAPE at all, whatever the model: the claude-bpx
bridge in interactive (tui) delivery answers an image part with HTTP 400
``tui_images_unsupported`` (Phase 1), and with ``runtime.tui.hostTools`` off it
answered a tool-bearing turn with 400 ``tui_tools_unsupported`` (2026-10-05
13:27 / 14:16: alr -> dtlr 502 -> btpr 400 -> bpr, two dead hops, two banners,
two HTTP round-trips per turn). Chain ORDER is Ace's (10-03) and stays; this
module only lets the walker skip a hop that cannot serve the current request.

Two halves:

* :func:`request_shape` / :func:`stamp_request_shape`: the shape of the wire
  payload the loop is about to send (``tools`` when ``tools[]`` is non-empty,
  ``images`` when any message carries a native image part), stamped on the
  agent by ``build_api_request`` for every attempt.
* :func:`lane_incapable_shape`: the first shape in the stamped request that the
  candidate provider's ``ProviderProfile.unsupported_request_shapes`` declares
  it cannot serve. The walker logs ``skipped <lane>: lane_incapable(<shape>)``
  at INFO and moves on: no banner, no ledger row, no HTTP call.

The declaration is opt-in and explicit. ``supports_vision`` is NOT read here:
it defaults to False on most profiles and the model catalog fills it in, so a
False would wrongly skip vision-capable lanes for every image request.

:data:`LANE_INCAPABLE_CODES` is the other direction: when a lane that did not
declare the gap (or the primary itself) answers with one of the bridge's
machine codes, the classifier files it as ``lane_incapable`` instead of an
unclassified Anthropic 400, and the banner names our bridge as the source.
"""

from __future__ import annotations

import logging
import re
from typing import Any, FrozenSet, Iterable, Mapping, NamedTuple, Optional

logger = logging.getLogger(__name__)

SHAPE_TOOLS = "tools"
SHAPE_IMAGES = "images"
REQUEST_SHAPES: FrozenSet[str] = frozenset((SHAPE_TOOLS, SHAPE_IMAGES))

# Bridge (claude-bpx tuiRunner) machine codes -> the request shape the lane
# refused. ``mode_not_allowed`` is the relay refusing the delivery-mode header
# for this sub (no shape: the lane itself is closed to the caller).
LANE_INCAPABLE_CODES: Mapping[str, Optional[str]] = {
    "tui_tools_unsupported": SHAPE_TOOLS,
    "tui_images_unsupported": SHAPE_IMAGES,
    "mode_not_allowed": None,
    # The DPX content-aliaser (dpx-content-alias internal/contentalias) refusing a
    # request whose HISTORY carries a tool_use / tool_result it cannot alias. The
    # bridge wraps that deterministic 400 as 502 ``tui_upstream_error`` ("API Error:
    # 400 contentalias:history_tool"), so by status it read as a transient server
    # error: 3 in-place retries here, 4 relay hops (2026-10-05 13:20, 13:27, 14:16
    # on sub-vps-21/22/15/13). Deterministic for the lane + this transcript.
    "contentalias:history_tool": SHAPE_TOOLS,
}
# The aliaser's error family in message text (``contentalias:<reason>``); only
# history_tool is a lane-shape refusal, the rest stay with their status class.
_CONTENTALIAS_RE = re.compile(r"contentalias:([a-z_]+)")


class BridgeCode(NamedTuple):
    """How a fallback row names one claude-bpx bridge machine code (t_5d79bfea).

    ``trigger_class``: the fallback_events §4.1 class. ``head``: the announce
    head label, replacing the status-derived one (a 409 ``tui_busy`` read
    ``(bad request)``); None keeps the reason label. ``cause``: the rider cause;
    None derives it from the error text (``fallback_policy._cause_phrase``).
    ``detail``: a clause after the seat. ``ours``: False when the bridge only
    REPORTS an upstream answer (a usage cap, a safeguard flag, an upstream error
    row); the rider then says ``(reported by the bridge)`` instead of ``(ours,
    not Anthropic)``."""

    trigger_class: str
    head: Optional[str]
    cause: Optional[str]
    detail: Optional[str] = None
    ours: bool = True


# Every machine code the bridge answers with (claude-bpx bridge/src/tuiRunner.js
# ``TUI_ERRORS``, 3b7ab72). The relay passes a bridge body through with
# ``x-relay-error-hop: bridge->upstream``, which is wrong for these: the bridge
# (or, for ``mode_not_allowed``, the relay) wrote the body, never Anthropic.
# ONE table: the class, head label, rider cause and hop of a bridge-coded
# failover all come from here (tests/agent/test_fallback_bridge_codes.py pins it
# against the bridge's list).
_LANE = "lane_incapable"
_POOL = "pool_pressure"
BRIDGE_ERROR_CODES: Mapping[str, BridgeCode] = {
    # The lane refuses this request shape (deterministic for the lane).
    "mode_not_allowed": BridgeCode(_LANE, "lane cannot serve this request", None),
    "tui_tools_unsupported": BridgeCode(_LANE, "lane cannot serve this request", None),
    "tui_images_unsupported": BridgeCode(_LANE, "lane cannot serve this request", None),
    "tui_no_session_key": BridgeCode(_LANE, "lane cannot serve this request",
                                     "interactive session needs a session key"),
    "tui_last_not_user": BridgeCode(_LANE, "lane cannot serve this request",
                                    "interactive session needs a user turn last"),
    "tui_turn_too_large": BridgeCode(_LANE, "lane cannot serve this request",
                                     "turn too large for the interactive input"),
    "tui_tools_invalid": BridgeCode(_LANE, "lane cannot serve this request",
                                    "tools the interactive session cannot offer"),
    "tui_tool_result_too_large": BridgeCode(_LANE, "lane cannot serve this request",
                                            "tool result too large for the interactive session"),
    "context_length_exceeded": BridgeCode(_LANE, "lane cannot serve this request",
                                          "interactive session hit the context limit"),
    "tui_config": BridgeCode(_LANE, "bridge refused the turn",
                             "interactive session config invalid on the box"),
    # The bridge refused this turn's tool protocol / output.
    "tui_tool_unknown": BridgeCode(_LANE, "bridge refused the turn",
                                   "tool result names an unknown tool call"),
    "tui_tool_duplicate": BridgeCode(_LANE, "bridge refused the turn",
                                     "tool call answered or issued twice"),
    "tui_ambiguous_parallel": BridgeCode(_LANE, None, "ambiguous parallel tool calls"),
    "tui_tool_not_host": BridgeCode(_LANE, None, "tool call outside the host namespace"),
    "tui_tool_mismatch": BridgeCode(_LANE, None, "tool call did not match its tool use"),
    "tui_tool_uncorrelated": BridgeCode(_LANE, None, "tool call not tied to its message"),
    "entrypoint_mismatch": BridgeCode(_LANE, None, "non-interactive entrypoint on the wire"),
    "tui_upstream_error": BridgeCode(_LANE, None, "interactive session reported an upstream error row",
                                     ours=False),
    # This session / this box cannot take the turn right now.
    "tui_busy": BridgeCode(_POOL, "session busy", "interactive session busy",
                           "this session's previous turn is still running"),
    "tui_history_diverged": BridgeCode(_POOL, "session demoted", None),
    "tui_cancelled": BridgeCode(_POOL, "session demoted",
                                "interactive session demoted (turn cancelled after the client left)"),
    "tui_capacity": BridgeCode(_POOL, None, "relay box at session capacity"),
    "seat_capacity": BridgeCode(_POOL, None, "no account seat for a new interactive session"),
    "tui_state_unwritable": BridgeCode(_POOL, None, "interactive session state unwritable on the box"),
    "tui_startup": BridgeCode(_POOL, None, "relay session startup timed out"),
    "tui_mcp_not_ready": BridgeCode(_POOL, None, "host tool server not ready"),
    "tui_turn_timeout": BridgeCode("conn", None, "interactive turn timed out at the bridge"),
    "tui_rate_limited": BridgeCode("quota_seat", None, None, ours=False),
    "safeguard_refusal": BridgeCode("refusal", None, None, ours=False),
}


def bridge_error_code(body: Any) -> Optional[str]:
    """The bridge machine code of an error body (one of :data:`BRIDGE_ERROR_CODES`),
    else None. Same body shapes as :func:`body_lane_incapable_code`."""
    if not isinstance(body, dict):
        return None
    for obj in (body, body.get("error")):
        if isinstance(obj, dict):
            for key in ("code", "error_code"):
                c = str(obj.get(key) or "").strip().lower()
                if c in BRIDGE_ERROR_CODES:
                    return c
    return None


def bridge_code_hop(code: str) -> str:
    """The hop that wrote a bridge-coded body: the relay for ``mode_not_allowed``,
    else the bridge (``relay→bridge``)."""
    return "relay" if code == "mode_not_allowed" else "relay→bridge"

_IMAGE_PART_TYPES = frozenset(("image_url", "input_image", "image"))


def _has_image_part(value: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, dict):
        if value.get("type") in _IMAGE_PART_TYPES:
            return True
        content = value.get("content")
        return _has_image_part(content, depth + 1) if content is not None else False
    if isinstance(value, (list, tuple)):
        return any(_has_image_part(v, depth + 1) for v in value)
    return False


def request_shape(api_messages: Any, tools: Any) -> FrozenSet[str]:
    """Shape of the payload about to go on the wire: a subset of :data:`REQUEST_SHAPES`."""
    shape = set()
    if tools:
        shape.add(SHAPE_TOOLS)
    if isinstance(api_messages, (list, tuple)) and _has_image_part(api_messages):
        shape.add(SHAPE_IMAGES)
    return frozenset(shape)


def stamp_request_shape(agent: Any, api_messages: Any, tools: Any) -> FrozenSet[str]:
    """Record the attempt's shape on ``agent._request_shape`` (read by the chain walker)."""
    shape = request_shape(api_messages, tools)
    try:
        agent._request_shape = shape
    except Exception:  # noqa: BLE001
        pass
    return shape


def stamped_request_shape(agent: Any) -> FrozenSet[str]:
    shape = getattr(agent, "_request_shape", None)
    return shape if isinstance(shape, frozenset) else frozenset()


def declared_unsupported_shapes(provider: Optional[str]) -> FrozenSet[str]:
    """``ProviderProfile.unsupported_request_shapes`` for ``provider`` (canonical or
    alias); empty when the provider has no registered profile or declares nothing."""
    name = (provider or "").strip().lower()
    if not name:
        return frozenset()
    if name.startswith("custom:"):
        name = name[len("custom:"):]
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(name)
    except Exception:  # noqa: BLE001
        return frozenset()
    declared = getattr(profile, "unsupported_request_shapes", None) if profile else None
    if not declared:
        return frozenset()
    try:
        return frozenset(str(s).strip().lower() for s in declared) & REQUEST_SHAPES
    except TypeError:
        return frozenset()


def lane_incapable_shape(provider: Optional[str], shape: Iterable[str]) -> Optional[str]:
    """The first request shape ``provider`` declares it cannot serve, else None."""
    declared = declared_unsupported_shapes(provider)
    if not declared:
        return None
    for s in (SHAPE_TOOLS, SHAPE_IMAGES):
        if s in shape and s in declared:
            return s
    return None


def lane_incapable_code(code: Any) -> Optional[str]:
    """The bridge machine code when ``code`` is one of :data:`LANE_INCAPABLE_CODES`."""
    c = str(code or "").strip().lower()
    return c if c in LANE_INCAPABLE_CODES else None


def message_lane_incapable_code(text: Any) -> Optional[str]:
    """``contentalias:history_tool`` when the aliaser's refusal rides inside a message
    (the bridge's 502 ``tui_upstream_error`` wrapper), else None."""
    if not text:
        return None
    m = _CONTENTALIAS_RE.search(str(text))
    return lane_incapable_code(f"contentalias:{m.group(1)}") if m else None


def body_lane_incapable_code(body: Any, text: Any = None) -> Optional[str]:
    """The lane-incapable code carried by an error body: OpenAI SDK ``body`` is the
    inner ``error`` object (``{"type", "code", "message"}``); the Anthropic SDK and a
    raw relay body carry it under ``error.code`` / ``error.error_code``. A bridge
    ``tui_upstream_error`` wrapper names the aliaser refusal in its message instead."""
    if isinstance(body, dict):
        for key in ("code", "error_code"):
            hit = lane_incapable_code(body.get(key))
            if hit:
                return hit
        inner = body.get("error")
        if isinstance(inner, dict):
            for key in ("code", "error_code"):
                hit = lane_incapable_code(inner.get(key))
                if hit:
                    return hit
            text = text or inner.get("message")
        text = text or body.get("message")
    return message_lane_incapable_code(text)
