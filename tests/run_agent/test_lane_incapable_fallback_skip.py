"""Capability-gated fallback hops + honest ``lane_incapable`` classification (t_1ed37625).

Wire shape (agent.log 2026-10-05 13:27:47 and 14:16:30, claude-btpr :18811):
    Error code: 400 - {'error': {'type': 'invalid_request_error',
        'code': 'tui_tools_unsupported',
        'message': 'interactive mode on this box serves tool-less turns only; tools[] must be empty (52 tools)'}}
Before the fix the chain alr -> dtlr -> btpr -> bpr paid a banner and an HTTP
round-trip on btpr for every tool-bearing turn, and the banner read
``unclassified error (Anthropic 400) on sub-vps-24``: Anthropic never saw it.

Contract:
  * a lane whose profile declares ``unsupported_request_shapes`` covering the
    request's shape is skipped by the chain walker: ONE INFO line, no banner,
    no HTTP call; a request without that shape still uses the lane;
  * the bridge codes classify ``lane_incapable`` (fail over, never retry in
    place) and the §4.1 ledger class is ``lane_incapable`` from the body code;
  * the banner names OUR bridge/relay as the source, never ``(Anthropic 400)``.
"""

from __future__ import annotations

import datetime as _dt
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

import providers
from agent import fallback_capability as fc
from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.error_classifier import FailoverReason, classify_api_error
from run_agent import AIAgent

BASE = "http://127.0.0.1:18811/v1"
TOOLS_BODY = {"error": {"type": "invalid_request_error", "code": "tui_tools_unsupported",
                        "message": "interactive mode on this box serves tool-less turns only; "
                                   "tools[] must be empty (52 tools)"}}
IMAGES_BODY = {"error": {"type": "invalid_request_error", "code": "tui_images_unsupported",
                         "message": "interactive mode on this box serves image-less turns only"}}
MODE_BODY = {"error": {"type": "invalid_request_error", "code": "mode_not_allowed",
                       "message": "this sub does not serve x-hermes-bpx-mode: tui"}}
PLAIN_400 = {"error": {"type": "invalid_request_error", "message": "messages: field required"}}


def _status_error(status: int, body_json: dict, *, base: str = BASE,
                  headers: dict | None = None) -> openai.APIStatusError:
    """The OpenAI SDK's ``_make_status_error`` passes ``body=data.get("error", data)``:
    the INNER error object; the message embeds the whole dict."""
    req = httpx.Request("POST", f"{base}/chat/completions")
    resp = httpx.Response(status, request=req, json=body_json, headers=headers or {})
    msg = f"Error code: {status} - {body_json}"
    cls = openai.BadRequestError if status == 400 else openai.APIStatusError
    return cls(msg, response=resp, body=body_json.get("error", body_json))


# ── 1. classification (routing) ───────────────────────────────────────────

@pytest.mark.parametrize("body,code", [
    (TOOLS_BODY, "tui_tools_unsupported"),
    (IMAGES_BODY, "tui_images_unsupported"),
    (MODE_BODY, "mode_not_allowed"),
])
def test_bridge_codes_classify_lane_incapable(body, code):
    c = classify_api_error(_status_error(400, body), provider="claude-btpr", model="claude-fable-5-1")
    assert c.reason is FailoverReason.lane_incapable
    assert c.should_fallback is True and c.retryable is False
    assert c.error_context.get("error_code") == code


def test_plain_400_keeps_format_error():
    c = classify_api_error(_status_error(400, PLAIN_400), provider="claude-btpr", model="claude-fable-5-1")
    assert c.reason is FailoverReason.format_error


# ── 2. §4.1 ledger class from the body code ───────────────────────────────

def test_ledger_class_is_lane_incapable_from_body_code():
    cls, src = fbe.classify_trigger(
        text=f"Error code: 400 - {TOOLS_BODY}", http_status=400,
        body=TOOLS_BODY["error"])  # the SDK's inner object, as stash_api_error keeps it
    assert (cls, src) == ("lane_incapable", "relay_code")
    assert "lane_incapable" in fbe.TRIGGER_CLASSES and "lane_incapable" in fp.TRIGGER_CLASSES


