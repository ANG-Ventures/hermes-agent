"""Provider-pricing token costs for multimodal content parts.

Media is billed by DIMENSION (images), PAGE (documents), or DURATION (audio) --
never by transport size. Counting a part's base64 payload as characters
overestimated a real 1710x1518 screenshot by ~145x and drove spurious
compaction (2026-08-07), so every counter routes media through here instead.

The parsers are deliberately cheap and total: they run on every message of every
turn, so they read only what a header requires, cap the bytes they inspect, and
NEVER raise -- an unparseable payload falls back to the flat per-part default
rather than failing a turn or (worse) reverting to character counting.

Rates are grounded in vendor documentation, not guesses:
  * images    -- Anthropic bills w*h/750; OpenAI 85 + 170/tile. A flat 1500 sits
                 between them for typical screenshots.
  * documents -- Anthropic's PDF docs state "each page typically uses 1,500-3,000
                 tokens per page depending on content density"; we use the
                 midpoint per page, so a 40-page PDF no longer reads as one flat
                 3000 (a ~40x under-count, which fires compaction too LATE).
  * audio     -- billed per second of duration. ~10 tokens/sec matches OpenAI's
                 audio-input accounting closely enough for a preflight estimate.
"""
from __future__ import annotations

import re
import struct
import zlib
from typing import Any, Optional

# --- Part-type vocabularies (shared so counters cannot disagree) -------------
IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
DOCUMENT_PART_TYPES = frozenset({"document", "input_file", "file"})
AUDIO_PART_TYPES = frozenset({"input_audio", "audio"})
MEDIA_PART_TYPES = IMAGE_PART_TYPES | DOCUMENT_PART_TYPES | AUDIO_PART_TYPES

# --- Rates ------------------------------------------------------------------
IMAGE_TOKEN_COST = 1500
DOCUMENT_TOKENS_PER_PAGE = 2250        # midpoint of Anthropic's documented 1500-3000
DOCUMENT_TOKEN_COST = 3000             # fallback when the page count is undecidable
AUDIO_TOKENS_PER_SECOND = 10
AUDIO_TOKEN_COST = 1500                # fallback when duration is undecidable

# Only ever decode/inspect this much of a payload. A page count lives in the
# structure, not the content, so a bounded read is enough -- and it keeps the
# cost of this function independent of attachment size.
_MAX_INSPECT_BYTES = 4 * 1024 * 1024
# Guardrails: a corrupt header must not yield an absurd bill.
_MAX_PAGES = 10_000
_MAX_AUDIO_SECONDS = 24 * 60 * 60


