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
from typing import Any, FrozenSet, Iterable, Mapping, Optional

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
