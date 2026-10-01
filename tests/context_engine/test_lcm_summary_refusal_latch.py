"""A safeguard refusal of an LCM summary is deterministic for that segment on
that model: the escalation path must never re-send it (t_6c01fd8e).

Measured 09-29 on session b6f8858a: the same ~245k-char "Summarize this
conversation segment" prompt was refused (apiRefusalCategory cyber) and the
only thing between re-sends was the 300 s route circuit-breaker cooldown, so
every compaction pass after the cooldown paid a fresh cold prefill to be
refused again (L1 and L2 both carry the same segment).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from plugins.context_engine.lcm import escalation
from plugins.context_engine.lcm.escalation import (
    SummaryCircuitBreaker,
    summarize_with_escalation,
)


def _bpx_safeguard_400() -> Exception:
    """The error claude-bpx#394 returns for a no-fallback safeguard refusal."""
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "safeguard_refusal",
            "message": "Claude Code returned an error result: API Error: "
            "claude-sonnet-5-5's safeguards flagged this message.",
        },
    }
    request = httpx.Request("POST", "http://127.0.0.1:18801/v1/chat/completions")
    response = httpx.Response(400, json=body, request=request)
    return openai.BadRequestError(
        "Error code: 400 - " + str(body), response=response, body=body
    )


def _legacy_bpx_500() -> Exception:
    """Pre-#394 shape seen in errors.log 09-29 04:47: a 500 with the CC text."""
    body = {
        "error": {
            "message": "Claude Code returned an error result: API Error: "
            "claude-sonnet-5-5's safeguards flagged this message. Our "
            "intentionally broad safeguards allow us ...",
            "type": "internal_error",
        }
    }
    request = httpx.Request("POST", "http://127.0.0.1:18801/v1/chat/completions")
    response = httpx.Response(500, json=body, request=request)
    return openai.InternalServerError(
        "Error code: 500 - " + str(body), response=response, body=body
    )


def _ok(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                 finish_reason="stop")]
    )


@pytest.fixture(autouse=True)
def _fresh_latch():
    escalation._SUMMARY_REFUSALS.clear()
    yield
    escalation._SUMMARY_REFUSALS.clear()


SEGMENT = "user: probe the relay\nassistant: " + ("x " * 3000)
SOURCE_TOKENS = 3000


def _run(**kw):
    return summarize_with_escalation(
        text=kw.pop("text", SEGMENT),
        source_tokens=SOURCE_TOKENS,
        token_budget=600,
        l3_truncate_tokens=64,
        **kw,
    )


@pytest.mark.parametrize("make_error", [_bpx_safeguard_400, _legacy_bpx_500])
def test_refused_segment_is_sent_once_then_never_again_on_that_model(monkeypatch, make_error):
    calls: list[dict] = []

    def refuse(**kw):
        calls.append(kw)
        raise make_error()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", refuse)
    # cooldown 0 == "the 300 s breaker cooldown has elapsed": the old loop.
    breaker = SummaryCircuitBreaker(failure_threshold=2, cooldown_seconds=0)

    for _ in range(3):
        summary, level = _run(model="claude-sonnet-5-5", circuit_breaker=breaker)
        assert level == 3  # deterministic fallback, compaction still converges
        assert summary

    # One refused L1 send total: not L2 on the same segment, not the next pass.
    assert len(calls) == 1


def test_refusal_falls_through_to_fallback_model(monkeypatch):
    seen: list[str] = []

    def route(**kw):
        model = kw.get("model") or ""
        seen.append(model)
        if model == "claude-sonnet-5-5":
            raise _bpx_safeguard_400()
        return _ok("short summary of the segment")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = _run(model="claude-sonnet-5-5", fallback_models=["kimi-k3"])
    assert (summary, level) == ("short summary of the segment", 1)
    # Second pass on the same segment skips the refused model outright.
    seen.clear()
    _run(model="claude-sonnet-5-5", fallback_models=["kimi-k3"])
    assert seen == ["kimi-k3"]


def test_latch_is_scoped_to_the_segment_and_does_not_open_the_route(monkeypatch):
    refuse_text = {"on": True}
    calls: list[str] = []

    def route(**kw):
        prompt = kw["messages"][0]["content"]
        calls.append(prompt)
        if refuse_text["on"] and "probe the relay" in prompt:
            raise _bpx_safeguard_400()
        return _ok("fine")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    breaker = SummaryCircuitBreaker(failure_threshold=1, cooldown_seconds=300)
    _run(model="claude-sonnet-5-5", circuit_breaker=breaker)
    # A refusal is about the content, not the route: other segments still summarize.
    assert breaker.allows("claude-sonnet-5-5")
    summary, level = _run(
        model="claude-sonnet-5-5",
        circuit_breaker=breaker,
        text="user: rename the file\nassistant: " + ("y " * 3000),
    )
    assert (summary, level) == ("fine", 1)


