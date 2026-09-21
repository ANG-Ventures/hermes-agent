from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch


def _agent(turn_id: str, provider: str = "claude-apr", *, subagent: bool = False):
    return SimpleNamespace(
        _current_turn_id=turn_id,
        provider=provider,
        model="claude-opus-4-8",
        api_mode="anthropic_messages",
        is_subagent=subagent,
    )


def _usage(input_tokens: int, output_tokens: int):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=input_tokens // 2,
        cache_creation_input_tokens=input_tokens // 4,
    )


def _response(sub_key: str | None, input_tokens: int, output_tokens: int, **headers):
    pool_headers = dict(headers)
    if sub_key is not None:
        pool_headers["x-pool-served-by"] = sub_key
    return SimpleNamespace(
        usage=_usage(input_tokens, output_tokens),
        pool_headers=pool_headers,
    )


@pytest.fixture
def recorded(monkeypatch):
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    return rows


def _assert_pairing(rows, expected):
    observed = {
        row["sub_key"]: row["usage"].input_tokens
        for row in rows
        if row["usage"] is not None
    }
    assert observed == expected


def test_mid_turn_failover_records_asymmetric_values_per_served_sub(recorded):
    agent = _agent("turn-parent")

    cch._record_successful_api_call(agent, _response("sub-vps-7", 1000, 50))
    cch._record_successful_api_call(agent, _response("sub-vps-2", 10, 7))

    assert [(r["seq"], r["sub_key"], r["http_status"]) for r in recorded] == [
        (0, "sub-vps-7", 200),
        (1, "sub-vps-2", 200),
    ]
    _assert_pairing(recorded, {"sub-vps-7": 1000, "sub-vps-2": 10})
    assert sum(r["usage"].input_tokens for r in recorded) == 1010

    # Mutation control: I4's sum stays green when identities are transposed,
    # but the value-pairing oracle must go red.
    transposed = [dict(recorded[0]), dict(recorded[1])]
    transposed[0]["sub_key"], transposed[1]["sub_key"] = (
        transposed[1]["sub_key"],
        transposed[0]["sub_key"],
    )
    assert sum(r["usage"].input_tokens for r in transposed) == 1010
    with pytest.raises(AssertionError):
        _assert_pairing(transposed, {"sub-vps-7": 1000, "sub-vps-2": 10})


def test_429_then_200_records_zero_token_error_then_success(recorded):
    agent = _agent("turn-retry")
    response = SimpleNamespace(
        status_code=429,
        headers={
            "x-pool-served-by": "sub-vps-7",
            "x-pool-route-id": "route-429",
            "x-pool-unreachable": "1",
        },
    )
    class FakeStatusError(RuntimeError):
        def __init__(self):
            super().__init__("rate limited")
            self.status_code = 429
            self.response = response

    error = FakeStatusError()

    cch._record_failed_api_call(agent, error)
    cch._record_successful_api_call(agent, _response("sub-vps-2", 10, 7))

    first, second = recorded
    assert (first["seq"], first["usage"], first["http_status"]) == (0, None, 429)
    assert first["sub_key"] == "sub-vps-7"
    assert first["relay_synthetic"] is True
    assert first["route_id"] == "route-429"
    assert (second["seq"], second["sub_key"], second["usage"].input_tokens) == (
        1,
        "sub-vps-2",
        10,
    )


@pytest.mark.parametrize(
    ("provider", "sub_key"),
    [
        ("claude-apx-7", "claude-apx-7"),
        ("claude-bpx-12", "claude-bpx-12"),
        ("xai-oauth", "supergrok"),
        ("gemini-bridge", "gemini"),
    ],
)
def test_pinned_provider_records_constant_identity(recorded, provider, sub_key):
    agent = _agent("turn-pinned", provider)
    response = SimpleNamespace(usage=_usage(1000, 50))

    cch._record_successful_api_call(agent, response)

    assert recorded[0]["sub_key"] == sub_key
    assert recorded[0]["attribution"] == "pinned"
    assert recorded[0]["usage"].input_tokens == 1000


