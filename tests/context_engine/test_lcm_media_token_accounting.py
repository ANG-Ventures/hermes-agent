"""Media parts must be billed by provider pricing, never by base64 length.

2026-08-07: a 1710x1518 screenshot (1.5 MB base64) was counted at 502,182
tokens against a real Anthropic cost of ~3,461 -- a 145x overestimate that
drove a spurious below-threshold compaction. The payload's character length
carries no pricing signal: providers bill by dimension, page, or duration.
"""
import base64
import math
import struct

import pytest

from agent.model_metadata import (
    compose_request_breakdown,
    estimate_messages_tokens_rough as HOST,
)

lcm_tokens = pytest.importorskip("plugins.context_engine.lcm.tokens")

BLOB = base64.b64encode(b"\x00" * 1_100_000).decode()
DATA_URL = "data:image/png;base64," + BLOB
# The bug: char-counting a payload this size yields ~366,000 tokens.
CHAR_COUNT_FLOOR = 100_000
SANE_CEILING = 10_000


@pytest.fixture(autouse=True)
def _no_injected_counter():
    """Exercise LCM's OWN estimate, not a host counter a sibling test left set."""
    prev = lcm_tokens.get_messages_token_counter()
    lcm_tokens.set_messages_token_counter(None)
    yield
    lcm_tokens.set_messages_token_counter(prev)


def _anthropic_pdf_part(page_count: int) -> dict:
    """Small parseable PDF with both /Pages and leaf /Page objects."""
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Count {page_count} >> endobj\n".encode(),
    ]
    for index in range(page_count):
        # Exercise both legal whitespace forms without letting /Pages count as
        # a leaf page.
        marker = b"/Type/Page" if index % 2 else b"/Type /Page"
        objects.append(
            f"{index + 3} 0 obj << ".encode()
            + marker
            + b" /Parent 2 0 R >> endobj\n"
        )
    pdf = b"%PDF-1.7\n" + b"".join(objects) + b"%%EOF\n"
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf).decode(),
        },
    }


def _openai_wav_part(duration_seconds: int) -> dict:
    sample_rate = 1_000
    byte_rate = sample_rate  # mono, 8-bit PCM
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 1, 8)
    junk = b"abc"  # odd-sized chunk exercises RIFF padding
    pcm = b"\x80" * (duration_seconds * byte_rate)
    body = (
        b"WAVE"
        + b"JUNK" + struct.pack("<I", len(junk)) + junk + b"\x00"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )
    wav = b"RIFF" + struct.pack("<I", len(body)) + body
    return {
        "type": "input_audio",
        "input_audio": {
            "format": "wav",
            "data": base64.b64encode(wav).decode(),
        },
    }


def _openai_mp3_part(frame_count: int) -> dict:
    # MPEG-2 Layer III, 8 kbps, 22.05 kHz: 576 samples and 26 bytes/frame.
    header = (
        (0x7FF << 21)
        | (0b10 << 19)
        | (0b01 << 17)
        | (1 << 16)
        | (1 << 12)
    ).to_bytes(4, "big")
    frame = header + (b"\x00" * 22)
    # Empty ID3v2 tag proves the parser skips container metadata before frames.
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + (frame * frame_count)
    return {
        "type": "input_audio",
        "input_audio": {
            "format": "mp3",
            "data": base64.b64encode(mp3).decode(),
        },
    }


MEDIA_CASES = {
    "image_url": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": DATA_URL}}]}],
    "input_image": [{"role": "user", "content": [
        {"type": "input_text", "text": "hi"},
        {"type": "input_image", "image_url": DATA_URL}]}],
    "anthropic_image_block": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png", "data": BLOB}}]}],
    "anthropic_stash": [{"role": "user", "content": "hi",
                         "_anthropic_content_blocks": [
                             {"type": "image", "source": {"type": "base64",
                                                          "media_type": "image/png",
                                                          "data": BLOB}}]}],
    "multimodal_tool_result": [{"role": "tool", "tool_call_id": "t1",
                                "content": {"_multimodal": True, "content": [
                                    {"type": "image_url",
                                     "image_url": {"url": DATA_URL}}]}}],
    "document_pdf": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": BLOB}}]}],
    "audio": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": BLOB, "format": "wav"}}]}],
}


