"""Summarizer INPUT carries no request-signing material (t_c2107577).

09-28/29: five LCM summary chunks were refused by Sonnet 5.5 (`[cyber]`) on
transcripts dense with relay tool output: Claude Code billing headers
(``cch=``, ``cc_prompt_id=``, the ``cc_version`` fingerprint) and
``signature=`` values. ``redact_sensitive_text`` passed all of them through.
Both summarizer paths (core ContextCompressor, LCM escalation) now mask the
VALUES and keep the key and the surrounding narrative.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.redact import redact_signing_material

CCH = "4f2a9"
PROMPT_ID = "9c1e77ab02"
FINGERPRINT = "ef3"
SIG = "3f9a0c1d2e4b5a6978"
APX_SIG = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
THINKING_SIG = "EqQBCkYIBxgCKkBxyz123456789abcdef"
BEARER = "sk-" + "ant-oat01-" + "abcdefghijklmnopqrstuvwxyz" + "0123456789"
VALUES = (CCH, PROMPT_ID, f"283.{FINGERPRINT}", SIG, APX_SIG, THINKING_SIG, BEARER)

COMMAND = "curl -sD- http://127.0.0.1:18801/v1/messages"
TOOL_OUTPUT = (
    "HTTP/1.1 200 OK\n"
    f"x-anthropic-billing-header: cc_version=2.1.283.{FINGERPRINT}; "
    f"cc_entrypoint=sdk-cli; cch={CCH}; cc_prompt_id={PROMPT_ID};\n"
    f"x-apx-signature: {APX_SIG}\n"
    f"signature={SIG}\n"
    f"Authorization: Bearer {BEARER}\n"
    '{"type":"thinking","signature":"' + THINKING_SIG + '"}\n'
    # JSON-escaped body: the escape letter must not shield the next value.
    '{\\"system\\":\\"line one\\ncch=' + CCH + ';\\"}\n'
    "relay replied 200; billing header present"
)


def _assert_scrubbed(text: str) -> None:
    leaked = [v for v in VALUES if v in text]
    assert not leaked, f"signing material reached the summarizer: {leaked}"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"cch={CCH};", "cch=***;"),
        (f"cc_version=2.1.283.{FINGERPRINT};", "cc_version=2.1.283.***;"),
        (f"cc_prompt_id={PROMPT_ID}", "cc_prompt_id=***"),
        (f"x-apx-signature: {APX_SIG}", "x-apx-signature: ***"),
        (f"a\\ncch={CCH}", "a\\ncch=***"),
        (f"a\\tsignature={SIG}", "a\\tsignature=***"),
    ],
)
def test_values_masked_keys_kept(raw, expected):
    assert redact_signing_material(raw) == expected


@pytest.mark.parametrize(
    "keep",
    [
        "template cch=${cch}; regex cch=([0-9a-f]{5}); cch=<5hex>",
        "sig = inspect.signature(fn); signature = compute_sig2(body)",
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "commit 636aed70fd; cc_version=2.1.283; xcch=4f2a9",
        # Prism P1 (#1554 r1): identifiers / algorithm names are not material.
        "signature = protocol_v2; hmac=HmacSHA256; signature: sign_request_v2",
        "signature=RequestSignerV2.compute(body)",
    ],
)
def test_code_and_identifiers_survive(keep):
    """Digests / SHAs are identifiers a summary must carry; code is not material."""
    assert redact_signing_material(keep) == keep


def _transcript():
    msgs = [{"role": "user", "content": "probe the relay billing header"}]
    for i in range(6):
        call_id = f"call_{i}"
        msgs.append({
            "role": "assistant",
            "content": f"checking relay pass {i}",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": COMMAND})},
            }],
        })
        # Distinct per pass so tool-output dedup keeps every body.
        msgs.append({"role": "tool", "tool_call_id": call_id, "content": f"pass {i}\n{TOOL_OUTPUT}"})
    msgs.append({"role": "user", "content": "ok, what next"})
    msgs.append({"role": "assistant", "content": "next: compare against the CLI"})
    return msgs


def test_core_compressor_request_body_has_no_signing_material():
    from agent.context_compressor import ContextCompressor, _SUMMARY_REFUSALS

    _SUMMARY_REFUSALS._refused.clear()
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(
            model="main-model", provider="claude-apr", protect_first_n=1,
            protect_last_n=2, abort_on_summary_failure=False, quiet_mode=True,
        )
    captured: list[dict] = []
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "Probed the relay with terminal curl."}}]}

    def capture(**kw):
        captured.append(kw)
        return ok

    with patch("agent.context_compressor.call_llm", side_effect=capture):
        c.compress(_transcript(), current_tokens=999999, force=True)

    assert captured, "summarizer was never called"
    body = json.dumps(captured[0]["messages"])
    _assert_scrubbed(body)
    # Narrative kept: the tool, the command, the header names.
    assert "terminal" in body and "curl -sD-" in body
    assert "x-anthropic-billing-header" in body and "cch=***" in body


def test_lcm_prompt_and_l3_have_no_signing_material(monkeypatch):
    from plugins.context_engine.lcm import escalation

    escalation._SUMMARY_REFUSALS.clear()
    text = f"[ASSISTANT]: checking relay\n[Tool calls:\n  terminal({COMMAND})\n]\n[TOOL RESULT call_0]: {TOOL_OUTPUT}"
    prompts: list[str] = []

    def fail(**kw):
        prompts.append(kw["messages"][0]["content"])
        raise RuntimeError("HTTP 503 upstream busy")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fail)
    summary, level = escalation.summarize_with_escalation(
        text=text, source_tokens=5000, token_budget=600, l3_truncate_tokens=400,
    )
    assert prompts, "summarizer was never called"
    for prompt in prompts:
        _assert_scrubbed(prompt)
        assert "terminal(" in prompt and "curl -sD-" in prompt
    # L3 deterministic truncation persists as the summary: scrubbed too.
    assert level == 3
    _assert_scrubbed(summary)


def test_lcm_focus_and_custom_instructions_are_redacted(monkeypatch):
    """Prism P1 (#1554 r1): the prompts interpolate focus_topic and
    custom_instructions too; neither may carry secrets or signing values."""
    from plugins.context_engine.lcm import escalation

    escalation._SUMMARY_REFUSALS.clear()
    prompts: list[str] = []

    def ok(**kw):
        prompts.append(json.dumps(kw["messages"]))
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="short"))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", ok)
    escalation.summarize_with_escalation(
        text="probe the relay " * 200, source_tokens=5000, token_budget=600,
        focus_topic=f"retry with Authorization: Bearer {BEARER} and cch={CCH}",
        custom_instructions=f"keep signature={SIG} out",
    )
    assert prompts
    for prompt in prompts:
        _assert_scrubbed(prompt)
        assert "Authorization" in prompt and "cch=***" in prompt


def test_lcm_refusal_shaped_200_walks_the_chain(monkeypatch):
    """Existing behaviour, pinned: a 200 with finish_reason=content_filter from
    the primary is a refusal, not a summary; the next route takes the segment
    and the primary is never re-sent that segment."""
    from plugins.context_engine.lcm import escalation

    escalation._SUMMARY_REFUSALS.clear()
    seen: list[str] = []

    def route(**kw):
        model = kw.get("model") or ""
        seen.append(model)
        if model == "claude-sonnet-5-5":
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="content_filter", message=SimpleNamespace(content=""))])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="relay probe summary"))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", route)
    kw = dict(text=TOOL_OUTPUT * 20, source_tokens=5000, token_budget=600,
              model="claude-sonnet-5-5", fallback_models=["gemini-bridge"])
    assert escalation.summarize_with_escalation(**kw) == ("relay probe summary", 1)
    assert seen == ["claude-sonnet-5-5", "gemini-bridge"]
    seen.clear()
    escalation.summarize_with_escalation(**kw)
    assert seen == ["gemini-bridge"]
    escalation._SUMMARY_REFUSALS.clear()
