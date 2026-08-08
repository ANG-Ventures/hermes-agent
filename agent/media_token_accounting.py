"""Cheap, fail-open token accounting for base64 PDF and audio parts.

The wire payload size is not a token-pricing signal. Providers bill PDF input by
page and audio input by duration, so this module inspects only the metadata
needed for those quantities and preserves the historical flat cost when it
cannot parse them safely.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Any, Optional

IMAGE_TOKEN_COST = 1_500
DOCUMENT_TOKEN_FALLBACK = 3_000
AUDIO_TOKEN_FALLBACK = 1_500

IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
DOCUMENT_PART_TYPES = frozenset({"document", "input_file", "file"})
AUDIO_PART_TYPES = frozenset({"input_audio", "audio"})
MEDIA_PART_TYPES = IMAGE_PART_TYPES | DOCUMENT_PART_TYPES | AUDIO_PART_TYPES

# Rates are intentionally provider-named rather than pretending transport bytes
# imply one universal price. The conservative value from each documented range
# is used because undercounting delays compaction until the provider rejects the
# request. Unknown provider wire shapes use the highest known rate for that
# modality.
_PROVIDER_MEDIA_RATES = {
    "anthropic": {"document_tokens_per_page": 3_000},
    "openai": {"audio_tokens_per_second": 10},
}

_PDF_PAGE_OBJECT_RE = re.compile(rb"/Type\s*/Page\b")
_MAX_PDF_INSPECTION_BYTES = 64 * 1024 * 1024
_MAX_AUDIO_HEADER_BYTES = 1024 * 1024

_MPEG1_LAYER3_BITRATES = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
)
_MPEG2_LAYER3_BITRATES = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
)
_MPEG1_SAMPLE_RATES = (44_100, 48_000, 32_000)


def _part_provider(part: dict[str, Any]) -> Optional[str]:
    part_type = part.get("type")
    if part_type == "document" or isinstance(part.get("source"), dict):
        return "anthropic"
    if part_type in {"input_file", "file", "input_audio"}:
        return "openai"
    return None


def _provider_rate(part: dict[str, Any], key: str) -> int:
    provider = _part_provider(part)
    provider_rate = _PROVIDER_MEDIA_RATES.get(provider or "", {}).get(key)
    if provider_rate:
        return provider_rate
    return max(rates[key] for rates in _PROVIDER_MEDIA_RATES.values() if key in rates)


def _part_base64_value(part: dict[str, Any]) -> Optional[str]:
    containers = [
        part.get("source"),
        part.get("input_audio"),
        part.get("input_file"),
        part.get("file"),
        part,
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("data", "file_data"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _base64_payload(value: str) -> Optional[str]:
    payload = value.strip()
    if payload.lower().startswith("data:"):
        header, separator, payload = payload.partition(",")
        if not separator or ";base64" not in header.lower():
            return None
    if not payload or len(payload) % 4:
        return None
    return payload


def _decoded_size(payload: str) -> Optional[int]:
    padding = len(payload) - len(payload.rstrip("="))
    if padding > 2 or "=" in payload[: -padding or None]:
        return None
    return (len(payload) // 4) * 3 - padding


def _decode_part_base64(
    part: dict[str, Any],
    *,
    max_decoded_bytes: Optional[int] = None,
) -> Optional[tuple[bytes, int]]:
    value = _part_base64_value(part)
    if value is None:
        return None
    payload = _base64_payload(value)
    if payload is None:
        return None
    total_size = _decoded_size(payload)
    if total_size is None:
        return None
    if max_decoded_bytes is None:
        encoded = payload
    else:
        encoded_chars = min(len(payload), ((max_decoded_bytes + 2) // 3) * 4)
        encoded_chars -= encoded_chars % 4
        encoded = payload[:encoded_chars]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return decoded, total_size


def _pdf_page_count(part: dict[str, Any]) -> Optional[int]:
    encoded = _part_base64_value(part)
    if encoded is None:
        return None
    payload = _base64_payload(encoded)
    if payload is None:
        return None
    total_size = _decoded_size(payload)
    if total_size is None or total_size > _MAX_PDF_INSPECTION_BYTES:
        return None
    decoded = _decode_part_base64(part)
    if decoded is None:
        return None
    raw, _ = decoded
    if b"%PDF-" not in raw[:1024]:
        return None
    page_count = len(_PDF_PAGE_OBJECT_RE.findall(raw))
    return page_count or None


def _wav_duration_seconds(raw: bytes, total_size: int) -> Optional[float]:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    riff_end = 8 + int.from_bytes(raw[4:8], "little")
    if riff_end > total_size or riff_end < 12:
        return None

    byte_rate: Optional[int] = None
    offset = 12
    scan_end = min(len(raw), riff_end)
    while offset + 8 <= scan_end:
        chunk_type = raw[offset : offset + 4]
        chunk_size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + chunk_size

        if chunk_type == b"fmt ":
            if chunk_size < 16 or data_start + 16 > scan_end:
                return None
            channels = int.from_bytes(raw[data_start + 2 : data_start + 4], "little")
            sample_rate = int.from_bytes(raw[data_start + 4 : data_start + 8], "little")
            byte_rate = int.from_bytes(raw[data_start + 8 : data_start + 12], "little")
            block_align = int.from_bytes(
                raw[data_start + 12 : data_start + 14], "little"
            )
            if not channels or not sample_rate or not byte_rate or not block_align:
                return None
        elif chunk_type == b"data":
            if data_end > total_size or data_end > riff_end or not byte_rate:
                return None
            duration = chunk_size / byte_rate
            return duration if duration > 0 else None

        offset = data_end + (chunk_size & 1)
        if offset > scan_end:
            return None
    return None


def _mp3_header(raw: bytes, offset: int) -> Optional[dict[str, int]]:
    if offset + 4 > len(raw):
        return None
    header = int.from_bytes(raw[offset : offset + 4], "big")
    if header >> 21 != 0x7FF:
        return None

    version_bits = (header >> 19) & 0b11
    layer_bits = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0xF
    sample_rate_index = (header >> 10) & 0b11
    if version_bits == 0b01 or layer_bits != 0b01:
        return None
    if bitrate_index in {0, 0xF} or sample_rate_index == 0b11:
        return None

    if version_bits == 0b11:
        version = 1
        bitrate = _MPEG1_LAYER3_BITRATES[bitrate_index]
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index]
        samples_per_frame = 1_152
        coefficient = 144
    else:
        version = 2 if version_bits == 0b10 else 25
        bitrate = _MPEG2_LAYER3_BITRATES[bitrate_index]
        divisor = 2 if version == 2 else 4
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index] // divisor
        samples_per_frame = 576
        coefficient = 72

    padding = (header >> 9) & 1
    frame_length = (coefficient * bitrate * 1_000) // sample_rate + padding
    if frame_length < 4:
        return None
    return {
        "version": version,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "samples_per_frame": samples_per_frame,
        "frame_length": frame_length,
        "has_crc": 0 if ((header >> 16) & 1) else 1,
        "channel_mode": (header >> 6) & 0b11,
    }


def _id3v2_end(raw: bytes) -> Optional[int]:
    if not raw.startswith(b"ID3"):
        return 0
    if len(raw) < 10 or any(value & 0x80 for value in raw[6:10]):
        return None
    tag_size = (raw[6] << 21) | (raw[7] << 14) | (raw[8] << 7) | raw[9]
    footer_size = 10 if raw[5] & 0x10 else 0
    return 10 + tag_size + footer_size


def _xing_frame_count(raw: bytes, offset: int, header: dict[str, int]) -> Optional[int]:
    if header["version"] == 1:
        side_info = 17 if header["channel_mode"] == 0b11 else 32
    else:
        side_info = 9 if header["channel_mode"] == 0b11 else 17
    xing_offset = offset + 4 + (2 if header["has_crc"] else 0) + side_info
    if raw[xing_offset : xing_offset + 4] in {b"Xing", b"Info"}:
        if xing_offset + 12 > len(raw):
            return None
        flags = int.from_bytes(raw[xing_offset + 4 : xing_offset + 8], "big")
        if flags & 1:
            count = int.from_bytes(raw[xing_offset + 8 : xing_offset + 12], "big")
            return count or None

    vbri_offset = offset + 4 + 32
    if raw[vbri_offset : vbri_offset + 4] == b"VBRI" and vbri_offset + 18 <= len(raw):
        count = int.from_bytes(raw[vbri_offset + 14 : vbri_offset + 18], "big")
        return count or None
    return None


def _mp3_duration_seconds(raw: bytes, total_size: int) -> Optional[float]:
    search_start = _id3v2_end(raw)
    if search_start is None or search_start >= len(raw):
        return None
    search_end = min(len(raw) - 4, search_start + 65_536)

    for offset in range(search_start, search_end + 1):
        first = _mp3_header(raw, offset)
        if first is None:
            continue

        xing_count = _xing_frame_count(raw, offset, first)
        if xing_count:
            return xing_count * first["samples_per_frame"] / first["sample_rate"]

        frame_lengths = []
        cursor = offset
        for _ in range(64):
            current = _mp3_header(raw, cursor)
            if current is None:
                break
            if any(
                current[key] != first[key]
                for key in ("version", "bitrate", "sample_rate", "samples_per_frame")
            ):
                break
            frame_lengths.append(current["frame_length"])
            cursor += current["frame_length"]
            if cursor + 4 > len(raw):
                break
        if len(frame_lengths) < 2:
            continue

        average_frame_length = sum(frame_lengths) / len(frame_lengths)
        frame_count = round((total_size - offset) / average_frame_length)
        if frame_count <= 0:
            return None
        return frame_count * first["samples_per_frame"] / first["sample_rate"]
    return None


def _audio_duration_seconds(part: dict[str, Any]) -> Optional[float]:
    decoded = _decode_part_base64(part, max_decoded_bytes=_MAX_AUDIO_HEADER_BYTES)
    if decoded is None:
        return None
    raw, total_size = decoded

    nested = part.get("input_audio")
    audio_format = nested.get("format") if isinstance(nested, dict) else None
    if not audio_format:
        audio_format = part.get("format")
    normalized_format = str(audio_format or "").lower().lstrip(".")

    if normalized_format in {"wav", "wave", "audio/wav", "audio/x-wav"} or (
        raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    ):
        return _wav_duration_seconds(raw, total_size)
    if normalized_format in {"mp3", "mpeg", "audio/mpeg", "audio/mp3"} or (
        raw.startswith(b"ID3") or _mp3_header(raw, 0) is not None
    ):
        return _mp3_duration_seconds(raw, total_size)
    return None


def media_part_token_cost(
    part: Any, *, image_token_cost: int = IMAGE_TOKEN_COST
) -> int:
    """Return provider-rate media tokens, or 0 when *part* is ordinary text.

    Parse failures intentionally return the historical flat document/audio
    costs. Token estimation is a safety valve and must never reject a request
    merely because an attachment uses an uncommon or malformed container.
    """
    if not isinstance(part, dict):
        return 0
    part_type = part.get("type")
    if part_type in IMAGE_PART_TYPES:
        return image_token_cost
    if part_type in DOCUMENT_PART_TYPES:
        page_count = _pdf_page_count(part)
        if page_count is None:
            return DOCUMENT_TOKEN_FALLBACK
        return page_count * _provider_rate(part, "document_tokens_per_page")
    if part_type in AUDIO_PART_TYPES:
        duration = _audio_duration_seconds(part)
        if duration is None:
            return AUDIO_TOKEN_FALLBACK
        rate = _provider_rate(part, "audio_tokens_per_second")
        return max(1, math.ceil(duration * rate))
    return 0
