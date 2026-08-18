"""Serialized request-body byte budgeting and image-aware remediation.

Token accounting cannot predict HTTP request size: base64 images add megabytes
of JSON bytes while contributing only a flat token estimate.  This module
measures the SDK-ready kwargs, then rewrites only the per-request copy when a
provider profile declares a byte ceiling.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# SDK request options that affect transport/headers but are not JSON body keys.
_NON_BODY_KWARGS = frozenset(
    {
        "timeout",
        "extra_headers",
        "extra_query",
    }
)

# Subscription relays that forward the native Anthropic Messages wire exactly.
# Bridge relays (claude-bpr / claude-bpx-N) are deliberately excluded because
# the real claude binary owns their Chat Completions request assembly.
_CLAUDE_ANTHROPIC_RELAY_RE = re.compile(r"^claude-(?:apr|apx-\d+)$")


@dataclass(frozen=True)
class RequestBodyRemediation:
    """Result of enforcing one provider's serialized request-body ceiling."""

    request_kwargs: dict[str, Any]
    before_bytes: int
    after_bytes: int
    max_body_bytes: int
    image_bytes: int
    image_count: int
    resized_images: int = 0
    evicted_images: int = 0

    @property
    def fits(self) -> bool:
        return self.after_bytes <= self.max_body_bytes


class RequestBodyBudgetExceeded(ValueError):
    """Raised before transport when image remediation cannot satisfy the cap."""

    def __init__(self, result: RequestBodyRemediation) -> None:
        self.result = result
        super().__init__(body_budget_error(result))


@dataclass(frozen=True)
class _ImageRef:
    part_path: tuple[Any, ...]
    data_path: tuple[Any, ...]
    data_url: str
    payload_chars: int
    raw_bytes: int
    turn_number: int
    part_type: str