def test_non_refusal_errors_are_not_latched(monkeypatch):
    calls: list[int] = []

    def flaky(**kw):
        calls.append(1)
        raise RuntimeError("Error code: 500 - Prompt is too long")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", flaky)
    _run(model="claude-sonnet-5-5")
    _run(model="claude-sonnet-5-5")
    # L1 + L2 each pass; the ordinary failure path is unchanged.
    assert len(calls) == 4


def test_content_filter_finish_is_a_refusal(monkeypatch):
    calls: list[int] = []

    def filtered(**kw):
        calls.append(1)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""),
                                     finish_reason="content_filter")]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", filtered)
    _run(model="claude-sonnet-5-5")
    _run(model="claude-sonnet-5-5")
    assert len(calls) == 1


# --- t_2ba784cc: the latch must expire and must key on the resolved route ---


def _pin_default_route(monkeypatch, main: dict) -> None:
    """Compression task on ``auto``: call_llm follows the live main runtime."""
    import agent.auxiliary_client as aux

    monkeypatch.setattr(
        aux, "_resolve_task_provider_model",
        lambda task=None, provider=None, model=None, **_: (
            provider or "auto", model, None, None, None),
    )
    monkeypatch.setattr(aux, "_read_main_provider", lambda: main["provider"])
    monkeypatch.setattr(aux, "_read_main_model_for_aux", lambda: main["model"])
    monkeypatch.setattr(aux, "_read_main_base_url", lambda: main.get("base_url", ""))


def test_default_route_latch_follows_a_model_switch(monkeypatch):
    main = {"provider": "claude-bpr", "model": "claude-sonnet-5-5"}
    _pin_default_route(monkeypatch, main)
    served: list[str] = []

    def route(**kw):
        served.append(main["model"])
        if main["model"] == "claude-sonnet-5-5":
            raise _bpx_safeguard_400()
        return _ok("short summary of the segment")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    assert _run()[1] == 3
    assert _run()[1] == 3  # still latched on the same resolved route
    assert served == ["claude-sonnet-5-5"]

    # /model switch: the "<task-default>" alias now resolves elsewhere.
    main.update(provider="openai-codex", model="gpt-5.5")
    assert _run() == ("short summary of the segment", 1)
    assert served == ["claude-sonnet-5-5", "gpt-5.5"]


def test_refusal_latch_expires(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(escalation.time, "monotonic", lambda: clock["now"])
    calls: list[int] = []

    def refuse(**kw):
        calls.append(1)
        raise _bpx_safeguard_400()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", refuse)
    _run(model="claude-sonnet-5-5")
    clock["now"] += 60
    _run(model="claude-sonnet-5-5")
    assert len(calls) == 1  # still inside the latch window

    # A misfiring / since-changed classifier gets another try eventually,
    # instead of forcing L3 truncation for the life of the gateway process.
    clock["now"] += 30 * 24 * 3600
    _run(model="claude-sonnet-5-5")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "field,first,second",
    [
        ("focus_topic", "exploit details", "relay config"),
        ("custom_instructions", "quote payloads verbatim", "omit payloads"),
    ],
)
def test_changed_request_inputs_are_resent(monkeypatch, field, first, second):
    """t_bf18e600: the latch identity is the whole request, not the segment
    alone; correcting focus/custom instructions must reach the model."""
    calls: list[dict] = []

    def refuse(**kw):
        calls.append(kw)
        raise _bpx_safeguard_400()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", refuse)
    breaker = SummaryCircuitBreaker(failure_threshold=99, cooldown_seconds=0)
    _run(model="claude-sonnet-5-5", circuit_breaker=breaker, **{field: first})
    _run(model="claude-sonnet-5-5", circuit_breaker=breaker, **{field: first})
    assert len(calls) == 1  # same request: latched
    _run(model="claude-sonnet-5-5", circuit_breaker=breaker, **{field: second})
    assert len(calls) == 2  # different request: sent


# --- t_f03a8117: an auto route inherits the live endpoint; key on it too ---