@pytest.mark.parametrize("name", sorted(MEDIA_CASES))
def test_lcm_does_not_bill_media_as_text(name):
    got = lcm_tokens.count_messages_tokens(MEDIA_CASES[name])
    assert got < SANE_CEILING, (
        f"{name}: {got:,} tokens for one media part -- payload is being "
        "counted as characters again"
    )


@pytest.mark.parametrize("name", sorted(MEDIA_CASES))
def test_host_does_not_bill_media_as_text(name):
    got = HOST(MEDIA_CASES[name])
    assert got < SANE_CEILING, f"{name}: host billed {got:,} tokens"


@pytest.mark.parametrize("name", ["document_pdf", "audio"])
def test_request_breakdown_does_not_double_bill_document_or_audio_base64(name):
    got = compose_request_breakdown(MEDIA_CASES[name])["total_tokens"]
    assert got < SANE_CEILING, f"{name}: request breakdown billed {got:,} tokens"


@pytest.mark.parametrize("name", sorted(MEDIA_CASES))
def test_both_counters_agree(name):
    """The whole point: one quantity, not two independent guesses."""
    host, lcm = HOST(MEDIA_CASES[name]), lcm_tokens.count_messages_tokens(MEDIA_CASES[name])
    assert abs(host - lcm) < 500, f"{name}: host={host:,} lcm={lcm:,} diverge"


def test_media_cost_is_independent_of_payload_size():
    """The load-bearing invariant: 10x the bytes must NOT mean 10x the tokens."""
    small = [{"role": "user", "content": [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + "A" * 20_000}}]}]
    large = [{"role": "user", "content": [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + "A" * 2_000_000}}]}]
    assert lcm_tokens.count_messages_tokens(small) == lcm_tokens.count_messages_tokens(large)


def test_surrounding_text_is_still_counted():
    """Stripping media must not swallow the prose next to it."""
    prose = "word " * 500
    with_text = [{"role": "user", "content": [
        {"type": "text", "text": prose},
        {"type": "image_url", "image_url": {"url": DATA_URL}}]}]
    without = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": DATA_URL}}]}]
    assert lcm_tokens.count_messages_tokens(with_text) > \
        lcm_tokens.count_messages_tokens(without) + 100


def test_media_is_not_double_counted():
    """Flat cost AND char cost both applying would silently re-inflate."""
    one = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": DATA_URL}}]}]
    two = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": DATA_URL}},
        {"type": "image_url", "image_url": {"url": DATA_URL}}]}]
    delta = HOST(two) - HOST(one)
    assert 1000 < delta < 2500, f"second image cost {delta:,}, expected ~1500"


def test_plain_text_messages_are_unaffected():
    msgs = [{"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"}]
    assert lcm_tokens.count_messages_tokens(msgs) < 100


def test_tool_calls_still_counted():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "search", "arguments": '{"q": "' + "x" * 400 + '"}'}}]}]
    assert lcm_tokens.count_messages_tokens(msgs) > 50


def test_anthropic_pdf_cost_scales_per_page_and_excludes_pages_tree():
    part = _anthropic_pdf_part(4)
    message = {"role": "user", "content": [part]}
    expected_media_tokens = 4 * 3_000

    assert lcm_tokens.media_part_token_cost(part) == expected_media_tokens
    assert expected_media_tokens <= lcm_tokens.count_message_tokens(message) < expected_media_tokens + 20
    assert expected_media_tokens <= HOST([message]) < expected_media_tokens + 100
    assert expected_media_tokens <= compose_request_breakdown([message])["history_tokens"] < expected_media_tokens + 100


def test_unparseable_pdf_keeps_flat_document_fallback():
    part = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(b"%PDF-1.7\nno page objects\n%%EOF").decode(),
        },
    }

    assert lcm_tokens.media_part_token_cost(part) == 3_000


def test_openai_wav_cost_uses_ten_tokens_per_second():
    part = _openai_wav_part(duration_seconds=600)
    message = {"role": "user", "content": [part]}
    expected_media_tokens = 600 * 10

    assert lcm_tokens.media_part_token_cost(part) == expected_media_tokens
    assert expected_media_tokens <= lcm_tokens.count_message_tokens(message) < expected_media_tokens + 20
    assert expected_media_tokens <= HOST([message]) < expected_media_tokens + 100
    assert expected_media_tokens <= compose_request_breakdown([message])["history_tokens"] < expected_media_tokens + 100


