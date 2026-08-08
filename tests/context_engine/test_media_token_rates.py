"""Media parts are priced by PROVIDER UNITS (page/second), not a flat constant.

Follow-up to #497, which stopped media being billed as base64 TEXT but left
documents and audio on flat placeholders. A flat 3000 under-counted a real
40-page PDF by 30x (measured), and under-counting is the dangerous direction:
compaction fires too LATE and the turn can hit a real provider 400.

Rates are vendor-grounded: Anthropic's PDF docs state each page uses
1,500-3,000 tokens; audio is billed per second.
"""
import base64
import subprocess

import pytest

from agent.media_tokens import (
    AUDIO_TOKENS_PER_SECOND,
    DOCUMENT_TOKENS_PER_PAGE,
    DOCUMENT_TOKEN_COST,
    AUDIO_TOKEN_COST,
    audio_duration_seconds,
    media_part_token_cost,
    pdf_page_count,
)
from agent.model_metadata import estimate_messages_tokens_rough as HOST

lcm_tokens = pytest.importorskip("plugins.context_engine.lcm.tokens")


@pytest.fixture(autouse=True)
def _no_injected_counter():
    prev = lcm_tokens.get_messages_token_counter()
    lcm_tokens.set_messages_token_counter(None)
    yield
    lcm_tokens.set_messages_token_counter(prev)


def _make_pdf(pages: int) -> bytes:
    """A REAL PDF with a known page count, via macOS cupsfilter."""
    text = "\n".join(f"Page {i}" + "\n" * 60 for i in range(1, pages + 1))
    with open("/tmp/_pytest_pdf_src.txt", "w") as fh:
        fh.write(text)
    try:
        out = subprocess.run(
            ["/usr/sbin/cupsfilter", "-i", "text/plain", "/tmp/_pytest_pdf_src.txt"],
            capture_output=True, timeout=60,
        ).stdout
    except Exception:
        out = b""
    if not out.startswith(b"%PDF"):
        pytest.skip("cupsfilter unavailable; cannot build a real PDF fixture")
    return out


def _doc_part(data: bytes):
    return {"type": "document", "source": {"type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(data).decode()}}


def _audio_part(data: bytes):
    return {"type": "input_audio",
            "input_audio": {"data": base64.b64encode(data).decode(), "format": "wav"}}


def _wav(seconds: float, rate: int = 8000) -> bytes:
    """A minimal but REAL WAV of a known duration."""
    import struct
    nframes = int(seconds * rate)
    data = b"\x00\x00" * nframes                      # 16-bit mono silence
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


# --- page counting ----------------------------------------------------------

@pytest.mark.parametrize("pages", [1, 12, 40])
def test_pdf_page_count_is_exact_on_real_pdfs(pages):
    assert pdf_page_count(_make_pdf(pages)) == pages


@pytest.mark.parametrize("pages", [1, 12, 40])
def test_document_cost_scales_with_pages(pages):
    assert media_part_token_cost(_doc_part(_make_pdf(pages))) == pages * DOCUMENT_TOKENS_PER_PAGE


def test_multipage_pdf_is_no_longer_undercounted():
    """THE regression: a 40-page PDF used to bill a flat 3000 (30x under)."""
    cost = media_part_token_cost(_doc_part(_make_pdf(40)))
    assert cost > DOCUMENT_TOKEN_COST * 10, f"40-page PDF billed only {cost}"


# --- audio duration ---------------------------------------------------------

@pytest.mark.parametrize("seconds", [1.0, 10.0, 600.0])
def test_audio_duration_parsed_from_real_wav(seconds):
    got = audio_duration_seconds(_wav(seconds))
    assert got is not None and abs(got - seconds) < 0.05


@pytest.mark.parametrize("seconds", [1.0, 10.0, 600.0])
def test_audio_cost_scales_with_duration(seconds):
    cost = media_part_token_cost(_audio_part(_wav(seconds)))
    assert abs(cost - seconds * AUDIO_TOKENS_PER_SECOND) <= 1


def test_ten_minute_clip_is_no_longer_undercounted():
    cost = media_part_token_cost(_audio_part(_wav(600.0)))
    assert cost > AUDIO_TOKEN_COST * 3, f"10-min clip billed only {cost}"


# --- fallbacks: undecidable input must degrade, never explode ---------------

def test_unparseable_document_falls_back_to_flat():
    assert media_part_token_cost(_doc_part(b"definitely not a pdf" * 50)) == DOCUMENT_TOKEN_COST


def test_unparseable_audio_falls_back_to_flat():
    assert media_part_token_cost(_audio_part(b"not audio" * 50)) == AUDIO_TOKEN_COST


def test_externalized_marker_is_not_decoded():
    """A ref-substituted payload must not be mistaken for base64 data."""
    part = {"type": "document",
            "source": {"type": "base64", "data": "[Externalized LCM ingest payload: ...]"}}
    assert media_part_token_cost(part) == DOCUMENT_TOKEN_COST


def test_cost_never_explodes_on_garbage():
    for bad in (None, {}, {"type": "document"}, {"type": "document", "source": {}},
                {"type": "input_audio", "input_audio": {}}):
        assert media_part_token_cost(bad) < 10_000


def test_text_parts_still_zero():
    assert media_part_token_cost({"type": "text", "text": "hello"}) == 0


# --- the two counters must not drift ---------------------------------------

@pytest.mark.parametrize("pages", [1, 40])
def test_host_and_lcm_agree_on_documents(pages):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                         _doc_part(_make_pdf(pages))]}]
    host, lcm = HOST(msgs), lcm_tokens.count_messages_tokens(msgs)
    assert abs(host - lcm) < 500, f"host={host} lcm={lcm}"


@pytest.mark.parametrize("seconds", [1.0, 600.0])
def test_host_and_lcm_agree_on_audio(seconds):
    msgs = [{"role": "user", "content": [_audio_part(_wav(seconds))]}]
    host, lcm = HOST(msgs), lcm_tokens.count_messages_tokens(msgs)
    assert abs(host - lcm) < 500, f"host={host} lcm={lcm}"


def test_rate_constants_match_across_both_implementations():
    """The LCM plugin keeps its own copy so it stays standalone/upstreamable.

    That is a deliberate duplication, which makes silent drift the hazard — pin
    the rates equal so a change to one side fails here instead of in production.
    """
    assert lcm_tokens._DOCUMENT_TOKENS_PER_PAGE == DOCUMENT_TOKENS_PER_PAGE
    assert lcm_tokens._AUDIO_TOKENS_PER_SECOND == AUDIO_TOKENS_PER_SECOND
    assert lcm_tokens._DOCUMENT_PART_TOKENS == DOCUMENT_TOKEN_COST
    assert lcm_tokens._AUDIO_PART_TOKENS == AUDIO_TOKEN_COST


def test_parsers_agree_across_both_implementations():
    pdf = _make_pdf(12)
    assert lcm_tokens.pdf_page_count(pdf) == pdf_page_count(pdf) == 12
    wav = _wav(10.0)
    assert abs(lcm_tokens.audio_duration_seconds(wav) - audio_duration_seconds(wav)) < 0.01


# --- cost of the parse itself ----------------------------------------------

def test_parsing_is_bounded_on_a_large_payload():
    """This runs on every message of every turn — it must not scale with size."""
    import time
    big = _make_pdf(1) + b"\x00" * (8 * 1024 * 1024)
    start = time.monotonic()
    media_part_token_cost(_doc_part(big))
    assert time.monotonic() - start < 5.0
