"""Finished / unfinished output split -- the ONE producer shared by every token surface.

Moved verbatim (pure move, no behaviour change) from tokens-ace
``src/tokens_data/aggregations.py`` so tokens.ace and subs.ace classify output with the
same code instead of two copies that drift (house rule: one producer, N consumers; the
usage surfaces share ``agent.account_usage`` for the same reason).

A turn's output splits into FINISHED (the last API call's output: the answer the user saw)
and UNFINISHED (every earlier call's output: tool-call turns, retries). The split is known
only when EVERY call entry in the turn's ``comp_calls_json`` carries a top-level
``output_tokens``; otherwise the whole billed output is ``output_pre_split`` -- never
coerced to zero unfinished output. A known split whose parts do not sum to the billed
output fails closed to pre-split and is counted in ``output_split_reclassified_count``.

Stdlib only: consumers import this from the runtime tree without the agent's dependencies.
"""
from __future__ import annotations

import json
from typing import Any

def _normalize_call(entry):
    """Return ``(composition, output_tokens, reasoning_tokens)`` for old/new call blobs.

    NEW shape is identified ONLY by top-level ``output_tokens``. OLD rows store
    the composition dict itself, so their per-call output is unknown.
    """
    if isinstance(entry, dict) and "output_tokens" in entry:
        try:
            out = int(entry.get("output_tokens") or 0)
        except (TypeError, ValueError):
            out = 0
        try:
            reasoning = int(entry.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            reasoning = 0
        return entry.get("composition"), out, reasoning
    return entry, None, None


def _loads_comp_calls(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw == "":
            return None
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw if isinstance(raw, list) else None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def turn_output_split(comp_calls, output_billed) -> tuple["int | None", "int | None"]:
    """Return ``(finished_output, unfinished_output)`` or ``(None, None)``.

    A turn's split is known only when EVERY call entry has a top-level
    ``output_tokens`` field. Missing/old/mixed blobs stay unknown; they are never
    coerced to zero unfinished output.
    """
    calls = _loads_comp_calls(comp_calls)
    if not calls:
        return (None, None)
    outs = [_normalize_call(entry)[1] for entry in calls]
    if any(o is None for o in outs):
        return (None, None)
    try:
        billed = int(output_billed or 0)
    except (TypeError, ValueError):
        billed = 0
    finished = int(outs[-1] or 0)
    return (finished, max(0, billed - finished))


_OUTPUT_SPLIT_FIELDS: tuple[str, ...] = (
    "output_split_known",
    "finished_output",
    "unfinished_output",
    "output_pre_split",
    "output_split_reclassified_count",
)


def _blank_output_split_fields() -> dict[str, int]:
    return {k: 0 for k in _OUTPUT_SPLIT_FIELDS}


def _output_split_bucket(comp_calls_json, output_billed) -> dict[str, int]:
    """Classify one turn's output into known split vs pre-split buckets.

    Unknown/old/empty ``comp_calls_json`` contributes all output to
    ``output_pre_split``. If the helper ever returns a known split whose sum
    does not match this turn's billed output, fail closed to pre-split and count
    the reclassification instead of emitting a silent gap or wrong split.
    """
    billed = max(0, _safe_int(output_billed))
    row = _blank_output_split_fields()
    finished, unfinished = turn_output_split(comp_calls_json, billed)
    if (finished, unfinished) == (None, None):
        row["output_pre_split"] = billed
        return row
    finished_i = max(0, _safe_int(finished))
    unfinished_i = max(0, _safe_int(unfinished))
    if finished_i + unfinished_i != billed:
        row["output_pre_split"] = billed
        row["output_split_reclassified_count"] = 1
        return row
    row["output_split_known"] = 1
    row["finished_output"] = finished_i
    row["unfinished_output"] = unfinished_i
    return row


def _add_output_split_bucket(dst: dict[str, Any], bucket: dict[str, int]) -> None:
    for k in _OUTPUT_SPLIT_FIELDS:
        dst[k] = dst.get(k, 0) + (bucket.get(k, 0) or 0)


# Public names. The underscored originals stay importable under their old names so the
# tokens-ace re-export is a pure move.
normalize_call = _normalize_call
loads_comp_calls = _loads_comp_calls
OUTPUT_SPLIT_FIELDS = _OUTPUT_SPLIT_FIELDS
blank_output_split_fields = _blank_output_split_fields
output_split_bucket = _output_split_bucket
add_output_split_bucket = _add_output_split_bucket

__all__ = [
    "turn_output_split", "normalize_call", "loads_comp_calls", "OUTPUT_SPLIT_FIELDS",
    "blank_output_split_fields", "output_split_bucket", "add_output_split_bucket",
]