def test_headerless_200_records_null_sub_key(recorded):
    agent = _agent("turn-headerless", "anthropic")

    cch._record_successful_api_call(agent, SimpleNamespace(usage=_usage(1000, 50)))

    assert recorded[0]["sub_key"] is None
    assert recorded[0]["attribution"] == "wire"
    assert recorded[0]["http_status"] == 200
    assert recorded[0]["usage"].input_tokens == 1000


def test_openai_codex_pool_uses_wire_attribution(recorded, monkeypatch):
    entry = SimpleNamespace(access_token="opaque-test-token")
    pool = SimpleNamespace(current=lambda: entry)
    agent = _agent("turn-codex", "openai-codex")
    agent._credential_pool = pool
    monkeypatch.setattr("hermes_cli.auth.get_codex_account_id", lambda _token: "97ff9716abcdef")

    cch._record_successful_api_call(agent, SimpleNamespace(usage=_usage(1000, 50)))

    assert recorded[0]["sub_key"] == "codex:97ff9716"
    assert recorded[0]["attribution"] == "wire"



def test_delegated_subagent_stamps_own_turn_id(recorded):
    parent = _agent("turn-parent")
    child = _agent("turn-child", subagent=True)

    cch._record_successful_api_call(child, _response("sub-vps-7", 1000, 50))

    assert recorded[0]["turn_id"] == "turn-child"
    assert recorded[0]["sub_key"] == "sub-vps-7"
    assert recorded[0]["usage"].input_tokens == 1000
    assert parent._current_turn_id == "turn-parent"


