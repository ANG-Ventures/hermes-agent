"""Tests for request-composition telemetry (fixed vs non-fixed token buckets).

Covers:
  * compose_request_breakdown — bucket sum invariant, system-message skip,
    tool-result/arg accounting, image flat-cost, empty input.
  * blackbox store — comp_* column migration idempotency + round-trip,
    comp_calls_json blob persistence, NULL fallback for old rows.
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from agent.media_tokens import MEDIA_PART_TYPES
from agent.model_metadata import compose_request_breakdown


# --------------------------------------------------------------------------- #
# compose_request_breakdown
# --------------------------------------------------------------------------- #
def test_buckets_sum_exactly_to_total():
    msgs = [
        {"role": "user", "content": "U" * 4000},
        {"role": "assistant", "content": "A" * 2000,
         "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "x" * 800}}]},
        {"role": "tool", "content": "T" * 20000, "tool_call_id": "1"},
    ]
    comp = compose_request_breakdown(
        msgs, system_prompt="S" * 48000, tools=[{"function": {"name": "f"}}] * 10
    )
    # No scaling: fixed + nonfixed == total, and each subtotal is its parts.
    assert comp["fixed_tokens"] == comp["sys_tokens"] + comp["tool_schema_tokens"]
    assert comp["nonfixed_tokens"] == (
        comp["history_tokens"] + comp["tool_result_tokens"]
        + comp["tool_arg_tokens"] + comp["framing_tokens"]
    )
    assert comp["total_tokens"] == comp["fixed_tokens"] + comp["nonfixed_tokens"]
    assert comp["tool_result_count"] == 1
    # History message count = user + assistant (the tool message is excluded;
    # it's counted under tool_result_count, not conversation history).
    assert comp["history_message_count"] == 2


def test_skills_count_matches_index_bullet_lines():
    # build_skills_system_prompt renders each skill as "    - name[: desc]"
    # (4-space indent). Category headers use 2 spaces and must NOT be counted.
    skills_prompt = (
        "## Skills (mandatory)\n"
        "<available_skills>\n"
        "  github: GitHub workflow skills\n"
        "    - github-pr-workflow: open/merge PRs\n"
        "    - github-issues: triage issues\n"
        "  devops:\n"
        "    - docker-management: containers\n"
        "</available_skills>\n"
    )
    comp = compose_request_breakdown(
        [{"role": "user", "content": "hi"}],
        system_prompt="IDENTITY " + skills_prompt,
        skills_prompt=skills_prompt,
    )
    # Three "    - " bullets → 3 skills; the two category headers are ignored.
    assert comp["skills_count"] == 3
    assert comp["skills_tokens"] > 0


def test_skills_count_zero_when_no_skills_prompt():
    comp = compose_request_breakdown(
        [{"role": "user", "content": "hi"}], system_prompt="IDENTITY ONLY"
    )
    assert comp["skills_count"] == 0


def test_system_message_in_list_is_not_double_counted():
    sys_prompt = "S" * 40000
    base = [{"role": "user", "content": "hello world"}]
    with_sys = [{"role": "system", "content": sys_prompt}] + base
    a = compose_request_breakdown(with_sys, system_prompt=sys_prompt)
    b = compose_request_breakdown(base, system_prompt=sys_prompt)
    # The system message embedded in messages must NOT inflate history — it's
    # already accounted via system_prompt.
    assert a["history_tokens"] == b["history_tokens"]
    assert a["sys_tokens"] == b["sys_tokens"]


def test_skills_split_sums_to_sys_tokens():
    # skills_prompt is a substring of the system prompt; identity + skills must
    # equal sys_tokens exactly (no double-count, invariant preserved).
    skills = "K" * 14000
    sys_prompt = ("S" * 26000) + skills
    comp = compose_request_breakdown(
        [{"role": "user", "content": "hi"}],
        system_prompt=sys_prompt,
        skills_prompt=skills,
    )
    assert comp["skills_tokens"] > 0
    assert comp["identity_tokens"] + comp["skills_tokens"] == comp["sys_tokens"]
    assert comp["skills_tokens"] <= comp["sys_tokens"]


def test_skills_absent_yields_zero_skills_tokens():
    comp = compose_request_breakdown(
        [{"role": "user", "content": "hi"}], system_prompt="S" * 4000
    )
    assert comp["skills_tokens"] == 0
    assert comp["identity_tokens"] == comp["sys_tokens"]


def test_skills_prompt_longer_than_system_is_clamped():
    # Defensive: a stale/mismatched skills stash longer than the system prompt
    # must never make skills_tokens exceed sys_tokens (no negative identity).
    comp = compose_request_breakdown(
        [{"role": "user", "content": "hi"}],
        system_prompt="S" * 1000,
        skills_prompt="K" * 9000,
    )
    assert comp["skills_tokens"] <= comp["sys_tokens"]
    assert comp["identity_tokens"] >= 0
    assert comp["identity_tokens"] + comp["skills_tokens"] == comp["sys_tokens"]


def test_framing_tokens_scale_with_message_count():
    from agent.model_metadata import PER_MESSAGE_FRAMING_TOKENS

    msgs = [{"role": "user", "content": "x"}] * 7
    comp = compose_request_breakdown(msgs, system_prompt="s")
    assert comp["framing_tokens"] == PER_MESSAGE_FRAMING_TOKENS * 7
    # framing is part of non-fixed and the total invariant still holds.
    assert comp["framing_tokens"] <= comp["nonfixed_tokens"]
    assert comp["total_tokens"] == comp["fixed_tokens"] + comp["nonfixed_tokens"]


def test_framing_zero_for_empty_messages():
    comp = compose_request_breakdown([], system_prompt="s")
    assert comp["framing_tokens"] == 0


def test_framing_env_override(monkeypatch):
    # The constant is import-time; re-evaluate the helper directly to confirm
    # the env override + clamp behavior without reloading the module.
    import agent.model_metadata as mm

    monkeypatch.setenv("HERMES_PER_MESSAGE_FRAMING_TOKENS", "6")
    assert mm._per_message_framing_tokens() == 6
    monkeypatch.setenv("HERMES_PER_MESSAGE_FRAMING_TOKENS", "0")
    assert mm._per_message_framing_tokens() == 0  # 0 disables
    monkeypatch.setenv("HERMES_PER_MESSAGE_FRAMING_TOKENS", "999")
    assert mm._per_message_framing_tokens() == 4  # out of range -> default
    monkeypatch.setenv("HERMES_PER_MESSAGE_FRAMING_TOKENS", "junk")
    assert mm._per_message_framing_tokens() == 4  # non-numeric -> default


def test_tool_args_counted_separately_from_history():
    no_args = [{"role": "assistant", "content": "x" * 400}]
    with_args = [{"role": "assistant", "content": "x" * 400,
                  "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "y" * 4000}}]}]
    a = compose_request_breakdown(no_args)
    b = compose_request_breakdown(with_args)
    assert a["tool_arg_tokens"] == 0
    assert b["tool_arg_tokens"] > 0
    # History (the assistant text) is identical; only tool_arg differs.
    assert a["history_tokens"] == b["history_tokens"]


def test_image_parts_use_flat_cost_not_raw_length():
    big_b64 = "Q" * 1_000_000  # 1MB base64 would be ~250k tok if counted raw
    msg = {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}},
    ]}
    comp = compose_request_breakdown([msg])
    # Image flat cost is 1500; the raw base64 must not blow up history.
    assert comp["history_tokens"] < 5000
    assert comp["history_tokens"] >= 1500


# --------------------------------------------------------------------------- #
# Media double-billing regression (documents + audio, not just images)
#
# `_estimate_message_chars` (the char walk behind history_tokens) stripped only
# IMAGE part types while `_count_image_tokens` tallied EVERY media type at its
# provider price. Documents and audio were therefore billed twice: once
# correctly per page/second, and again as raw base64 characters. Measured on a
# synthetic 3-page PDF (533,580 base64 chars): 159,254 tokens instead of 6,788.
# --------------------------------------------------------------------------- #
def _synthetic_pdf(pages: int, pad_bytes: int) -> bytes:
    """Minimal PDF whose page tree yields exactly `pages`, padded to size."""
    objs = b"".join(
        b"%d 0 obj\n<< /Type /Page /Parent 1 0 R >>\nendobj\n" % (i + 2)
        for i in range(pages)
    )
    # Pad with a comment run so the payload is large without adding page nodes.
    return (
        b"%PDF-1.4\n" + objs + b"%" + (b"A" * pad_bytes) + b"\n"
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def _synthetic_wav(seconds: float, rate: int = 16000) -> bytes:
    """Minimal 16-bit mono WAV of the requested duration."""
    import struct

    per_sec = rate * 2
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    data = b"\x00" * int(seconds * per_sec)
    body = (
        b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _media_message(part: dict) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": "look at this"}, part]}


@pytest.mark.parametrize(
    "label, part_factory",
    [
        (
            "document_pdf",
            lambda: {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(
                        _synthetic_pdf(pages=3, pad_bytes=400_000)
                    ).decode(),
                },
            },
        ),
        (
            "audio",
            lambda: {
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(_synthetic_wav(seconds=12.5)).decode(),
                    "format": "wav",
                },
            },
        ),
    ],
)
def test_document_and_audio_base64_is_not_double_billed(label, part_factory):
    """Non-image media must be priced once, not once flat + once as base64."""
    from agent.media_tokens import media_part_token_cost
    from agent.model_metadata import estimate_messages_tokens_rough

    part = part_factory()
    msgs = [_media_message(part)]

    priced = media_part_token_cost(part)
    assert priced > 0, f"{label}: fixture is not recognized as media"

    rough = estimate_messages_tokens_rough(msgs)
    comp = compose_request_breakdown(msgs)
    billed = comp["total_tokens"]

    # `estimate_messages_tokens_rough` already strips media correctly; the
    # breakdown must agree with it rather than re-billing the base64 payload.
    # The small delta is per-message framing overhead, which rough omits.
    assert billed <= rough + 64, (
        f"{label}: billed {billed:,} tokens but the correct estimate is "
        f"{rough:,} (media priced at {priced:,}); the base64 payload is being "
        f"counted a second time as characters"
    )
    # And the absolute bound: nowhere near the raw base64 char count.
    assert billed < priced * 4, (
        f"{label}: billed {billed:,} tokens for media priced at {priced:,}"
    )


def test_media_stripping_is_insensitive_to_payload_size():
    """Growing a document payload 10x must not change the token bill.

    Payload size carries no pricing signal for media (documents bill per page),
    so a larger base64 blob with the same page count must bill identically.
    """
    small = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(_synthetic_pdf(3, 40_000)).decode(),
        },
    }
    large = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(_synthetic_pdf(3, 400_000)).decode(),
        },
    }
    assert len(large["source"]["data"]) > len(small["source"]["data"]) * 5

    a = compose_request_breakdown([_media_message(small)])["total_tokens"]
    b = compose_request_breakdown([_media_message(large)])["total_tokens"]
    assert a == b, (
        f"token bill moved with payload size ({a:,} -> {b:,}); media base64 "
        f"is leaking into the character walk"
    )


# --------------------------------------------------------------------------- #
# DRIFT GUARD
#
# The bug above was two sibling functions disagreeing about which part types
# count as media. `_estimate_message_chars` and `_estimate_message_dense_sparse`
# must strip the SAME set, and that set must be exactly `_count_image_tokens`'s
# tallied set (`MEDIA_PART_TYPES`). This is behavioral: it exercises both
# functions against every member of the shared constant.
# --------------------------------------------------------------------------- #
def _payload_for(part_type: str, blob: str) -> dict:
    """A content part of `part_type` carrying `blob` wherever that type puts it."""
    from agent.media_tokens import AUDIO_PART_TYPES, DOCUMENT_PART_TYPES

    if part_type in AUDIO_PART_TYPES:
        return {"type": part_type, "input_audio": {"data": blob, "format": "wav"}}
    if part_type in DOCUMENT_PART_TYPES:
        return {
            "type": part_type,
            "source": {"type": "base64", "media_type": "application/pdf", "data": blob},
        }
    return {"type": part_type, "image_url": {"url": f"data:image/png;base64,{blob}"}}


@pytest.mark.parametrize("part_type", sorted(MEDIA_PART_TYPES))
def test_both_char_walks_strip_every_media_part_type(part_type):
    """Drift guard: both estimators must strip EVERY member of MEDIA_PART_TYPES.

    If either function's stripped set falls behind the shared constant, its
    char count scales with the base64 payload and the part gets billed twice.
    """
    from agent.model_metadata import (
        _estimate_message_chars,
        _estimate_message_dense_sparse,
    )

    small = _media_message(_payload_for(part_type, "Q" * 1_000))
    large = _media_message(_payload_for(part_type, "Q" * 500_000))

    chars_small = _estimate_message_chars(small)
    chars_large = _estimate_message_chars(large)
    assert chars_small == chars_large, (
        f"_estimate_message_chars does not strip {part_type!r}: char count grew "
        f"{chars_small:,} -> {chars_large:,} with the payload, so its base64 is "
        f"billed on top of the flat media price"
    )

    ds_small = _estimate_message_dense_sparse(small)
    ds_large = _estimate_message_dense_sparse(large)
    assert ds_small == ds_large, (
        f"_estimate_message_dense_sparse does not strip {part_type!r}: "
        f"{ds_small} -> {ds_large}"
    )


def test_char_walks_agree_on_which_types_are_media():
    """The two sibling walks must classify every part type identically.

    Sweeps media AND non-media types: a part is 'stripped' iff its char count
    is insensitive to payload size. Both functions must make the same call on
    every type, so neither can drift ahead of (or behind) the other.
    """
    from agent.media_tokens import MEDIA_PART_TYPES
    from agent.model_metadata import (
        _estimate_message_chars,
        _estimate_message_dense_sparse,
    )

    candidates = sorted(MEDIA_PART_TYPES) + ["text", "thinking", "tool_result"]
    chars_strips: dict[str, bool] = {}
    dense_strips: dict[str, bool] = {}

    for part_type in candidates:
        small = _media_message(_payload_for(part_type, "Q" * 1_000))
        large = _media_message(_payload_for(part_type, "Q" * 200_000))
        chars_strips[part_type] = (
            _estimate_message_chars(small) == _estimate_message_chars(large)
        )
        dense_strips[part_type] = (
            _estimate_message_dense_sparse(small)
            == _estimate_message_dense_sparse(large)
        )

    assert chars_strips == dense_strips, (
        "the two char walks disagree about which part types are media: "
        f"_estimate_message_chars={chars_strips} vs "
        f"_estimate_message_dense_sparse={dense_strips}"
    )
    # And the agreed-upon set is exactly the shared media vocabulary.
    assert {t for t, stripped in chars_strips.items() if stripped} == set(
        MEDIA_PART_TYPES
    )


def test_empty_inputs_are_all_zero():
    comp = compose_request_breakdown([], system_prompt="", tools=None)
    assert comp["total_tokens"] == 0
    assert comp["fixed_tokens"] == 0
    assert comp["nonfixed_tokens"] == 0
    assert comp["tool_result_count"] == 0
    assert comp["history_message_count"] == 0


def test_non_dict_messages_do_not_crash():
    comp = compose_request_breakdown(["raw string", 123, None])  # type: ignore[list-item]
    assert comp["total_tokens"] >= 0


def test_chars_per_token_divisor_default_is_3_5():
    # Default divisor is 3.5, not the old 4. Use a tool result (counted close to
    # raw content length) and assert the bucket is ~content/3.5, allowing a small
    # margin for the dict-framing overhead _estimate_message_chars includes.
    n = 70000
    comp = compose_request_breakdown(
        [{"role": "tool", "content": "x" * n, "tool_call_id": "1"}]
    )
    tok = comp["tool_result_tokens"]
    assert tok >= n / 3.5, f"{tok} should be >= {n}/3.5"
    # Strictly larger than the old /4 rule would have produced.
    assert tok > (n + 3) // 4


def test_chars_per_token_divisor_env_override(monkeypatch):
    # The divisor is read at import; reload the module under a patched env so
    # the override path is exercised end-to-end. With 4.0 the same payload
    # yields fewer tokens than the default 3.5.
    import importlib
    import agent.model_metadata as mm
    n = 80000
    monkeypatch.setenv("HERMES_COMPOSITION_CHARS_PER_TOKEN", "4")
    importlib.reload(mm)
    try:
        assert mm.COMPOSITION_CHARS_PER_TOKEN == 4.0
        comp4 = mm.compose_request_breakdown(
            [{"role": "tool", "content": "x" * n, "tool_call_id": "1"}]
        )
        tok4 = comp4["tool_result_tokens"]
        assert tok4 >= n / 4.0
        # 4.0 must produce strictly fewer tokens than the 3.5 default.
        monkeypatch.setenv("HERMES_COMPOSITION_CHARS_PER_TOKEN", "3.5")
        importlib.reload(mm)
        comp35 = mm.compose_request_breakdown(
            [{"role": "tool", "content": "x" * n, "tool_call_id": "1"}]
        )
        assert comp35["tool_result_tokens"] > tok4
    finally:
        monkeypatch.delenv("HERMES_COMPOSITION_CHARS_PER_TOKEN", raising=False)
        importlib.reload(mm)  # restore default 3.5 for other tests


def test_chars_per_token_divisor_out_of_range_falls_back_to_default(monkeypatch):
    import importlib
    import agent.model_metadata as mm
    # Absurd / malformed values (typo guard) must be ignored -> default 3.5.
    for bad in ("0", "999", "-3", "notanumber", ""):
        monkeypatch.setenv("HERMES_COMPOSITION_CHARS_PER_TOKEN", bad)
        importlib.reload(mm)
        assert mm.COMPOSITION_CHARS_PER_TOKEN == 3.5, f"value {bad!r} should fall back"
    monkeypatch.delenv("HERMES_COMPOSITION_CHARS_PER_TOKEN", raising=False)
    importlib.reload(mm)


# --------------------------------------------------------------------------- #
# two-tier divisor: FIXED buckets /FIXED (~4.2), NON-FIXED buckets /3.5
# --------------------------------------------------------------------------- #
def test_fixed_buckets_use_fixed_divisor_nonfixed_use_3_5():
    # sys_tokens is from a RAW string (no message wrapping) so it's exact:
    # ceil(chars / FIXED). tool_result goes through _estimate_message_chars
    # (small role/structure overhead), so assert it tracks /3.5, not /FIXED.
    import math
    import agent.model_metadata as mm
    F = mm.COMPOSITION_CHARS_PER_TOKEN_FIXED
    comp = compose_request_breakdown(
        [{"role": "tool", "content": "r" * 3500, "tool_call_id": "1"}],
        system_prompt="s" * 4000,
    )
    assert comp["sys_tokens"] == math.ceil(4000 / F), "fixed sys bucket must use FIXED divisor"
    # tool_result must be the /3.5 regime, strictly more than /FIXED would give.
    assert math.ceil(3500 / 3.5) <= comp["tool_result_tokens"] <= math.ceil(3600 / 3.5)
    assert comp["tool_result_tokens"] > math.ceil(3600 / F), "must NOT use FIXED divisor"
    assert comp["total_tokens"] == comp["fixed_tokens"] + comp["nonfixed_tokens"]


def test_tool_schema_bucket_uses_fixed_divisor():
    # tool_schema_chars = len(str(tools)); pick tools whose repr is a known length.
    import math
    import agent.model_metadata as mm
    F = mm.COMPOSITION_CHARS_PER_TOKEN_FIXED
    tools = [{"x": "a" * 3996}]  # str(tools) length is deterministic & > 4000
    comp = compose_request_breakdown([], tools=tools)
    chars = len(str(tools))
    assert comp["tool_schema_tokens"] == math.ceil(chars / F)
    # And strictly fewer than the old /3.5 would have given.
    assert comp["tool_schema_tokens"] < math.ceil(chars / 3.5)


def test_all_three_nonfixed_buckets_stay_on_3_5():
    # Prove the negative: the most likely bug is moving a non-fixed bucket to the
    # FIXED divisor. fixed+nonfixed==total does NOT catch mis-bucketing, so assert
    # each /3.5. Message buckets carry small role/structure overhead, so bound
    # around /3.5 and prove they did NOT collapse toward the FIXED divisor.
    import math
    import agent.model_metadata as mm
    F = mm.COMPOSITION_CHARS_PER_TOKEN_FIXED
    msgs = [
        {"role": "assistant", "content": "h" * 7000,
         "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "a" * 7000}}]},
        {"role": "tool", "content": "r" * 7000, "tool_call_id": "1"},
    ]
    comp = compose_request_breakdown(msgs)
    lo, hi = math.ceil(7000 / 3.5), math.ceil(7100 / 3.5)   # 2000 .. ~2029
    fixed_val = math.ceil(7100 / F)                          # ~1690 at 4.2
    for key in ("history_tokens", "tool_arg_tokens", "tool_result_tokens"):
        assert lo <= comp[key] <= hi, f"{key}={comp[key]} not in /3.5 band [{lo},{hi}]"
        assert comp[key] > fixed_val, f"{key} collapsed toward FIXED divisor"


def test_fixed_and_nonfixed_differ_for_identical_chars():
    # Collapse guard: identical char count must yield DIFFERENT token counts
    # across the two tiers (catches "both divisors accidentally equal").
    import math
    import agent.model_metadata as mm
    F = mm.COMPOSITION_CHARS_PER_TOKEN_FIXED
    comp = compose_request_breakdown(
        [{"role": "tool", "content": "r" * 7000, "tool_call_id": "1"}],
        system_prompt="s" * 7000,
    )
    assert comp["sys_tokens"] == math.ceil(7000 / F)            # exact (raw string)
    assert comp["tool_result_tokens"] >= math.ceil(7000 / 3.5)  # >= 2000 (3.5 regime)
    assert comp["tool_result_tokens"] > comp["sys_tokens"], "tiers must differ"


def test_identity_plus_skills_equals_sys_both_fixed():
    # skills index is a substring of the system prompt; both use the FIXED rule,
    # so identity + skills must sum to sys exactly (invariant preserved).
    skills = "    - foo: a skill\n    - bar: another\n"
    sysp = "IDENTITY RULES " * 50 + skills
    comp = compose_request_breakdown([], system_prompt=sysp, skills_prompt=skills)
    assert comp["identity_tokens"] + comp["skills_tokens"] == comp["sys_tokens"]
    assert comp["skills_count"] == 2


def test_fixed_divisor_env_override(monkeypatch):
    # The advertised rollback knob must be the EXACT name the code comment uses.
    import importlib
    import agent.model_metadata as mm
    n = 8000
    # Override fixed back to 3.5 -> collapses fixed to old behavior.
    monkeypatch.setenv("HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED", "3.5")
    importlib.reload(mm)
    try:
        assert mm.COMPOSITION_CHARS_PER_TOKEN_FIXED == 3.5
        comp = mm.compose_request_breakdown([], system_prompt="s" * n)
        import math
        assert comp["sys_tokens"] == math.ceil(n / 3.5)  # old behavior restored
    finally:
        monkeypatch.delenv("HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED", raising=False)
        importlib.reload(mm)


def test_fixed_divisor_default_and_out_of_range_fallback(monkeypatch):
    import importlib
    import agent.model_metadata as mm
    # Use monkeypatch (not raw os.environ) so a mid-loop assertion failure can't
    # leak HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED into later tests — monkeypatch
    # restores the env and the try/finally restores the module's default 4.5.
    try:
        # Default (no env) is 4.5.
        monkeypatch.delenv("HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED", raising=False)
        importlib.reload(mm)
        assert mm.COMPOSITION_CHARS_PER_TOKEN_FIXED == 4.5, "default fixed divisor must be 4.5"
        # Absurd / malformed values (typo guard) fall back to the 4.5 default.
        for bad in ("0", "999", "-3", "notanumber", ""):
            monkeypatch.setenv("HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED", bad)
            importlib.reload(mm)
            assert mm.COMPOSITION_CHARS_PER_TOKEN_FIXED == 4.5, f"{bad!r} should fall back to 4.5"
    finally:
        monkeypatch.delenv("HERMES_COMPOSITION_CHARS_PER_TOKEN_FIXED", raising=False)
        importlib.reload(mm)  # restore default 4.5 for other tests


# --------------------------------------------------------------------------- #
# blackbox store — comp_* persistence
# --------------------------------------------------------------------------- #
@pytest.fixture
def bb_store(tmp_path, monkeypatch):
    from plugins.blackbox import store as st

    db = tmp_path / "turns.db"
    monkeypatch.setattr(st, "_db_path", lambda: db)
    return st


def _record(**over):
    from plugins.blackbox.record import TurnRecord

    base: dict = dict(
        turn_id="turn_test1", ts_start=1.0, ts_end=2.0, platform="discord",
        chat_id="c1", chat_name="n", api_calls=3,
        input_tokens=100, output_tokens=10, cache_read_tokens=50,
        cache_write_tokens=0, context_used=5000, context_length=200000,
        comp_sys_tokens=1200, comp_tool_schema_tokens=1800,
        comp_history_tokens=300, comp_tool_result_tokens=1400,
        comp_history_message_count=12,
        comp_tool_arg_tokens=200, comp_tool_result_count=4,
        comp_skills_tokens=600, comp_framing_tokens=720,
        comp_skills_count=58,
        comp_calls_json=json.dumps([
            {
                "composition": {"fixed_tokens": 3000},
                "output_tokens": 7,
                "reasoning_tokens": 1,
            }
        ]),
    )
    base.update(over)
    return TurnRecord(**base)  # type: ignore[arg-type]


def test_comp_columns_round_trip(bb_store):
    bb_store.insert_turn(_record())
    got = bb_store.get_turn("turn_test1")
    assert got is not None
    assert got["comp_sys_tokens"] == 1200
    assert got["comp_tool_schema_tokens"] == 1800
    assert got["comp_history_tokens"] == 300
    assert got["comp_history_message_count"] == 12
    assert got["comp_tool_result_tokens"] == 1400
    assert got["comp_tool_arg_tokens"] == 200
    assert got["comp_tool_result_count"] == 4
    assert got["comp_skills_tokens"] == 600
    assert got["comp_skills_count"] == 58
    assert got["comp_framing_tokens"] == 720
    assert json.loads(got["comp_calls_json"]) == [
        {
            "composition": {"fixed_tokens": 3000},
            "output_tokens": 7,
            "reasoning_tokens": 1,
        }
    ]


def test_comp_columns_null_for_absent(bb_store):
    bb_store.insert_turn(_record(
        turn_id="turn_null", comp_sys_tokens=None, comp_tool_schema_tokens=None,
        comp_history_tokens=None, comp_tool_result_tokens=None,
        comp_tool_arg_tokens=None, comp_tool_result_count=None, comp_calls_json=None,
    ))
    got = bb_store.get_turn("turn_null")
    assert got["comp_sys_tokens"] is None
    assert got["comp_calls_json"] is None


def test_migration_adds_comp_columns_to_legacy_db(bb_store, tmp_path):
    # Simulate a pre-composition DB: create the turns table WITHOUT comp_* cols,
    # then let _ensure_schema migrate it.
    db = bb_store._db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    # Legacy schema: real columns the indexes reference, but NO comp_* columns.
    conn.execute(
        "CREATE TABLE turns ("
        "turn_id TEXT PRIMARY KEY, platform TEXT, chat_id TEXT, ts_end REAL, "
        "cost_usd REAL, context_used INT, last_uncached INT)"
    )
    conn.commit()
    conn.close()
    # _connect → _ensure_schema runs the guarded ALTERs.
    with bb_store._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
    for c in ("comp_sys_tokens", "comp_tool_schema_tokens", "comp_history_tokens",
              "comp_history_message_count",
              "comp_tool_result_tokens", "comp_tool_arg_tokens",
              "comp_tool_result_count", "comp_skills_tokens",
              "comp_skills_count",
              "comp_framing_tokens", "comp_calls_json"):
        assert c in cols, f"migration missed {c}"


def test_migration_idempotent(bb_store):
    # Connecting twice must not raise "duplicate column".
    with bb_store._connect() as conn:
        cols1 = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
    with bb_store._connect() as conn:
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
    assert cols1 == cols2
