"""Token counting utilities for LCM.

Uses tiktoken when available, falls back to char-based estimate.
"""

import logging
import threading
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional

from .message_content import normalize_content_value

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4

# --- Media part accounting ---------------------------------------------------
#
# Multimodal content parts carry their payload as base64 (a data URL, or an
# Anthropic ``source.data`` blob). ``normalize_content_value`` JSON-dumps the
# whole structure, so counting its characters bills a 1.5 MB screenshot as
# ~500,000 tokens when the provider charges ~1,500. Measured on a real
# 1710x1518 PNG: char-counting gave 502,182 tokens vs Anthropic's actual
# (w * h / 750) = 3,461 -- a 145x overestimate that drove spurious compaction.
#
# Providers price attachments by DIMENSION or PAGE, never by transport size, so
# the payload's character length carries no pricing signal at all. Use a flat
# per-part cost, which is the same model the host estimator uses.
_IMAGE_PART_TOKENS = 1500
# A PDF/document page costs roughly an image; we cannot cheaply count pages
# without parsing, so bill a conservative multi-page default.
_DOCUMENT_PART_TOKENS = 3000
# Audio is billed per second of duration; the payload length is likewise not a
# usable proxy. Flat, conservative.
_AUDIO_PART_TOKENS = 1500

_IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
_DOCUMENT_PART_TYPES = frozenset({"document", "input_file", "file"})
_AUDIO_PART_TYPES = frozenset({"input_audio", "audio"})

_MEDIA_PART_COSTS = tuple(
    (types, cost)
    for types, cost in (
        (_IMAGE_PART_TYPES, _IMAGE_PART_TOKENS),
        (_DOCUMENT_PART_TYPES, _DOCUMENT_PART_TOKENS),
        (_AUDIO_PART_TYPES, _AUDIO_PART_TOKENS),
    )
)


def media_part_token_cost(part: Any) -> int:
    """Flat token cost for a multimodal content part, or 0 if it is not media.

    Returns 0 for text parts and anything unrecognized, so callers can fall
    through to normal character-based counting.
    """
    if not isinstance(part, dict):
        return 0
    part_type = part.get("type")
    if not part_type:
        return 0
    for types, cost in _MEDIA_PART_COSTS:
        if part_type in types:
            return cost
    return 0


# Optional host-supplied replacement for ``count_messages_tokens``. A host that
# already calibrates its token estimates against real provider usage can
# register that counter here so LCM's compaction decisions and the host's
# display agree, instead of each maintaining an independent guess that can
# silently diverge. ``None`` = use the built-in estimate.
_host_messages_token_counter: "Optional[Callable[[List[Dict[str, Any]]], int]]" = None


def set_messages_token_counter(
    counter: "Optional[Callable[[List[Dict[str, Any]]], int]]",
) -> None:
    """Register (or clear, with ``None``) the host's message token counter."""
    global _host_messages_token_counter
    if counter is not None and not callable(counter):
        raise TypeError("token counter must be callable or None")
    _host_messages_token_counter = counter


def get_messages_token_counter() -> "Optional[Callable[[List[Dict[str, Any]]], int]]":
    """Return the registered host counter, or ``None`` if using the built-in."""
    return _host_messages_token_counter


_encoder = None
_encoder_ready = False
_encoder_lock = threading.Lock()
_encoder_thread = None
# Cache-key generation, bumped (under _encoder_lock) when the real encoder is
# adopted. The memoized count is keyed on (text, generation), so an estimator
# result computed before adoption can only ever be inserted under the old
# generation: a cache_clear() alone cannot stop an in-flight LRU miss from
# repopulating the cache with a stale estimate *after* the clear, but a stale
# insert under a dead key is unreachable by post-adoption lookups.
_encoder_generation = 0

# Bound on how long a count_tokens() caller will wait for the FIRST encoder
# load. tiktoken.get_encoding() downloads the BPE file over the network when
# it is not already cached on disk; on restricted-egress hosts that request
# can hang for minutes before failing (measured: ~127s per fresh process in a
# production deployment), and _get_encoder() sits on the host's post-turn
# hook path -- so the hang blocked reply delivery. Callers that hit the bound
# use the char-based estimate; the real encoder is adopted (and the count
# cache cleared) whenever the background load completes.
_ENCODER_FIRST_WAIT_S = 2.0


def _load_encoder():
    """The (possibly network-backed) tiktoken load. Patchable in tests."""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def _encoder_loader() -> None:
    global _encoder, _encoder_ready, _encoder_generation
    enc = None
    try:
        enc = _load_encoder()
    except Exception:
        logger.debug("tiktoken not available, using char-based estimates")
    with _encoder_lock:
        _encoder = enc
        _encoder_ready = True
        if enc is not None:
            # Counts for identical text change estimator -> encoder on
            # adoption; bumping the generation retires every pre-adoption
            # cache key, including ones an in-flight miss has yet to insert.
            _encoder_generation += 1
    if enc is not None:
        # Memory hygiene only (correctness comes from the generation key):
        # evict the now-unreachable old-generation entries.
        _count_tokens_cached.cache_clear()


