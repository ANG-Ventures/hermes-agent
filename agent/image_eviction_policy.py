"""Send-path image eviction policy shared by both stateless outbound passes.

Two passes retire old tool-result images from the per-request copy of the conversation:
``agent.context_compressor.evict_stale_outbound_tool_images`` on the OpenAI-shaped list and
``agent.anthropic_message_convert._evict_old_screenshots`` on the Anthropic wire list. Both run
from scratch on a fresh clone every request, so they must agree on one policy or the second
pass re-evicts on a different frontier than the first (#113517). This module is stdlib-only so
the wire converter, a leaf, can import it without dragging in the compaction stack.

Why the trigger is a provider limit, not a keep-newest count: retiring an image edits a message
the provider has already cached, and Anthropic matches its prompt cache on an exact byte prefix.
A keep-newest-N window retires one more message on every new image, so every turn is a
full-prefix miss. Holding images until the request would cross a real API limit and then
retiring a batch costs one slower turn per batch and nothing below the limit.

Both numbers are Anthropic limits mirrored in the fleet catalog ``config/anthropic-limits.json``
(hermes-home, ``images.*``; card t_3a0b1ce4), read at CALL time through
:func:`agent.anthropic_limits.image_limits` so a catalog edit applies without a restart; the literals
below are only the fallback when the catalog is unreadable, and hermes-home's
``anthropic-limits-fallback-lint`` holds ``OUTBOUND_IMAGE_BUDGET_BYTES`` equal to
``images.outbound_budget_bytes``. ``images.stricter_cap_threshold_count`` (20) is the documented
image count above which Anthropic applies a stricter per-image dimension cap to EVERY image in the
request, counting images nested in tool_result content; since 2026-10-29 that cap is 3000 px (was
2000 px). The hard ceilings are higher (100 images per request on 200K-context models, 600
otherwise), but the request-size limit usually binds first: 128 MB on the first-party API since
2026-10-29 (was 32 MB; 20-32 MB through Bedrock/Vertex, catalog
``cloud_platform_request_body_bytes``). The byte budget is 75% of it (96 MB, was 24 MB), leaving
headroom for text.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

# Fallbacks for catalog images.stricter_cap_threshold_count / images.outbound_budget_bytes (see docstring).
OUTBOUND_IMAGE_LIMIT = 20
OUTBOUND_IMAGE_BUDGET_BYTES = 96_000_000
IMAGE_EVICTION_BATCH = 8
# Satisfiability floor — see outbound_image_retire_count.
OUTBOUND_IMAGE_FLOOR = 3


def _catalog_image_limits() -> Tuple[int, int]:
    """``(limit, budget)`` from the fleet catalog, else the literals. Imported lazily so this module stays a
    stdlib-only leaf at import time; any failure to read the catalog means the literals."""
    try:
        from agent.anthropic_limits import image_limits

        return image_limits(OUTBOUND_IMAGE_LIMIT, OUTBOUND_IMAGE_BUDGET_BYTES)
    except Exception:  # noqa: BLE001 - the send path must never fail on the catalog
        return OUTBOUND_IMAGE_LIMIT, OUTBOUND_IMAGE_BUDGET_BYTES


def outbound_image_retire_count(
    carrier_blocks_newest_first: Sequence[int],
    reserved_blocks: int,
    *,
    carrier_bytes_newest_first: Optional[Sequence[int]] = None,
    reserved_bytes: int = 0,
    limit: Optional[int] = None,
    budget: Optional[int] = None,
    batch: int = IMAGE_EVICTION_BATCH,
    floor: int = OUTBOUND_IMAGE_FLOOR,
) -> int:
    """How many of the OLDEST image-bearing tool results to retire.

    ``carrier_blocks_newest_first`` is the image-block count per image-bearing tool result,
    newest first; ``reserved_blocks`` counts images the pass must never rewrite (user
    uploads). The byte dimension is active only when ``carrier_bytes_newest_first`` is
    given (the wire pass has no sizes).

    The retire count must be a STEP FUNCTION of the overshoot, because the pass is recomputed
    on every request: an exact ``count - limit`` target moves the frontier on every new image,
    and a fixed one-batch retire stops enforcing the limit after the first batch. So the count
    advances in quanta until the request fits.

    The quantum is ``batch`` capped at ``window - floor``, where ``window`` is how many newest
    carriers fit under the ceiling. A quantum wider than that would step past the newest frames
    on every advance; cutting each step back to exactly ``total - floor`` instead makes the
    retire count track ``total`` again — the per-image frontier this policy exists to avoid,
    visible whenever a tool result carries several images or uploads fill most of the ceiling.
    Holding for ``window - floor`` turns per advance is the most the floor allows.

    The floor is a SATISFIABILITY floor: it shelters the newest frames only when reserved
    uploads alone breach the block ceiling (no retirement can fix that), and never under byte
    pressure — the request-size limit is hard and the provider answers 413.

    ``limit`` / ``budget`` default (None) to the catalog values, resolved per call; pass them to pin.
    """
    if limit is None or budget is None:
        cat_limit, cat_budget = _catalog_image_limits()
        limit = cat_limit if limit is None else limit
        budget = cat_budget if budget is None else budget
    total = len(carrier_blocks_newest_first)
    sizes = carrier_bytes_newest_first
    blocks_kept = [0] * (total + 1)
    bytes_kept = [0] * (total + 1)
    for i in range(total):
        blocks_kept[i + 1] = blocks_kept[i] + carrier_blocks_newest_first[i]
        bytes_kept[i + 1] = bytes_kept[i] + (sizes[i] if sizes is not None else 0)

    def _bytes_fit(kept: int) -> bool:
        return sizes is None or reserved_bytes + bytes_kept[kept] <= budget

    def _fits(kept: int) -> bool:
        return reserved_blocks + blocks_kept[kept] <= limit and _bytes_fit(kept)

    if _fits(total):
        return 0

    floor = min(max(floor, 0), total)
    max_retire = total - floor if not _fits(0) and _bytes_fit(floor) else total
    if max_retire <= 0:
        return 0
    window = max(k for k in range(total + 1) if _fits(k)) if _fits(0) else 0
    quantum = max(1, min(batch, window - floor))

    retire = 0
    while retire < max_retire:
        retire = min(retire + quantum, max_retire)
        if _fits(total - retire):
            break
    return retire
