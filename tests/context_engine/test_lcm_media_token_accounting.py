"""Media parts must be billed by provider pricing, never by base64 length.

2026-08-07: a 1710x1518 screenshot (1.5 MB base64) was counted at 502,182
tokens against a real Anthropic cost of ~3,461 -- a 145x overestimate that
drove a spurious below-threshold compaction. The payload's character length
carries no pricing signal: providers bill by dimension, page, or duration.
"""
import base64

import pytest

from agent.model_metadata import estimate_messages_tokens_rough as HOST

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