def test_default_route_latch_keys_on_the_inherited_endpoint(monkeypatch):
    """Same provider=custom + model name, different runtime base_url: a refusal
    on one endpoint must not suppress (or stay latched after a switch to) the
    other. ``_resolve_task_provider_model`` returns ``base_url=None`` under
    auto; ``call_llm`` then sends to the main runtime endpoint."""
    main = {"provider": "custom", "model": "llama-4",
            "base_url": "http://endpoint-a:8000/v1"}
    _pin_default_route(monkeypatch, main)
    served: list[str] = []

    def route(**kw):
        served.append(main["base_url"])
        if main["base_url"].startswith("http://endpoint-a"):
            raise _bpx_safeguard_400()
        return _ok("short summary of the segment")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    assert _run()[1] == 3
    assert _run()[1] == 3  # latched on endpoint A
    assert served == ["http://endpoint-a:8000/v1"]

    # Only the runtime base_url changes (other session / endpoint switch).
    main["base_url"] = "http://endpoint-b:8000/v1"
    assert _run() == ("short summary of the segment", 1)
    assert served == ["http://endpoint-a:8000/v1", "http://endpoint-b:8000/v1"]


# --- t_cdf67f57: a finish_reason=stop meta-refusal is a refusal, not a summary ---

# Reply shape from LCM node 946 (session 20260924_123749_d91a4b7d) replayed on
# claude-bpr / claude-sonnet-5-5: finish_reason=stop, ~140 tokens, 12,000 budget.
NODE_946_REPLY = (
    "Understood. I won't redo that summary, even in a different wording. I "
    "can't tell which part tripped the classifier, so any rephrasing of the "
    "same material is likely to be interrupted again. If you want, point me at "
    "a narrower slice of the conversation and I'll summarize just that part, "
    "or tell me which topics to leave out and I'll work around them."
)


def test_node_946_meta_refusal_falls_through_to_fallback_and_latches(monkeypatch):
    seen: list[str] = []

    def route(**kw):
        model = kw.get("model") or ""
        seen.append(model)
        if model == "claude-sonnet-5-5":
            return _ok(NODE_946_REPLY)
        return _ok("short summary of the segment")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = _run(model="claude-sonnet-5-5", fallback_models=["flash"])
    assert (summary, level) == ("short summary of the segment", 1)
    assert NODE_946_REPLY not in summary
    # Deterministic like the raised refusal: the next pass skips sonnet outright.
    seen.clear()
    _run(model="claude-sonnet-5-5", fallback_models=["flash"])
    assert seen == ["flash"]


def test_node_946_meta_refusal_with_no_fallback_is_never_persisted(monkeypatch):
    calls: list[int] = []

    def route(**kw):
        calls.append(1)
        return _ok(NODE_946_REPLY)

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = _run(model="claude-sonnet-5-5")
    assert level == 3
    assert "classifier" not in summary
    assert len(calls) == 1  # L2 on the same segment is latched too


@pytest.mark.parametrize(
    "reply",
    [
        NODE_946_REPLY,
        "I'm unable to summarize this segment because it was flagged by the safety filter.",
        "Sorry, I can't continue with that summary; it triggered the safeguards again.",
        "I cannot provide a summary of this content under the usage policy.",
    ],
)
def test_meta_refusal_detector_hits(reply):
    assert escalation._is_meta_refusal_reply(reply, 24000)


@pytest.mark.parametrize(
    "reply",
    [
        "short summary of the segment",
        # Third-person summary of a conversation ABOUT the classifier.
        "The user investigated why the relay tripped the classifier on node 946 "
        "and decided to add a meta-refusal detector.\nExpand for details about: "
        "relay debugging",
        "Assistant renamed config.yaml keys; user approved. Expand for details about: rename",
        # Prism d4968847c2b5: a short summary that QUOTES or reports a refusal.
        'Assistant said "I cannot provide a summary"; the user switched to relay '
        "configuration. Expand for details about: relay configuration",
        "Assistant replied \u201cI won't redo that summary\u201d and the user narrowed "
        "the scope to the relay config.",
        "The assistant said I can't summarize the signing material; user removed it.",
    ],
)
def test_meta_refusal_detector_misses_real_summaries(reply):
    # A short third-person summary about a classifier must not be thrown away.
    assert not escalation._is_meta_refusal_reply(reply, 24000)


def test_long_reply_with_refusal_quote_is_accepted(monkeypatch):
    """A real summary that quotes an in-conversation refusal is long: keep it."""
    body = (
        "- User asked the assistant to redo a summary; assistant said "
        "\"I won't redo that summary\".\n" + ("- kept decision detail\n" * 400)
    )
    assert not escalation._is_meta_refusal_reply(body, 1200)


