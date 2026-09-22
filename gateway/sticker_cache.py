"""
Sticker description cache for Telegram.

When users send stickers, we describe them via the vision tool and cache
the descriptions keyed by file_unique_id so we don't re-analyze the same
sticker image on every send. Descriptions are concise (1-2 sentences).

Cache location: ~/.hermes/sticker_cache.json
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from typing import Optional

from hermes_cli.config import get_hermes_home


CACHE_PATH = get_hermes_home() / "sticker_cache.json"

# Vision prompt for describing stickers -- kept concise to save tokens
STICKER_VISION_PROMPT = (
    "Describe this sticker in 1-2 sentences. Focus on what it depicts -- "
    "character, action, emotion. Be concise and objective."
)


def _load_cache() -> dict:
    """Load the sticker cache from disk."""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# Serializes the read-modify-write in ``cache_sticker_description``.
#
# ``atomic_replace`` makes each individual WRITE atomic; it does not make the
# load/mutate/save TRIPLE atomic.  On the pre-existing inline code path the
# event loop happened to serialize every caller, so the race could not be
# observed.  ``cache_sticker_description_async`` below moves the write to a
# worker thread, which removes that accidental serialization -- so the lock has
# to be added in the SAME change that introduces the concurrency, or two
# stickers described at once silently drop one of the two descriptions.
#
# Re-entrant because the async wrapper dispatches straight into the sync form.
_CACHE_LOCK = threading.RLock()


def _save_cache(cache: dict) -> None:
    """Save the sticker cache to disk atomically."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(CACHE_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CACHE_PATH))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_cached_description(file_unique_id: str) -> Optional[dict]:
    """
    Look up a cached sticker description.

    Returns:
        dict with keys {description, emoji, set_name, cached_at} or None.
    """
    cache = _load_cache()
    return cache.get(file_unique_id)


def cache_sticker_description(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> None:
    """
    Store a sticker description in the cache.

    Args:
        file_unique_id: Telegram's stable sticker identifier.
        description:    Vision-generated description text.
        emoji:          Associated emoji (e.g. "😀").
        set_name:       Sticker set name if available.
    """
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[file_unique_id] = {
            "description": description,
            "emoji": emoji,
            "set_name": set_name,
            "cached_at": time.time(),
        }
        _save_cache(cache)


async def cache_sticker_description_async(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> None:
    """Off-loop form of :func:`cache_sticker_description`.

    ``_save_cache`` ends in ``os.fsync`` + ``os.replace``, whose duration is
    unbounded under filesystem pressure -- the exact tail that blocked the
    Apollo event loop for 30s on 2026-09-20.  Telegram's ``_handle_sticker``
    is an inbound-message coroutine, so it must not pay that inline.

    The sync form keeps its exact contract for the non-loop callers (it is the
    public API and is what this wrapper dispatches to), so it is not removed.
    """
    await asyncio.to_thread(
        cache_sticker_description, file_unique_id, description, emoji, set_name
    )


def build_sticker_injection(
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> str:
    """
    Build the warm-style injection text for a sticker description.

    Returns a string like:
      [The user sent a sticker 😀 from "MyPack"~ It shows: "A cat waving" (=^.w.^=)]
    """
    context = ""
    if set_name and emoji:
        context = f" {emoji} from \"{set_name}\""
    elif emoji:
        context = f" {emoji}"

    return f"[The user sent a sticker{context}~ It shows: \"{description}\" (=^.w.^=)]"


def build_animated_sticker_injection(emoji: str = "") -> str:
    """
    Build injection text for animated/video stickers we can't analyze.
    """
    if emoji:
        return (
            f"[The user sent an animated sticker {emoji}~ "
            f"I can't see animated ones yet, but the emoji suggests: {emoji}]"
        )
    return "[The user sent an animated sticker~ I can't see animated ones yet]"
