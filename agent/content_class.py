"""Content-class taxonomy for token-estimate calibration.

The rough token estimator is a character-rate approximation, and its error is a
RATE error — measured on real production sessions the fraction it misses by is
far more stable than the absolute miss. But that rate is *not* uniform across
content: natural-language prose packs at a different chars/token rate than the
JSON/structured tool output an agent session is mostly made of, and media parts
are billed by dimension/page/duration rather than by transport size at all.

A single global skew ratio therefore blends a text-heavy turn and a
tool-output-heavy turn into a number that is wrong for both. This module names
the classes and provides the (cheap, total, never-raising) classifier the
calibration consults, so each turn can be corrected by the ratio measured on
the content it is actually made of.

Design notes:

* Weights are attributed at the PART level, not the message level. An assistant
  message that carries prose *and* a tool_calls payload contributes to both
  ``text`` and ``tool`` in proportion to its bytes — attributing the whole
  message to one class would systematically mislabel the most common message
  shape in an agent transcript.
* Media is weighed at its flat provider-pricing cost (``media_part_token_cost``),
  never at base64 length. Weighing a screenshot by transport size would let a
  single image out-vote an entire conversation.
* "Dominant" means a strict MAJORITY of the weight. With three classes,
  plurality-without-majority is genuinely ambiguous: a 40/35/25 turn has no
  single correct per-class ratio, and the honest correction for it is the
  blended global one. Majority is the *definition* of dominance here, not a
  tuning value, so it is deliberately not a config knob.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent.media_tokens import MEDIA_PART_TYPES, media_part_token_cost

CLASS_TEXT = "text"
CLASS_TOOL = "tool"
CLASS_MEDIA = "media"

#: Every class the calibration may track. Order is display order.
CONTENT_CLASSES = (CLASS_TEXT, CLASS_TOOL, CLASS_MEDIA)

# Content-part types that are structured tool traffic rather than prose. Both
# the OpenAI-style and Anthropic-style spellings appear in the wire shapes
# Hermes builds, so both are listed.
_TOOL_PART_TYPES = frozenset(
    {"tool_result", "tool_use", "tool_call", "function_call", "function_result"}
)

# Characters-per-weight-unit for text bytes. The classifier only needs RELATIVE
# shares, so the exact divisor does not change which class wins between two text
# buckets — it exists so text bytes are compared against a media part's flat
# TOKEN cost in the same (approximate) unit.
_CHARS_PER_WEIGHT = 3.5


def _text_weight(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return int(len(value) / _CHARS_PER_WEIGHT)
    try:
        return int(len(json.dumps(value, default=str)) / _CHARS_PER_WEIGHT)
    except Exception:
        return int(len(str(value)) / _CHARS_PER_WEIGHT)


def _add(weights: Dict[str, int], cls: str, amount: int) -> None:
    if amount > 0:
        weights[cls] = weights.get(cls, 0) + amount


def _accumulate_content(weights: Dict[str, int], content: Any, base_cls: str) -> None:
    """Attribute one ``content`` field's weight, walking multimodal part lists."""
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                _add(weights, base_cls, _text_weight(part))
                continue
            ptype = part.get("type")
            if ptype in MEDIA_PART_TYPES:
                _add(weights, CLASS_MEDIA, max(1, int(media_part_token_cost(part))))
            elif ptype in _TOOL_PART_TYPES:
                _add(weights, CLASS_TOOL, _text_weight(part))
            else:
                _add(weights, base_cls, _text_weight(part))
        return
    _add(weights, base_cls, _text_weight(content))


def content_class_weights(messages: Optional[List[Dict[str, Any]]]) -> Dict[str, int]:
    """Approximate per-class token weight of a message list. Never raises.

    Returns a dict keyed by :data:`CONTENT_CLASSES`; absent classes are omitted.
    An empty or unusable input yields ``{}``.
    """
    weights: Dict[str, int] = {}
    if not messages:
        return weights
    try:
        for msg in messages:
            if not isinstance(msg, dict):
                _add(weights, CLASS_TEXT, _text_weight(msg))
                continue
            role = msg.get("role")
            base = CLASS_TOOL if role == "tool" else CLASS_TEXT
            _accumulate_content(weights, msg.get("content"), base)
            # An assistant turn's tool_calls payload is structured output even
            # though it rides on a non-tool role.
            for key in ("tool_calls", "function_call"):
                if msg.get(key):
                    _add(weights, CLASS_TOOL, _text_weight(msg.get(key)))
    except Exception:
        # Classification is an optimization; a malformed transcript must fall
        # back to the global calibration, never fail a turn.
        return {}
    return weights


def dominant_content_class(
    messages: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    """The class holding a strict MAJORITY of the estimated weight, else ``None``.

    ``None`` means "no single class dominates this request" and is the caller's
    signal to use the blended global calibration — which is the correct
    correction for a genuinely mixed turn.
    """
    weights = content_class_weights(messages)
    if not weights:
        return None
    total = sum(weights.values())
    if total <= 0:
        return None
    top_cls, top_weight = max(weights.items(), key=lambda kv: kv[1])
    if top_weight * 2 > total:
        return top_cls
    return None