def test_tiny_output_for_huge_input_without_refusal_phrasing_is_accepted(monkeypatch):
    """Ratio alone never rejects (Apollo 2026-09-30): it would also latch a
    budget-dependent length miss across L1/L2 (Prism eb6459fd5d4e)."""
    seen = []

    def route(**kw):
        seen.append(kw.get("model"))
        return _ok("Decision: keep sonnet primary; flash ruled out.")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = summarize_with_escalation(
        text=SEGMENT, source_tokens=100_000, token_budget=12_000,
        model="claude-sonnet-5-5", fallback_models=["luna"],
    )
    assert (summary, level) == ("Decision: keep sonnet primary; flash ruled out.", 1)
    assert seen == ["claude-sonnet-5-5"]


def test_refusal_phrasing_in_long_reply_is_accepted(monkeypatch):
    """Phrasing alone never rejects either: a long reply is a summary."""
    long_reply = "I can't summarize every tool call, so here are the decisions.\n" + (
        "- kept decision detail\n" * 400)
    assert not escalation._is_meta_refusal_reply(long_reply, 1200)


def test_short_summary_of_short_input_remains_valid(monkeypatch):
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: _ok("Decision: ship it."))
    assert summarize_with_escalation(
        text="User approved shipping after test passed.", source_tokens=20,
        token_budget=200, model="claude-sonnet-5-5",
    ) == ("Decision: ship it.", 1)


def test_refusal_finish_reason_is_rejected(monkeypatch):
    calls = []

    def route(**kw):
        calls.append(kw.get("model"))
        if kw.get("model") == "claude-sonnet-5-5":
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="refused"), finish_reason="refusal")])
        return _ok("decision summary")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    assert _run(model="claude-sonnet-5-5", fallback_models=["luna"]) == (
        "decision summary", 1)
    assert calls == ["claude-sonnet-5-5", "luna"]


def test_huge_prompt_skips_flash_bridge_with_truncated_tail(monkeypatch):
    seen = []

    def route(**kw):
        seen.append(kw.get("model"))
        if kw.get("model") == "sonnet":
            return _ok(NODE_946_REPLY)
        return _ok("A summary from luna")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = summarize_with_escalation(
        text="user: " + ("long history " * 20_000), source_tokens=100_000,
        token_budget=12_000, model="sonnet",
        fallback_models=["gemini-3.8-flash-medium", "gpt-6-luna-900k"],
    )
    assert seen == ["sonnet", "gpt-6-luna-900k"]
    assert (summary, level) == ("A summary from luna", 1)


# --- t_cdf67f57 (Apollo 18:20): all routes refuse => explicit marker + one page per session ---


@pytest.fixture
def pages(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(escalation, "_send_summary_unavailable_page", sent.append)
    monkeypatch.setattr(escalation, "_PAGED_SUMMARY_UNAVAILABLE", set())
    return sent


def _segment(n: int) -> str:
    return f"user: segment {n} " + ("detail " * 400)


def test_refusal_shaped_200_is_never_stored_as_the_summary(monkeypatch, pages):
    seen: list[str] = []

    def route(**kw):
        seen.append(kw.get("model") or "")
        return _ok(NODE_946_REPLY)  # every route answers 200 with the meta-refusal

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = summarize_with_escalation(
        text=_segment(1), source_tokens=1200, token_budget=600,
        model="claude-sonnet-5-5", fallback_models=["gpt-6-luna-900k"],
        session_id="sess-a",
    )
    assert level == 3
    assert summary.startswith(escalation.SUMMARY_UNAVAILABLE_MARKER)
    assert "classifier" not in summary and "I won't" not in summary
    # One hop to the other provider, never a same-route resend (latch contract).
    assert seen == ["claude-sonnet-5-5", "gpt-6-luna-900k"]
    assert len(pages) == 1 and "sess-a" in pages[0]


def test_summary_unavailable_pages_once_per_session(monkeypatch, pages):
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: _ok(NODE_946_REPLY))
    for n in (2, 3):
        summary, _ = summarize_with_escalation(
            text=_segment(n), source_tokens=1200, token_budget=600,
            model="claude-sonnet-5-5", session_id="sess-b",
        )
        assert summary.startswith(escalation.SUMMARY_UNAVAILABLE_MARKER)
    assert len(pages) == 1
    summarize_with_escalation(
        text=_segment(4), source_tokens=1200, token_budget=600,
        model="claude-sonnet-5-5", session_id="sess-c",
    )
    assert len(pages) == 2 and "sess-c" in pages[1]


def test_non_refusal_l3_has_no_marker_and_no_page(monkeypatch, pages):
    def route(**kw):
        raise RuntimeError("502 upstream")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    summary, level = summarize_with_escalation(
        text=_segment(5), source_tokens=1200, token_budget=600,
        model="claude-sonnet-5-5", session_id="sess-d",
    )
    assert level == 3
    assert not summary.startswith(escalation.SUMMARY_UNAVAILABLE_MARKER)
    assert pages == []
