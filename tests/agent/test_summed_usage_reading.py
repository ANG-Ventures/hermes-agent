"""A relay-summed usage reading must not become the context size (t_dd918516).

Live incident 2026-10-26 03:41 (aegis, session 20261019_100140_d44d5dbb, claude-dtlr/claude-opus-5-5,
1M window, compaction threshold 650,000):

    API call #88  in=619053
    API call #89  in=unknown           (usage-less tool-call reply)
    API call #90  in=1241066           (2.0x: the bpx tui relay summed both requests' prompts)

The compressor stamped 1,241,066 as ``last_prompt_tokens`` and the usage anchor captured it, so the
next preflight read ``basis=anchored request=~1,242,888`` (= 1,241,066 + 1,616 completion + delta)
and fired compaction at a real ~622k (the next real reading), under the 650k threshold. The
gateway footer, hygiene valve ("stored last_prompt_tokens ~1,241,066") and delegate headroom all
read the same compressor figure. Across 2026-10-12..27 the first measured claude-dtlr response
after a usage-less one read 2.0x the previous one 24 times (of 383).

The anchor arithmetic itself is correct; the defect is adopting a figure that cannot be one
request's prompt. These tests pin: the bound, the adopt/skip decision in record_response_usage,
and the downstream compaction basis.
"""

from types import SimpleNamespace

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from agent.usage_anchor import (
    anchored_context_tokens,
    capture_usage_anchor,
    implausible_prompt_reading,
    set_usage_anchor,
)

CTX = 1_000_000
THRESHOLD = 650_000


def _history():
    msgs = [{"role": "user", "content": "start"}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": f"step {i}", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok " * 200})
    return msgs


def _append_round(msgs, n):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"x{n}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": f"x{n}", "content": "out " * 300})


class TestBound:
    def test_incident_reading_is_implausible(self):
        msgs = _history()
        anchor = capture_usage_anchor(619_053, 287, msgs)
        _append_round(msgs, 1)  # the usage-less call #89 and its tool result
        _append_round(msgs, 2)
        reason = implausible_prompt_reading(1_241_066, msgs, anchor, context_length=CTX)
        assert reason and reason.startswith("exceeds_window")

    def test_doubling_inside_the_window_is_implausible(self):
        # 2026-10-26 04:10: 246,251 -> 497,829 after two usage-less replies; under the window.
        msgs = _history()
        anchor = capture_usage_anchor(246_251, 300, msgs)
        _append_round(msgs, 1)
        reason = implausible_prompt_reading(497_829, msgs, anchor, context_length=CTX)
        assert reason and reason.startswith("anchor_multiple")

    def test_real_growth_is_plausible(self):
        msgs = _history()
        anchor = capture_usage_anchor(619_053, 287, msgs)
        _append_round(msgs, 1)
        # Next real reading of the incident: +3k; and a large tool result read 1.55x LOW by rough.
        assert implausible_prompt_reading(622_216, msgs, anchor, context_length=CTX) is None
        msgs.append({"role": "user", "content": "x " * 40_000})
        expected = anchored_context_tokens(msgs, anchor)
        grown = 619_053 + int((expected - 619_053) * 1.6)
        assert implausible_prompt_reading(grown, msgs, anchor, context_length=CTX) is None

    def test_no_anchor_only_window_bound(self):
        msgs = _history()
        assert implausible_prompt_reading(900_000, msgs, None, context_length=CTX) is None
        assert implausible_prompt_reading(1_000_001, msgs, None, context_length=CTX)
        assert implausible_prompt_reading(5_000_000, msgs, None, context_length=0) is None


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    a = AIAgent(api_key="k", base_url="https://inference-api.nousresearch.com/v1", provider="nous",
                api_mode="chat_completions", model="anthropic/claude-fable-5.1", session_id="t",
                platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
                save_trajectories=False, enabled_toolsets=["file"])
    comp = a.context_compressor
    comp.context_length = CTX
    comp.threshold_tokens = THRESHOLD
    return a


def _resp(prompt, completion):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            total_tokens=prompt + completion,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=prompt - 1000,
                                                                  cache_write_tokens=500),
                            completion_tokens_details=None)
    return SimpleNamespace(usage=usage, id="chatcmpl-x", model="anthropic/claude-fable-5.1")


def _no_usage():
    return SimpleNamespace(usage=None, id="chatcmpl-nousage", model="anthropic/claude-fable-5.1")


def _record(agent, resp, msgs, n):
    from agent import turn_usage

    turn_usage.record_response_usage(agent, resp, messages=msgs, api_call_count=n, api_duration=1.0,
                                     compression_attempts=0, max_compression_attempts=3)