def _decode_payload(part: Any) -> Optional[bytes]:
    """Best-effort raw bytes for a media part, or None. Never raises."""
    try:
        import base64

        raw = None
        if isinstance(part, dict):
            src = part.get("source")
            if isinstance(src, dict) and isinstance(src.get("data"), str):
                raw = src["data"]
            if raw is None:
                for key in ("image_url", "input_audio", "file"):
                    holder = part.get(key)
                    if isinstance(holder, dict):
                        for field in ("url", "data", "file_data"):
                            if isinstance(holder.get(field), str):
                                raw = holder[field]
                                break
                    elif isinstance(holder, str):
                        raw = holder
                    if raw is not None:
                        break
            if raw is None and isinstance(part.get("data"), str):
                raw = part["data"]
        elif isinstance(part, str):
            raw = part
        if not isinstance(raw, str) or not raw:
            return None
        if raw.startswith("["):          # an externalized-payload marker, not data
            return None
        if "," in raw[:64] and raw.lstrip().startswith("data:"):
            raw = raw.split(",", 1)[1]
        # Cap BEFORE decoding; b64 length must be a multiple of 4.
        cap = (_MAX_INSPECT_BYTES // 3) * 4
        if len(raw) > cap:
            raw = raw[:cap]
        return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
    except Exception:
        return None


def pdf_page_count(data: bytes) -> Optional[int]:
    """Pages in a PDF, or None if undecidable. Cheap, bounded, never raises.

    Three strategies in increasing cost, because real PDFs vary in how they
    store the page tree (all three were validated against files on disk):
      1. ``/Type /Page`` occurrences  -- the common uncompressed case.
      2. ``max(/Count N)``            -- the page-tree root's own tally.
      3. inflate object streams       -- modern PDFs compress the catalog.
    """
    try:
        if not data or not data.startswith(b"%PDF"):
            return None
        n = len(re.findall(rb"/Type\s*/Page[^s]", data))
        if n:
            return min(n, _MAX_PAGES)
        counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", data)]
        if counts:
            return min(max(counts), _MAX_PAGES)
        total = 0
        for m in re.finditer(rb"stream\r?\n", data):
            start = m.end()
            end = data.find(b"endstream", start)
            if end == -1:
                continue
            try:
                inflated = zlib.decompress(data[start:end])
            except Exception:
                continue
            total += len(re.findall(rb"/Type\s*/Page[^s]", inflated))
            if total >= _MAX_PAGES:
                break
        return min(total, _MAX_PAGES) if total else None
    except Exception:
        return None


def audio_duration_seconds(data: bytes) -> Optional[float]:
    """Duration of a WAV/AIFF payload, or None. Cheap, bounded, never raises."""
    try:
        if not data or len(data) < 16:
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            pos, rate, bits, channels, nbytes = 12, None, None, None, None
            while pos + 8 <= len(data):
                cid = data[pos:pos + 4]
                size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
                if cid == b"fmt " and pos + 24 <= len(data):
                    channels = struct.unpack("<H", data[pos + 10:pos + 12])[0]
                    rate = struct.unpack("<I", data[pos + 12:pos + 16])[0]
                    bits = struct.unpack("<H", data[pos + 22:pos + 24])[0]
                elif cid == b"data":
                    nbytes = size
                    break
                pos += 8 + size + (size & 1)
            if rate and nbytes and bits and channels:
                per_sec = rate * channels * max(bits // 8, 1)
                if per_sec > 0:
                    return min(nbytes / float(per_sec), _MAX_AUDIO_SECONDS)
            return None
        if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
            pos = 12
            while pos + 8 <= len(data):
                cid = data[pos:pos + 4]
                size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
                if cid == b"COMM" and pos + 22 <= len(data):
                    frames = struct.unpack(">I", data[pos + 10:pos + 14])[0]
                    exponent = struct.unpack(">H", data[pos + 16:pos + 18])[0]
                    mantissa = struct.unpack(">I", data[pos + 18:pos + 22])[0]
                    rate = mantissa * (2.0 ** (exponent - 16383 - 31))
                    if rate > 0:
                        return min(frames / rate, _MAX_AUDIO_SECONDS)
                    return None
                pos += 8 + size + (size & 1)
        return None
    except Exception:
        return None


def media_part_token_cost(part: Any) -> int:
    """Token cost for a multimodal content part; 0 if it is not media.

    Returns 0 for text and anything unrecognized so callers fall through to
    normal character counting.
    """
    if not isinstance(part, dict):
        return 0
    part_type = part.get("type")
    if not part_type:
        return 0
    if part_type in IMAGE_PART_TYPES:
        return IMAGE_TOKEN_COST
    if part_type in DOCUMENT_PART_TYPES:
        pages = pdf_page_count(_decode_payload(part) or b"")
        return pages * DOCUMENT_TOKENS_PER_PAGE if pages else DOCUMENT_TOKEN_COST
    if part_type in AUDIO_PART_TYPES:
        seconds = audio_duration_seconds(_decode_payload(part) or b"")
        if seconds:
            return max(1, int(seconds * AUDIO_TOKENS_PER_SECOND))
        return AUDIO_TOKEN_COST
    return 0