def test_ledger_class_envelope_shape_too():
    cls, _ = fbe.classify_trigger(text="x", http_status=400, body=IMAGES_BODY)
    assert cls == "lane_incapable"


def test_plain_400_text_is_not_lane_incapable():
    cls, _ = fbe.classify_trigger(text="messages: field required", http_status=400,
                                  body=PLAIN_400["error"])
    assert cls != "lane_incapable"


# ── 3. banner names OUR hop, never Anthropic ──────────────────────────────

def _ts(h, m, s):
    return _dt.datetime(2026, 10, 5, h, m, s, tzinfo=_dt.timezone.utc).timestamp()


def test_rider_names_our_bridge_not_anthropic():
    row = {"trigger_class": "lane_incapable", "lane_code": "tui_tools_unsupported",
           "hop": "relay→bridge", "seat": "sub-vps-24", "http_status": 400,
           "from_provider": "claude-btpr", "attempts": 1, "first_err_ts": _ts(13, 27, 47),
           "err_head": TOOLS_BODY["error"]["message"]}
    rider = fp.format_cause_rider(row, tz=_dt.timezone.utc)
    assert rider.startswith("lane cannot serve tools")
    assert "400 tui_tools_unsupported at the bridge" in rider
    assert "not Anthropic" in rider and "on sub-vps-24" in rider
    assert "(Anthropic 400)" not in rider and "unclassified" not in rider
    assert rider.endswith("13:27:47")


def test_rider_mode_not_allowed_is_at_the_relay():
    row = {"trigger_class": "lane_incapable", "lane_code": "mode_not_allowed",
           "seat": None, "http_status": 400, "from_provider": "claude-dtlr",
           "attempts": 1, "first_err_ts": _ts(13, 27, 47)}
    rider = fp.format_cause_rider(row, tz=_dt.timezone.utc)
    assert "lane closed to this delivery mode" in rider
    assert "at the relay" in rider and "(sub unknown)" in rider


def test_build_row_records_lane_code_and_our_hop():
    """build_row: the relay stamps bridge->upstream on a bridge 400 it passed through;
    the row keeps the body code and the hop becomes ours."""
    agent = SimpleNamespace(session_id="s", _current_turn_id="s:1")
    err = _status_error(400, TOOLS_BODY, headers={
        "x-relay-error-class": "upstream_passthrough",
        "x-relay-error-hop": "bridge->upstream", "x-pool-served-by": "sub-vps-24"})
    fbe.stash_api_error(agent, err, 400)
    row = fbe.build_row(agent, "failover", from_provider="claude-btpr", from_model="claude-fable-5-1",
                        to_provider="claude-bpr", to_model="claude-fable-5-1",
                        reason=FailoverReason.lane_incapable)
    assert row["trigger_class"] == "lane_incapable"
    assert row["lane_code"] == "tui_tools_unsupported"
    assert row["hop"] == "relay→bridge"
    assert row["seat"] == "sub-vps-24"


# ── 4. the walker: declared-incapable hop is skipped, no HTTP call ────────

def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="m", usage=None)


FB_CHAIN = [
    {"provider": "incapable-lane", "model": "claude-fable-5-1", "base_url": "http://127.0.0.1:18816/v1"},
    {"provider": "capable-lane", "model": "claude-fable-5-1", "base_url": "http://127.0.0.1:18812/v1"},
]
_TOOL = {"type": "function", "function": {"name": "read_file", "description": "r",
                                          "parameters": {"type": "object", "properties": {}}}}


def _make_agent(*, with_tools: bool):
    with (
        patch("model_tools.get_tool_definitions", return_value=[_TOOL] if with_tools else []),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k-abcdef123456", base_url=BASE, provider="custom",
            model="claude-fable-5-1", quiet_mode=True, skip_context_files=True,
            skip_memory=True, fallback_model=FB_CHAIN,
        )
        agent.client = MagicMock()
        agent._api_max_retries = 3
        if with_tools and not agent.tools:
            agent.tools = [_TOOL]
        return agent