def _request_body_payload(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Return the JSON body shape produced by OpenAI/Anthropic SDK kwargs.

    Both SDKs keep request controls (timeout/headers/query) off-body and merge
    ``extra_body`` at the top level before compact JSON serialization.
    """
    body = {
        key: value
        for key, value in request_kwargs.items()
        if key not in _NON_BODY_KWARGS
        and key != "extra_body"
        and not str(key).startswith("_")
    }
    extra_body = request_kwargs.get("extra_body")
    if isinstance(extra_body, Mapping):
        body.update(extra_body)
    return body


def serialized_request_body_size(request_kwargs: Mapping[str, Any]) -> int:
    """Measure compact UTF-8 JSON bytes for SDK-ready request kwargs."""
    payload = _request_body_payload(request_kwargs)
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def request_body_limit_for_provider(
    provider: str | None,
    model: str | None,
    *,
    api_mode: str | None = None,
) -> int | None:
    """Resolve a provider/model byte cap from its declarative profile."""
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider or "")
        # Named custom relays do not have a ProviderProfile. When they use the
        # native Anthropic Messages protocol, inherit that protocol's declared
        # body ceiling rather than waiting for the upstream 413.
        if profile is None and api_mode == "anthropic_messages":
            profile = get_provider_profile("anthropic")
        if profile is None:
            return None
        cap = profile.get_max_request_body_bytes(model)
        # APR/APX profiles are out-of-tree fleet configuration and may predate
        # this field. They proxy Anthropic's exact wire, so an unset cap inherits
        # the native protocol limit. Other named profiles retain their own None.
        if (
            cap is None
            and api_mode == "anthropic_messages"
            and _CLAUDE_ANTHROPIC_RELAY_RE.fullmatch(provider or "")
        ):
            anthropic = get_provider_profile("anthropic")
            if anthropic is not None:
                cap = anthropic.get_max_request_body_bytes(model)
        if cap is None:
            return None
        cap = int(cap)
        return cap if cap > 0 else None
    except Exception:
        return None


def request_body_limit_from_error(error: BaseException) -> int | None:
    """Parse a provider-reported hard byte cap and retain five percent headroom."""
    text = str(error).lower()
    match = re.search(r"(?:max(?:imum)?|limit)\D{0,24}(\d{6,})\s*bytes?", text)
    if match:
        return max(1, int(match.group(1)) * 95 // 100)
    match = re.search(r"(?:max(?:imum)?|limit)\D{0,24}(\d+(?:\.\d+)?)\s*(mib|mb)\b", text)
    if match:
        multiplier = 1024 * 1024 if match.group(2) == "mib" else 1_000_000
        return max(1, int(float(match.group(1)) * multiplier * 0.95))
    return None


def format_byte_count(value: int) -> str:
    """Format byte counts compactly for actionable provider errors."""
    value = max(0, int(value))
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def body_budget_error(result: RequestBodyRemediation) -> str:
    """Build the fail-fast message for a body that cannot be remediated."""
    image_word = "image" if result.image_count == 1 else "images"
    detail = (
        f"body={format_byte_count(result.after_bytes)}"
        + (
            f" (initially {format_byte_count(result.before_bytes)})"
            if result.after_bytes != result.before_bytes
            else ""
        )
        + f", cap={format_byte_count(result.max_body_bytes)}, "
        + f"images={format_byte_count(result.image_bytes)} across "
        + f"{result.image_count} {image_word}"
    )
    if result.image_count == 0:
        action = "No image payloads are available to resize or evict. Reduce text/tool output or run /new to start a new session."
    else:
        action = "Image resizing and oldest-first eviction could not make the request fit. Reduce attachments or run /new to start a new session."
    return f"Request body too large before provider dispatch: {detail}. {action}"


def _raw_base64_size(data: str) -> int:
    """Return decoded byte length without allocating another copy of the blob."""
    if not data:
        return 0
    padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    return max(0, (len(data) * 3) // 4 - padding)


def _turn_number(path: tuple[Any, ...]) -> int:
    for index, segment in enumerate(path[:-1]):
        if segment in {"messages", "input"} and isinstance(path[index + 1], int):
            return path[index + 1] + 1
    return 1


def _collect_image_refs(root: Any) -> list[_ImageRef]:
    refs: list[_ImageRef] = []

    def walk(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, Mapping):
            part_type = str(node.get("type") or "")
            if part_type in {"image_url", "input_image"}:
                image_value = node.get("image_url")
                data_path: tuple[Any, ...] | None = None
                data_url: str | None = None
                if isinstance(image_value, str):
                    data_path = path + ("image_url",)
                    data_url = image_value
                elif isinstance(image_value, Mapping) and isinstance(image_value.get("url"), str):
                    data_path = path + ("image_url", "url")
                    data_url = image_value["url"]
                if data_path and data_url and data_url.startswith("data:image/"):
                    _header, _comma, encoded = data_url.partition(",")
                    if encoded:
                        refs.append(
                            _ImageRef(
                                part_path=path,
                                data_path=data_path,
                                data_url=data_url,
                                payload_chars=len(data_url),
                                raw_bytes=_raw_base64_size(encoded),
                                turn_number=_turn_number(path),
                                part_type=part_type,
                            )
                        )
                        return
            elif part_type == "image":
                source = node.get("source")
                if (
                    isinstance(source, Mapping)
                    and source.get("type") == "base64"
                    and isinstance(source.get("data"), str)
                    and source.get("data")
                ):
                    encoded = source["data"]
                    media_type = str(source.get("media_type") or "image/jpeg")
                    if not media_type.startswith("image/"):
                        media_type = "image/jpeg"
                    refs.append(
                        _ImageRef(
                            part_path=path,
                            data_path=path + ("source", "data"),
                            data_url=f"data:{media_type};base64,{encoded}",
                            payload_chars=len(encoded),
                            raw_bytes=_raw_base64_size(encoded),
                            turn_number=_turn_number(path),
                            part_type=part_type,
                        )
                    )
                    return

            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for index, value in enumerate(node):
                walk(value, path + (index,))

    walk(root, ())
    return refs


def _replace_at_path(root: Any, path: tuple[Any, ...], replacement: Any) -> Any:
    """Copy only containers on *path*, leaving the source request untouched."""
    if not path:
        return replacement
    head, *tail = path
    if isinstance(root, Mapping):
        clone = dict(root)
        clone[head] = _replace_at_path(root[head], tuple(tail), replacement)
        return clone
    if isinstance(root, list):
        clone = list(root)
        clone[head] = _replace_at_path(root[head], tuple(tail), replacement)
        return clone
    if isinstance(root, tuple):
        clone = list(root)
        clone[head] = _replace_at_path(root[head], tuple(tail), replacement)
        return tuple(clone)
    raise TypeError(f"Cannot replace request path through {type(root).__name__}")


def _value_at_path(root: Any, path: tuple[Any, ...]) -> Any:
    value = root
    for segment in path:
        value = value[segment]
    return value


def _resize_data_url(data_url: str, target_chars: int) -> str | None:
    """Best-effort Pillow downscale to a smaller base64 data URL."""
    if target_chars <= 0 or len(data_url) <= target_chars:
        return None
    header, comma, encoded = data_url.partition(",")
    if not comma or not header.startswith("data:image/") or not encoded:
        return None
    media_type = header[len("data:") :].split(";", 1)[0]
    suffix = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/bmp": ".bmp",
    }.get(media_type, ".img")
    try:
        raw = base64.b64decode(encoded, validate=True)
        from tools.vision_tools import _resize_image_for_vision

        temp = tempfile.NamedTemporaryFile(
            prefix="hermes_body_budget_",
            suffix=suffix,
            delete=False,
        )
        try:
            temp.write(raw)
            temp.close()
            resized = _resize_image_for_vision(
                Path(temp.name),
                mime_type=media_type,
                max_base64_bytes=target_chars,
            )
        finally:
            try:
                Path(temp.name).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("request-body image resize skipped: %s", exc)
        return None
    if resized and len(resized) < len(data_url):
        return resized
    return None


def _eviction_placeholder(ref: _ImageRef) -> dict[str, str]:
    size_mb = ref.raw_bytes / (1024 * 1024)
    text = f"[image evicted: {size_mb:.1f} MB screenshot, turn {ref.turn_number}]"
    return {
        "type": "input_text" if ref.part_type == "input_image" else "text",
        "text": text,
    }


def remediate_request_body(
    request_kwargs: Mapping[str, Any],
    *,
    max_body_bytes: int,
) -> RequestBodyRemediation:
    """Resize images, then evict oldest images until the request fits.

    The returned request is copy-on-write; persistent conversation history is
    never degraded.  Image traversal is recursive so Anthropic-native images
    nested inside ``tool_result.content`` remain evictable even in the fresh
    tail that text compression intentionally protects.
    """
    current: dict[str, Any] = dict(request_kwargs)
    before = serialized_request_body_size(current)
    initial_refs = _collect_image_refs(current)
    image_bytes = sum(ref.raw_bytes for ref in initial_refs)
    if before <= max_body_bytes:
        return RequestBodyRemediation(
            request_kwargs=current,
            before_bytes=before,
            after_bytes=before,
            max_body_bytes=max_body_bytes,
            image_bytes=image_bytes,
            image_count=len(initial_refs),
        )

    resized_count = 0
    evicted_count = 0

    # Allocate the bytes left after non-image JSON evenly across images.  The
    # helper may undershoot (it halves dimensions), which is useful headroom.
    image_payload_chars = sum(ref.payload_chars for ref in initial_refs)
    fixed_bytes = max(0, before - image_payload_chars)
    reserve = min(1024, max_body_bytes // 100)
    available_image_chars = max(0, max_body_bytes - fixed_bytes - reserve)
    target_per_image = (
        max(1024, available_image_chars // len(initial_refs))
        if initial_refs and available_image_chars
        else 0
    )

    if target_per_image:
        # Largest first gets under budget with the fewest expensive Pillow runs.
        for ref in sorted(initial_refs, key=lambda item: item.payload_chars, reverse=True):
            if serialized_request_body_size(current) <= max_body_bytes:
                break
            if ref.payload_chars <= target_per_image:
                continue
            resized = _resize_data_url(ref.data_url, target_per_image)
            if resized is None:
                continue
            if ref.part_type == "image":
                source_path = ref.part_path + ("source",)
                source = dict(_value_at_path(current, source_path))
                source["media_type"] = resized[len("data:") :].split(";", 1)[0]
                source["data"] = resized.partition(",")[2]
                current = _replace_at_path(current, source_path, source)
            else:
                current = _replace_at_path(
                    current,
                    ref.data_path,
                    resized,
                )
            resized_count += 1

    # Resizing is quality-preserving recovery.  Only after it cannot satisfy
    # the cap do we replace images, oldest request turn first.
    if serialized_request_body_size(current) > max_body_bytes:
        for ref in sorted(
            _collect_image_refs(current),
            key=lambda item: (item.turn_number, item.part_path),
        ):
            if serialized_request_body_size(current) <= max_body_bytes:
                break
            current = _replace_at_path(
                current,
                ref.part_path,
                _eviction_placeholder(ref),
            )
            evicted_count += 1

    after = serialized_request_body_size(current)
    return RequestBodyRemediation(
        request_kwargs=current,
        before_bytes=before,
        after_bytes=after,
        max_body_bytes=max_body_bytes,
        image_bytes=image_bytes,
        image_count=len(initial_refs),
        resized_images=resized_count,
        evicted_images=evicted_count,
    )


__all__ = [
    "RequestBodyBudgetExceeded",
    "RequestBodyRemediation",
    "body_budget_error",
    "format_byte_count",
    "remediate_request_body",
    "request_body_limit_for_provider",
    "request_body_limit_from_error",
    "serialized_request_body_size",
]