class TestRecordResponseUsage:
    def test_summed_reading_does_not_become_context(self, tmp_path, monkeypatch, caplog):
        a = _agent(tmp_path, monkeypatch)
        try:
            msgs = _history()
            _record(a, _resp(619_053, 287), msgs, 1)
            assert a.context_compressor.last_prompt_tokens == 619_053
            good_anchor = a._usage_anchor
            msgs.append({"role": "assistant", "content": "reply 1"})
            _append_round(msgs, 1)
            # API call #89 in=unknown: the reply that carried no usage; the relay folds its prompt
            # into the next reading.
            _record(a, _no_usage(), msgs, 2)
            _append_round(msgs, 2)
            caplog.set_level("WARNING", logger="agent.conversation_loop")
            _record(a, _resp(1_241_066, 1_616), msgs, 3)

            comp = a.context_compressor
            assert comp.last_prompt_tokens == 619_053, "summed figure stamped as occupancy"
            assert a._usage_anchor is good_anchor, "anchor re-captured from the summed figure"
            # Delegate headroom never takes the summed figure (the usage-less reply in between sets it
            # the way fork/main already does for any usage-less reply; not this fix's concern).
            assert a._last_prompt_size_tokens != 1_241_066
            # Same compressor fields feed the gateway footer/meter, the SessionEntry the hygiene
            # valve reads ("stored last_prompt_tokens ~1,241,066"), and its "actual (live)" figure.
            from gateway.runtime_footer import live_context_tokens

            assert comp.last_real_prompt_tokens == 619_053
            assert live_context_tokens(comp).tokens == 619_053
            # The compaction basis the incident logged as ~1,242,888 stays under the threshold.
            basis = anchored_context_tokens(msgs, a._usage_anchor)
            assert basis is not None and basis < THRESHOLD
            # Billing still counts what the provider billed.
            assert a.session_prompt_tokens == 619_053 + 1_241_066
            assert "not one request's context size" in caplog.text
        finally:
            a.close()

    def test_plausible_reading_is_adopted(self, tmp_path, monkeypatch):
        a = _agent(tmp_path, monkeypatch)
        try:
            msgs = _history()
            _record(a, _resp(619_053, 287), msgs, 1)
            msgs.append({"role": "assistant", "content": "reply 1"})
            _append_round(msgs, 1)
            _record(a, _resp(622_216, 812), msgs, 2)
            assert a.context_compressor.last_prompt_tokens == 622_216
            assert a._usage_anchor["prompt_tokens"] == 622_216
        finally:
            a.close()

    def test_big_jump_without_a_usage_gap_is_adopted(self, tmp_path, monkeypatch):
        """No usage-less reply before it, so the relay cannot have summed: a large real jump (a huge
        tool result, or a caller forcing compaction as test_in_place_preflight_rewind does) must
        still become the context size and drive compaction."""
        a = _agent(tmp_path, monkeypatch)
        try:
            msgs = _history()
            _record(a, _resp(100_000, 287), msgs, 1)
            msgs.append({"role": "assistant", "content": "reply 1"})
            _append_round(msgs, 1)
            _record(a, _resp(600_000, 812), msgs, 2)
            assert a.context_compressor.last_prompt_tokens == 600_000
            assert a._usage_anchor["prompt_tokens"] == 600_000
        finally:
            a.close()

    def test_gap_closes_after_one_measured_reading(self, tmp_path, monkeypatch):
        """The gap is consumed by the next measured reading (summed or not): the reading after it is
        single again and a later real jump is adopted."""
        a = _agent(tmp_path, monkeypatch)
        try:
            msgs = _history()
            _record(a, _resp(100_000, 287), msgs, 1)
            _record(a, _no_usage(), msgs, 2)
            _record(a, _resp(201_000, 300), msgs, 3)      # summed: ignored for context
            assert a.context_compressor.last_prompt_tokens == 100_000
            _record(a, _resp(400_000, 300), msgs, 4)      # no gap now: adopted
            assert a.context_compressor.last_prompt_tokens == 400_000
        finally:
            a.close()

    def test_first_reading_over_window_is_rejected(self, tmp_path, monkeypatch):
        a = _agent(tmp_path, monkeypatch)
        try:
            msgs = _history()
            _record(a, _resp(1_241_066, 1_616), msgs, 1)
            assert a.context_compressor.last_prompt_tokens in (0, None)
            assert a._usage_anchor is None
        finally:
            a.close()
