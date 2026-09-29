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