def _get_encoder():
    """Return the tiktoken encoder, never blocking on unbounded network I/O.

    The load runs in a daemon thread. The first caller waits briefly
    (_ENCODER_FIRST_WAIT_S) so the common already-cached-on-disk case still
    gets exact counts immediately; after that, callers never wait -- they use
    the estimator until the loader finishes.
    """
    global _encoder_thread
    if _encoder_ready:
        return _encoder
    first = False
    with _encoder_lock:
        if _encoder_ready:
            return _encoder
        if _encoder_thread is None:
            _encoder_thread = threading.Thread(
                target=_encoder_loader, name="lcm-tiktoken-load", daemon=True
            )
            _encoder_thread.start()
            first = True
    if first:
        _encoder_thread.join(timeout=_ENCODER_FIRST_WAIT_S)
    return _encoder if _encoder_ready else None


def _fallback_token_estimate(text: str) -> int:
    # Latin text is ~4 chars/token, but CJK and other non-Latin scripts
    # tokenize far denser (~1-2 tokens/char). A flat len//4 undercounts them
    # ~3-4x, so preflight under-triggers and assembly can overflow the real
    # budget. ASCII-only text is overwhelmingly common and can use the cheap
    # legacy estimate without scanning every character.
    length = len(text)
    if length == 0:
        return 0
    if text.isascii():
        return length // _CHARS_PER_TOKEN + 1
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ratio = non_ascii / length
    if ratio >= 0.5:
        divisor = 1.5
    elif ratio >= 0.2:
        divisor = 2.5
    else:
        divisor = _CHARS_PER_TOKEN
    return int(length / divisor) + 1


def _count_tokens_core(text) -> int:
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    if isinstance(text, str):
        return _fallback_token_estimate(text)
    return len(text) // _CHARS_PER_TOKEN + 1


@lru_cache(maxsize=2048)
def _count_tokens_cached(text: str, generation: int) -> int:
    # tiktoken encoding is the dominant per-turn cost: assembly and preflight
    # re-count the same content many times per turn. The counting function is
    # stable within one encoder generation (see _encoder_generation), so a
    # (content, generation) key is stable.
    return _count_tokens_core(text)


# Cap what the LRU may retain by reference. Very large strings are the ones
# least likely to recur identically, and caching them would still let the
# bounded LRU pin unnecessary memory; count those uncached (cost is
# proportional to size either way).
_MAX_CACHEABLE_TOKEN_TEXT_CHARS = 32_768


def count_tokens(text) -> int:
    """Count tokens in a string."""
    if not text:
        return 0
    # Only strings are memoized. Callers may pass non-string, unhashable values
    # (e.g. tool_call arguments as a dict); preserve the legacy tolerance by
    # counting those uncached rather than feeding them to the LRU.
    if isinstance(text, str) and len(text) <= _MAX_CACHEABLE_TOKEN_TEXT_CHARS:
        return _count_tokens_cached(text, _encoder_generation)
    return _count_tokens_core(text)


def _split_media_from_content(content: Any) -> tuple[Any, int]:
    """Return (content_without_media, media_tokens).

    Media parts are replaced by a short placeholder so the surrounding text is
    still counted normally, while the base64 payload never reaches the
    character counter.
    """
    media_tokens = 0
    if isinstance(content, list):
        remaining: List[Any] = []
        for part in content:
            cost = media_part_token_cost(part)
            if cost:
                media_tokens += cost
                continue
            remaining.append(part)
        return remaining, media_tokens
    # Multimodal tool results arrive wrapped: {"_multimodal": True, "content": [...]}
    if isinstance(content, dict) and content.get("_multimodal"):
        inner, inner_tokens = _split_media_from_content(content.get("content"))
        if inner_tokens:
            return {**content, "content": inner}, inner_tokens
    return content, media_tokens


def count_message_tokens(msg: Dict[str, Any]) -> int:
    """Estimate tokens for a single OpenAI-format message.

    Multimodal parts (images, documents, audio) are billed at a flat per-part
    cost rather than by the character length of their base64 payload -- see
    ``media_part_token_cost``. Counting the payload as text overestimated a real
    screenshot by 145x and caused spurious compaction.
    """
    total = 4  # role + overhead
    content, media_tokens = _split_media_from_content(msg.get("content"))
    total += media_tokens
    normalized = normalize_content_value(content) or ""
    total += count_tokens(normalized)
    # Anthropic-native blocks stashed off to the side still cost real tokens.
    stashed = msg.get("_anthropic_content_blocks")
    if isinstance(stashed, list):
        for part in stashed:
            total += media_part_token_cost(part)
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            total += count_tokens(fn.get("name", ""))
            total += count_tokens(fn.get("arguments", ""))
        total += 3  # per-call overhead
    return total


def count_messages_tokens_builtin(messages: List[Dict[str, Any]]) -> int:
    """LCM's own estimate, bypassing any registered host counter.

    Exposed so a host can delegate selectively — using its own estimate only
    where it is better informed, and falling back to this for the rest — without
    reimplementing the per-message walk.
    """
    return sum(count_message_tokens(m) for m in messages)


def count_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate total tokens for a message list.

    Delegates to a host-supplied counter when one is registered (see
    ``set_messages_token_counter``), so an embedding application that already
    calibrates its estimates against real provider usage can make LCM's
    accounting agree with its own instead of maintaining a second, independent
    guess. Falls back to the built-in estimate when no counter is registered or
    the host's counter raises.
    """
    counter = _host_messages_token_counter
    if counter is not None:
        try:
            value = counter(messages)
            if isinstance(value, int) and value >= 0:
                return value
            logger.debug(
                "host token counter returned %r; using built-in estimate",
                type(value).__name__,
            )
        except Exception:
            logger.debug("host token counter failed; using built-in estimate",
                         exc_info=True)
    return count_messages_tokens_builtin(messages)