def test_concurrent_parent_and_subagent_keep_turn_and_identity_paired(recorded):
    parent = _agent("turn-parent")
    child = _agent("turn-child", subagent=True)
    barrier = threading.Barrier(3)

    def record(agent, response):
        barrier.wait()
        cch._record_successful_api_call(agent, response)

    threads = [
        threading.Thread(target=record, args=(parent, _response("sub-vps-7", 1000, 50))),
        threading.Thread(target=record, args=(child, _response("sub-vps-2", 10, 7))),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    by_turn = {row["turn_id"]: row for row in recorded}
    assert (by_turn["turn-parent"]["sub_key"], by_turn["turn-parent"]["usage"].input_tokens) == (
        "sub-vps-7",
        1000,
    )
    assert (by_turn["turn-child"]["sub_key"], by_turn["turn-child"]["usage"].input_tokens) == (
        "sub-vps-2",
        10,
    )
    assert by_turn["turn-parent"]["seq"] == 0
    assert by_turn["turn-child"]["seq"] == 0


def test_pooled_provider_requires_transport_stamp(recorded, caplog):
    agent = _agent("turn-missing-stamp")
    cch._record_successful_api_call(agent, SimpleNamespace(usage=_usage(10, 7)))
    assert recorded == []
    assert agent._api_call_recording_failures == 1
    assert "lost pool_headers stamp" in caplog.text


def test_partial_stub_already_recorded_as_failure_is_not_double_counted(recorded):
    agent = _agent("turn-partial", "anthropic")
    stub = SimpleNamespace(
        usage=None,
        _api_call_failure_recorded=True,
    )
    cch._record_successful_api_call(agent, stub)
    assert recorded == []



def test_interruptible_api_call_success_wires_recorder(recorded, monkeypatch):
    agent = _agent("turn-wire", "anthropic")
    response = SimpleNamespace(usage=_usage(1000, 50))
    monkeypatch.setattr(cch, "should_use_direct_api_call", lambda _agent: True)
    monkeypatch.setattr(cch, "direct_api_call", lambda _agent, _kwargs: response)

    assert cch.interruptible_api_call(agent, {"model": "test"}) is response
    assert len(recorded) == 1
    assert recorded[0]["turn_id"] == "turn-wire"
    assert recorded[0]["usage"].input_tokens == 1000



def test_interruptible_api_call_error_wires_zero_token_recorder(recorded, monkeypatch):
    agent = _agent("turn-error-wire", "anthropic")
    monkeypatch.setattr(cch, "should_use_direct_api_call", lambda _agent: True)

    def fail(_agent, _kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cch, "direct_api_call", fail)
    with pytest.raises(RuntimeError, match="boom"):
        cch.interruptible_api_call(agent, {"model": "test"})
    assert len(recorded) == 1
    assert recorded[0]["turn_id"] == "turn-error-wire"
    assert recorded[0]["usage"] is None



def test_nonstream_transport_pairs_raw_headers_with_parsed_message(monkeypatch):
    captured = []

    class Agent:
        api_mode = "anthropic_messages"
        log_prefix = ""
        _disable_streaming = False

        def _capture_anthropic_response_headers(self, response):
            captured.append(response)

    agent = Agent()

    def create(client, api_kwargs, **kwargs):
        assert api_kwargs == {"model": "claude-opus-4-8"}
        kwargs["on_response"](
            SimpleNamespace(
                headers={
                    "x-pool-served-by": "sub-vps-7",
                    "x-pool-route-id": "route-7",
                }
            )
        )
        return SimpleNamespace(usage=_usage(1000, 50))

    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", create)
    message = cch._dispatch_nonstreaming_api_request(
        agent,
        {"model": "claude-opus-4-8"},
        make_client=lambda *args, **kwargs: object(),
    )

    assert message.pool_headers == {
        "x-pool-served-by": "sub-vps-7",
        "x-pool-route-id": "route-7",
    }
    assert len(captured) == 1
    assert "_capture_anthropic_response_headers" not in vars(agent)



def test_nonstream_chat_completions_pool_captures_raw_headers():
    parsed = SimpleNamespace(usage=_usage(1000, 50))
    raw = SimpleNamespace(
        headers={"x-pool-served-by": "sub-vps-12"},
        parse=lambda: parsed,
    )
    completions = SimpleNamespace(
        with_raw_response=SimpleNamespace(create=lambda **_kwargs: raw)
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = SimpleNamespace(api_mode="chat_completions", provider="claude-bpr")

    result = cch._dispatch_nonstreaming_api_request(
        agent,
        {"model": "test"},
        make_client=lambda *_args, **_kwargs: client,
    )

    assert result is parsed
    assert result.pool_headers == {"x-pool-served-by": "sub-vps-12"}



def test_real_anthropic_message_accepts_pool_header_stamp():
    from anthropic.types import Message, TextBlock, Usage

    message = Message(
        id="msg-test",
        content=[TextBlock(type="text", text="ok", citations=None)],
        model="claude-opus-4-8",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=10, output_tokens=7),
    )
    assert cch._stamp_pool_headers(
        message, {"x-pool-served-by": "sub-vps-7"}
    ).pool_headers == {"x-pool-served-by": "sub-vps-7"}



def test_response_identity_never_echoes_through_real_request_builder(recorded, monkeypatch):
    from agent.transports.anthropic import AnthropicTransport

    agent = _agent("turn-echo")
    agent.session_id = "session-echo"
    agent.platform = "cli"
    agent._delegate_depth = 0
    agent.tools = None
    agent.max_tokens = 1024
    agent.reasoning_config = None
    agent.request_overrides = {}
    agent.context_compressor = None
    agent._ephemeral_max_output_tokens = None
    agent._is_anthropic_oauth = False
    agent._anthropic_base_url = "http://127.0.0.1:18810"
    agent._oauth_1m_beta_disabled = False
    agent._get_transport = lambda: AnthropicTransport()
    agent._prepare_anthropic_messages_for_api = lambda messages: messages
    agent._anthropic_preserve_dots = lambda: False

    inbound = _response("sub-vps-7", 1000, 50, **{"x-pool-route-id": "route-7"})
    cch._record_successful_api_call(agent, inbound)
    built = cch.build_api_kwargs(agent, [{"role": "user", "content": "next"}])
    outbound = built.get("extra_headers", {})
    assert not any(name.lower().startswith("x-pool-") for name in outbound)

    # Mutation control at the production builder's routing-header source.
    monkeypatch.setattr(
        cch,
        "_pool_affinity_headers",
        lambda *_args, **_kwargs: {"x-pool-served-by": "sub-vps-7"},
    )
    mutated = cch.build_api_kwargs(agent, [{"role": "user", "content": "next"}])
    with pytest.raises(AssertionError):
        assert not any(
            name.lower().startswith("x-pool-")
            for name in mutated.get("extra_headers", {})
        )
