import pytest

from gateway.response_filters import (
    SHORT_NOTE_WORDS,
    internal_short_note_words,
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    is_internal_silence_response,
    strip_internal_silence_marker_lines,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_translated_sentinel_is_silence_in_every_form_the_english_one_is():
    """#110935: a lane that answers the cron instruction in its own language translates the
    sentinel; ``[静默]`` must suppress delivery exactly like ``[SILENT]`` (exact, own-line note,
    reordered lines, bracketless, edge punctuation)."""
    assert is_intentional_silence_response("[静默]")
    assert is_intentional_silence_response("**沉默**")
    assert is_autonomous_silence_response("[静默]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[沉默]")
    assert is_autonomous_silence_response("静默")


def test_prose_mentioning_the_translated_sentinel_is_delivered():
    assert not is_intentional_silence_response("status: 静默 means the lane is quiet")
    assert not is_autonomous_silence_response("the lane said 静默 mid-sentence and kept talking")


def test_autonomous_lane_agrees_with_interactive_lane_on_cjk_punctuation_variants():
    """A Chinese lane emits fullwidth brackets or a trailing ``。``; cron/webhook must suppress
    exactly what the interactive predicate suppresses, or the two lanes drift on the new tokens."""
    for variant in ("【静默】", "静默。", "【沉默】", "沉默。", "**[静默]**", "NO_REPLY."):
        assert is_intentional_silence_response(variant)
        assert is_autonomous_silence_response(variant) == is_intentional_silence_response(variant), variant


# --------------------------------------------------------------------------
# Internal turns: a marker beside a long reply delivers the reply (t_bb4059d2)
# --------------------------------------------------------------------------

# 35 words, the median prose+marker reply measured on the live state.db.
LONG_SUMMARY = (
    "PR 4791 merged and the dispatcher picked up the new placement rule on the next tick. "
    "Two cards that were parked on local_full respawned on ci-box and both finished green "
    "within ten minutes."
)
OK = {"failed": False}


def test_long_summary_with_marker_is_delivered_without_the_marker():
    for reply in (LONG_SUMMARY + "\n\nNO_REPLY", "NO_REPLY\n\n" + LONG_SUMMARY):
        assert not is_intentional_silence_agent_result(OK, reply, internal=True, short_note_words=12)
        assert strip_internal_silence_marker_lines(reply) == LONG_SUMMARY
    # The loose rule the cron/webhook lanes keep still reads it as silence.
    assert is_autonomous_silence_response(LONG_SUMMARY + "\n\nNO_REPLY")


@pytest.mark.parametrize("reply", [
    "Already reported.\nNO_REPLY",
    "NO_REPLY\nduplicate inbound, already handled",
    "NO_REPLY",
    "  no_reply  ",
    "[SILENT]",
    "[SILENT] No changes detected this tick, the watch is still green and nothing moved anywhere.",
])
def test_short_note_bare_marker_and_bracketed_prefix_stay_silent(reply):
    assert is_intentional_silence_agent_result(OK, reply, internal=True, short_note_words=12)


def test_short_note_must_be_one_line_and_at_most_the_word_cap():
    two_lines = "Already reported.\nNothing new.\nNO_REPLY"
    assert not is_internal_silence_response(two_lines, 12)
    assert strip_internal_silence_marker_lines(two_lines) == "Already reported.\nNothing new."
    at_cap = " ".join(["w"] * 12) + "\nNO_REPLY"
    over_cap = " ".join(["w"] * 13) + "\nNO_REPLY"
    assert is_internal_silence_response(at_cap, 12)
    assert not is_internal_silence_response(over_cap, 12)


def test_zero_restores_the_loose_rule():
    assert is_internal_silence_response(LONG_SUMMARY + "\n\nNO_REPLY", 0)


def test_config_key_reads_display_setting_with_default():
    assert internal_short_note_words({}) == SHORT_NOTE_WORDS
    assert internal_short_note_words({"display": {"internal_silence_short_note_words": 0}}) == 0
    assert internal_short_note_words({"display": {"internal_silence_short_note_words": "30"}}) == 30
    assert internal_short_note_words({"display": {"internal_silence_short_note_words": "x"}}) == SHORT_NOTE_WORDS


def test_internal_rule_reads_gateway_config_when_unset(monkeypatch):
    import gateway.run as gateway_run
    cfg = {"display": {"internal_silence_short_note_words": 0}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: cfg)
    assert is_internal_silence_response(LONG_SUMMARY + "\n\nNO_REPLY")
    cfg["display"]["internal_silence_short_note_words"] = 12
    assert not is_internal_silence_response(LONG_SUMMARY + "\n\nNO_REPLY")


def test_cron_keeps_the_loose_rule_for_a_long_report():
    from cron.scheduler import _is_cron_silence_response
    assert _is_cron_silence_response(LONG_SUMMARY + "\n\nNO_REPLY")


def test_cjk_reply_beside_a_marker_counts_each_ideograph_as_a_word():
    """CJK prose has no spaces: a 50-character summary was one "word" and stayed silent."""
    long_zh = "这是一个很长的总结，说明部署已经完成，网关已经重启，漂移告警现在报告的是正确的状态。"
    assert not is_internal_silence_response(long_zh + "\nNO_REPLY", 12)
    assert strip_internal_silence_marker_lines(long_zh + "\nNO_REPLY") == long_zh
    assert is_internal_silence_response("已经报告了。\nNO_REPLY", 12)
    assert is_internal_silence_response("PR 12 已合并。\nNO_REPLY", 12)