def test_openai_mp3_cost_uses_frame_duration_at_ten_tokens_per_second():
    # Larger than the parser's 1 MiB inspection cap: duration must come from
    # sampled frame metadata + container length, not a full payload decode.
    frame_count = 60_000
    part = _openai_mp3_part(frame_count)
    message = {"role": "user", "content": [part]}
    expected_media_tokens = math.ceil(frame_count * 576 / 22_050 * 10)

    assert lcm_tokens.media_part_token_cost(part) == expected_media_tokens
    assert expected_media_tokens <= lcm_tokens.count_message_tokens(message) < expected_media_tokens + 20
    assert expected_media_tokens <= HOST([message]) < expected_media_tokens + 100
    assert expected_media_tokens <= compose_request_breakdown([message])["history_tokens"] < expected_media_tokens + 100


@pytest.mark.parametrize("audio_format", ["wav", "mp3"])
def test_unparseable_audio_keeps_flat_audio_fallback(audio_format):
    part = {
        "type": "input_audio",
        "input_audio": {
            "format": audio_format,
            "data": base64.b64encode(b"not an audio container").decode(),
        },
    }

    assert lcm_tokens.media_part_token_cost(part) == 1_500


# --- the injectable seam ----------------------------------------------------

def test_host_counter_overrides_builtin():
    lcm_tokens.set_messages_token_counter(lambda m: 4242)
    assert lcm_tokens.count_messages_tokens([{"role": "user", "content": "x"}]) == 4242


def test_host_counter_failure_falls_back():
    """A broken host counter must never break compaction."""
    def _boom(_):
        raise RuntimeError("host counter exploded")

    lcm_tokens.set_messages_token_counter(_boom)
    assert lcm_tokens.count_messages_tokens([{"role": "user", "content": "x"}]) > 0


def test_host_counter_bad_return_falls_back():
    lcm_tokens.set_messages_token_counter(lambda m: "not an int")
    assert lcm_tokens.count_messages_tokens([{"role": "user", "content": "x"}]) > 0


def test_setter_rejects_non_callable():
    with pytest.raises(TypeError):
        lcm_tokens.set_messages_token_counter(object())


def test_production_loader_registers_the_host_counter():
    """The seam is worthless if nothing wires it -- pin the wiring.

    Restores the previous counter in a finally: load_context_engine registers
    globally, and leaving the host counter set leaks a different token scale
    into every later test in the session (caught by
    test_lcm_fresh_tail_token_budget going red only when run after this file).
    """
    from plugins.context_engine import load_context_engine

    prev = lcm_tokens.get_messages_token_counter()
    try:
        lcm_tokens.set_messages_token_counter(None)
        engine = load_context_engine("lcm")
        if engine is None:
            pytest.skip("lcm engine not loadable in this environment")
        registered = lcm_tokens.get_messages_token_counter()
        assert registered is not None, "loader did not inject the host token counter"
        # Delegation is SELECTIVE: media lists go to the host estimate, pure
        # text stays on the engine's own tokenizer (which its budget arithmetic
        # is internally consistent with).
        media_msgs = MEDIA_CASES["image_url"]
        assert registered(media_msgs) == HOST(media_msgs)
        text_msgs = [{"role": "user", "content": "hello world"}]
        assert registered(text_msgs) == lcm_tokens.count_messages_tokens_builtin(text_msgs)
    finally:
        lcm_tokens.set_messages_token_counter(prev)


def test_text_only_counting_stays_internally_consistent():
    """count_messages_tokens must equal sum(count_message_tokens) for text.

    LCM's fresh-tail budget walk compares the two directly, so a host counter
    on a different scale silently breaks tail sizing (caught in review when a
    blanket delegation turned test_budget_walk_small_messages red).
    """
    from plugins.context_engine import load_context_engine

    prev = lcm_tokens.get_messages_token_counter()
    try:
        lcm_tokens.set_messages_token_counter(None)
        if load_context_engine("lcm") is None:
            pytest.skip("lcm engine not loadable in this environment")
        msgs = [{"role": "assistant", "content": f"m{i:05d} " + "x" * 40}
                for i in range(400)]
        assert lcm_tokens.count_messages_tokens(msgs) == sum(
            lcm_tokens.count_message_tokens(m) for m in msgs
        )
    finally:
        lcm_tokens.set_messages_token_counter(prev)