_INCAPABLE = providers.ProviderProfile(
    name="incapable-lane", base_url="http://127.0.0.1:18816/v1",
    # The declaration under test: this lane cannot serve a tools[] request.
    unsupported_request_shapes=frozenset({"tools"}),
)
_CAPABLE = providers.ProviderProfile(name="capable-lane", base_url="http://127.0.0.1:18812/v1")


def _profile_for(name: str):
    return {"incapable-lane": _INCAPABLE, "capable-lane": _CAPABLE}.get(name)


def _run(agent, fake_api_call, caplog):
    fb_client = MagicMock()
    fb_client.api_key = "k-abcdef123456"
    fb_client._custom_headers = None
    fb_client.default_headers = None

    def _resolve(provider, model=None, **kw):
        fb_client.base_url = kw.get("explicit_base_url") or BASE
        return fb_client, model

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.retry_utils.jittered_backoff", return_value=0.01),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch.object(providers, "get_provider_profile", side_effect=_profile_for),
        caplog.at_level(logging.INFO, logger="agent.chat_completion_helpers"),
    ):
        return agent.run_conversation("hello")


def test_tool_request_skips_incapable_hop_with_exactly_one_http_call(caplog):
    agent = _make_agent(with_tools=True)
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model, bool(api_kwargs.get("tools"))))
        if agent.provider == "custom":
            raise _status_error(400, TOOLS_BODY)
        if agent.provider == "incapable-lane":
            raise _status_error(400, TOOLS_BODY, base="http://127.0.0.1:18816/v1")
        return _mock_response("served with tools")

    result = _run(agent, fake_api_call, caplog)
    assert result["final_response"] == "served with tools"
    fallback_calls = [c for c in calls if c[0] != "custom"]
    # The declared-incapable hop costs no round-trip: ONE HTTP call after the primary.
    assert fallback_calls == [("capable-lane", "claude-fable-5-1", True)]
    assert "skipped incapable-lane: lane_incapable(tools)" in caplog.text
    # No banner / ledger row for the skipped hop: the one failover is custom -> capable-lane.
    ev = agent._last_fallback_event
    assert ev["old_provider"] == "custom" and ev["new_provider"] == "capable-lane"


def test_toolless_request_still_uses_the_lane(caplog):
    agent = _make_agent(with_tools=False)
    agent.tools = []
    calls = []

    def fake_api_call(api_kwargs):
        calls.append(agent.provider)
        if agent.provider == "custom":
            raise _status_error(503, {"error": "no eligible sub"})
        return _mock_response("served tool-less by the tui lane")

    result = _run(agent, fake_api_call, caplog)
    assert result["final_response"] == "served tool-less by the tui lane"
    assert [c for c in calls if c != "custom"] == ["incapable-lane"]
    assert "lane_incapable" not in caplog.text


# ── 5. shape detection + declaration plumbing ─────────────────────────────

def test_request_shape_detects_tools_and_native_images():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    assert fc.request_shape(msgs, [_TOOL]) == frozenset({"tools", "images"})
    assert fc.request_shape([{"role": "user", "content": "hi"}], None) == frozenset()
    assert fc.request_shape(msgs, []) == frozenset({"images"})


def test_profile_declaration_is_opt_in(monkeypatch):
    """A profile with the default (empty) declaration never skips; supports_vision=False
    alone is not a declaration (catalog-resolved default on most profiles)."""
    monkeypatch.setattr(providers, "get_provider_profile",
                        lambda n: SimpleNamespace(supports_vision=False, unsupported_request_shapes=frozenset()))
    assert fc.lane_incapable_shape("claude-whatever", frozenset({"images", "tools"})) is None
    monkeypatch.setattr(providers, "get_provider_profile",
                        lambda n: SimpleNamespace(unsupported_request_shapes=frozenset({"images"})))
    assert fc.lane_incapable_shape("claude-btpr", frozenset({"images", "tools"})) == "images"
    assert fc.lane_incapable_shape("claude-btpr", frozenset({"tools"})) is None
    assert providers.ProviderProfile(name="x").unsupported_request_shapes == frozenset()
