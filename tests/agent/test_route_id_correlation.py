"""S7 D1 (t_ebbae2c8): route_id is the ONE correlation id, harness side.

* Pooled lanes (claude-apr / claude-bpr): the relay mints; the harness sends
  no route id and records the relay's ``x-pool-route-id`` (origin ``relay``).
* Pinned lanes (claude-apx-N / claude-bpx-N): no relay in the path, so the
  harness mints ``h`` + 32 hex per HTTP attempt, sends it as
  ``x-hermes-route-id`` and records the SAME value (origin ``harness``).
* Auxiliary calls to a fleet lane: one harness id per aux call. The ledger
  row records what the WIRE saw on the served attempt (t_d5f71d8e): a pooled
  relay's own ``x-pool-route-id`` wins; otherwise the harness id, only if the
  served request actually carried it.
* lane-src now rides bpr and the pinned lanes too (apr already had it).
* Nothing is ever sent to a third-party provider.
"""
from __future__ import annotations

import sqlite3
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from agent import auxiliary_client as ac
from agent import chat_completion_helpers as cch
from agent.fork_ext import relay_headers as rh
from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import store


def _agent(provider: str, turn_id: str = "turn-1"):
    return SimpleNamespace(
        _current_turn_id=turn_id, provider=provider, model="claude-opus-4-8",
        api_mode="chat_completions", platform="discord", _delegate_depth=0,
    )


@pytest.fixture
def recorded(monkeypatch):
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    return rows


def _direct(monkeypatch, pool_headers=None):
    sent = []

    def call(_agent, kwargs):
        sent.append(dict(kwargs.get("extra_headers") or {}))
        return SimpleNamespace(usage=None, pool_headers=dict(pool_headers or {}))

    monkeypatch.setattr(cch, "should_use_direct_api_call", lambda _agent: True)
    monkeypatch.setattr(cch, "direct_api_call", call)
    return sent


@pytest.mark.parametrize("provider", ["claude-bpx-21", "claude-apx-7", "claude-bpx-0"])
def test_pinned_lane_sends_harness_id_and_ledger_row_carries_it(recorded, monkeypatch, provider):
    sent = _direct(monkeypatch)
    cch.interruptible_api_call(_agent(provider), {"model": "m", "extra_headers": {"x-keep": "1"}})
    rid = sent[0][rh.ROUTE_ID_HEADER]
    assert rid.startswith("h") and rh.ROUTE_ID_RE.fullmatch(rid)
    assert rh.route_id_origin(rid) == "harness"
    assert recorded[0]["route_id"] == rid
    assert sent[0]["x-keep"] == "1"
    assert sent[0][rh.LANE_SRC_HEADER] == "platform=discord;delegate_depth=0;aux_task=-"


def test_pinned_lane_mints_a_fresh_id_per_attempt(recorded, monkeypatch):
    sent = _direct(monkeypatch)
    agent, kwargs = _agent("claude-apx-7"), {"model": "m"}
    cch.interruptible_api_call(agent, kwargs)
    cch.interruptible_api_call(agent, kwargs)
    ids = [h[rh.ROUTE_ID_HEADER] for h in sent]
    assert len(set(ids)) == 2
    assert [r["route_id"] for r in recorded] == ids


@pytest.mark.parametrize("provider", ["claude-apr", "claude-bpr"])
def test_pooled_lane_never_gets_a_harness_id_relay_id_wins(recorded, monkeypatch, provider):
    relay_rid = "0123456789abcdef0123456789abcdef"
    sent = _direct(monkeypatch, pool_headers={"x-pool-route-id": relay_rid})
    # a stale harness id on reused kwargs must be dropped, not forwarded
    kwargs = {"model": "m", "extra_headers": {rh.ROUTE_ID_HEADER: "h" + "a" * 32}}
    cch.interruptible_api_call(_agent(provider), kwargs)
    assert rh.ROUTE_ID_HEADER not in sent[0]
    assert recorded[0]["route_id"] == relay_rid
    assert rh.route_id_origin(relay_rid) == "relay"


def test_bpr_now_carries_lane_src(recorded, monkeypatch):
    sent = _direct(monkeypatch, pool_headers={"x-pool-route-id": "f" * 32})
    cch.interruptible_api_call(_agent("claude-bpr"), {"model": "m"})
    assert sent[0][rh.LANE_SRC_HEADER].startswith("platform=discord;")


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "openai-codex", "custom"])
def test_third_party_provider_gets_neither_header(recorded, monkeypatch, provider):
    sent = _direct(monkeypatch)
    stale = {rh.ROUTE_ID_HEADER: "h" + "b" * 32, rh.LANE_SRC_HEADER: "platform=cli"}
    cch.interruptible_api_call(_agent(provider), {"model": "m", "extra_headers": stale})
    assert rh.ROUTE_ID_HEADER not in sent[0] and rh.LANE_SRC_HEADER not in sent[0]
    assert recorded[0]["route_id"] is None


def test_apr_keeps_the_lane_src_its_affinity_set_stamped(recorded, monkeypatch):
    sent = _direct(monkeypatch, pool_headers={"x-pool-route-id": "e" * 32})
    kw = {"model": "m", "extra_headers": {rh.LANE_SRC_HEADER: "platform=cli;delegate_depth=0;aux_task=-"}}
    cch.interruptible_api_call(_agent("claude-apr"), kw)
    assert sent[0][rh.LANE_SRC_HEADER] == "platform=cli;delegate_depth=0;aux_task=-"


@pytest.mark.parametrize("value,origin", [
    ("0" * 32, "relay"), ("h" + "0" * 32, "harness"), ("c" + "0" * 32, "cli"),
    ("x" + "0" * 32, None), ("0" * 31, None), ("H" + "0" * 32, None), (None, None), ("", None),
])
def test_origin_is_the_id_grammar(value, origin):
    assert rh.route_id_origin(value) == origin


# ---- auxiliary calls ------------------------------------------------------

def _wire(kwargs, relay_id=None):
    """The httpx response the served attempt would produce for ``kwargs``."""
    req = httpx.Request("POST", "http://127.0.0.1/v1/x", headers=dict(kwargs.get("extra_headers") or {}))
    return httpx.Response(200, request=req, headers={"x-pool-route-id": relay_id} if relay_id else {})


@pytest.mark.parametrize("provider,sent", [
    ("gemini-bridge", True), ("claude-bpr", True), ("claude-apr", True),
    ("claude-bpx-21", True), ("openrouter", False), ("nous", False), ("auto", False),
])
def test_aux_build_kwargs_carries_id_only_to_fleet_lanes(provider, sent):
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs(provider, "m", [{"role": "user", "content": "x"}], task="compression")
        assert route.id_for(provider) is None, "kwargs intent alone is not evidence the id was sent"
        rh.note_aux_http_response(_wire(kw))
    got = (kw.get("extra_headers") or {}).get(rh.ROUTE_ID_HEADER)
    assert (got == route.route_id) is sent
    assert (route.id_for(provider) == route.route_id) is sent


def test_aux_build_kwargs_outside_a_scope_sends_nothing():
    kw = ac._build_call_kwargs("gemini-bridge", "m", [{"role": "user", "content": "x"}], task="vision")
    assert rh.ROUTE_ID_HEADER not in (kw.get("extra_headers") or {})


def test_aux_id_is_recorded_only_for_the_provider_that_served(monkeypatch):
    """Fallback case: the id went to gemini-bridge, a third party served."""
    got = []
    monkeypatch.setattr("agent.aux_accounting.record_aux_api_call",
                        lambda response, task, route_info, route_id=None, pool_headers=None: got.append(route_id))

    def impl(**kw):
        first = ac._build_call_kwargs("gemini-bridge", "m", kw["messages"], task=kw["task"])
        rh.note_aux_http_response(_wire(first))
        if served != "gemini-bridge":  # fallback attempt: a fresh build, a third party serves
            rh.note_aux_http_response(_wire(ac._build_call_kwargs(served, "m", kw["messages"], task=kw["task"])))
        kw["route_info"]["provider"] = served
        return SimpleNamespace(choices=[], usage=None)

    monkeypatch.setattr(ac, "_call_llm_impl", impl)
    for served in ("gemini-bridge", "openrouter"):
        ac.call_llm("title_generation", messages=[{"role": "user", "content": "x"}])
    assert got[0] and got[0].startswith("h") and got[1] is None


# ---- t_d5f71d8e: the aux id is what the wire carried -----------------------

RELAY_RID = "0123456789abcdef0123456789abcdef"


class _FakeStream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            model="claude-haiku-4-5", id="msg_1", role="assistant", type="message",
        )


def _anthropic_adapter(relay_id=None):
    """A real _AnthropicCompletionsAdapter over a fake SDK client that builds the
    request from exactly the kwargs the adapter hands it (what reaches the wire)."""
    calls = []

    def stream(**kw):
        calls.append(kw)
        req = httpx.Request("POST", "http://127.0.0.1/v1/messages", headers=dict(kw.get("extra_headers") or {}))
        return _FakeStream(httpx.Response(200, request=req,
                                          headers={"x-pool-route-id": relay_id} if relay_id else {}))

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream, create=None))
    return ac._AnthropicCompletionsAdapter(client, "claude-haiku-4-5", base_url="http://127.0.0.1:18801/anthropic"), calls


@pytest.mark.parametrize("provider", ["claude-apr", "claude-apx-7"])
def test_anthropic_aux_adapter_forwards_the_route_header(provider):
    """P1 f9f24feb: the Messages adapter's kwargs allow-list dropped extra_headers."""
    adapter, calls = _anthropic_adapter()
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs(provider, "claude-haiku-4-5", [{"role": "user", "content": "x"}], task="title_generation")
        adapter.create(**kw)
    assert (calls[0].get("extra_headers") or {}).get(rh.ROUTE_ID_HEADER) == route.route_id
    assert route.id_for(provider) == route.route_id


def test_aux_id_is_none_when_the_served_request_never_carried_it():
    """An adapter that drops the header must not leave a ledger claim behind."""
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs("claude-apx-7", "m", [{"role": "user", "content": "x"}], task="vision")
        rh.note_aux_http_response(_wire({}))  # the wire saw no route header
    assert kw["extra_headers"][rh.ROUTE_ID_HEADER] == route.route_id
    assert route.id_for("claude-apx-7") is None


@pytest.mark.parametrize("provider", ["claude-apr", "claude-bpr"])
def test_pooled_aux_call_records_the_relay_minted_id(provider):
    """P1 a3c3697f: the pooled relay forwards ITS id to the box; the ledger must too."""
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs(provider, "m", [{"role": "user", "content": "x"}], task="title_generation")
        rh.note_aux_http_response(_wire(kw, relay_id=RELAY_RID))
    assert route.id_for(provider) == RELAY_RID
    assert rh.route_id_origin(route.id_for(provider)) == "relay"


def test_pooled_anthropic_aux_call_records_the_relay_id_end_to_end():
    adapter, _ = _anthropic_adapter(relay_id=RELAY_RID)
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs("claude-apr", "claude-haiku-4-5", [{"role": "user", "content": "x"}], task="title_generation")
        adapter.create(**kw)
    assert route.id_for("claude-apr") == RELAY_RID


class _PoolRelay(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        _PoolRelay.seen.append(self.headers.get(rh.ROUTE_ID_HEADER))
        body = json.dumps({
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-pool-route-id", RELAY_RID)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_openai_aux_client_records_the_relay_id_from_a_real_http_response(monkeypatch):
    """claude-bpr is chat_completions: the aux OpenAI client (real httpx, real
    loopback socket) reads the relay's x-pool-route-id off the served response."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    server = HTTPServer(("127.0.0.1", 0), _PoolRelay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = ac._create_openai_client(api_key="k", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        with rh.aux_route_scope() as route:
            kw = ac._build_call_kwargs("claude-bpr", "m", [{"role": "user", "content": "x"}], task="title_generation")
            client.chat.completions.create(**kw)
    finally:
        server.shutdown()
    assert _PoolRelay.seen[-1] == route.route_id
    assert route.id_for("claude-bpr") == RELAY_RID


def test_aux_row_carries_route_id_into_blackbox(monkeypatch):
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    agent = SimpleNamespace(_current_turn_id="t")
    cch._emit_aux_api_call_record(agent, "t", task="vision", provider="gemini-bridge",
                                  model="m", usage=None, api_mode="chat_completions",
                                  route_id="h" + "1" * 32)
    assert rows[0]["route_id"] == "h" + "1" * 32 and rows[0]["attribution"] == "aux:vision"


# ---- Blackbox store --------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650, "prefix_guard": False,
    })
    store._connect().close()
    return store._db_path()


def test_store_records_route_id_origin(db):
    for seq, rid in enumerate(["a" * 32, "h" + "b" * 32, None]):
        store.insert_api_call("turn-x", seq, ts=1.0, provider="claude-bpx-21", model="m",
                              usage=CanonicalUsage(input_tokens=1), sub_key=None,
                              attribution="pinned", route_id=rid)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT route_id, route_id_origin FROM turn_api_calls ORDER BY seq").fetchall()
    assert rows == [("a" * 32, "relay"), ("h" + "b" * 32, "harness"), (None, None)]


def test_store_migrates_an_existing_db_without_the_column(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store._connect().close()
    path = store._db_path()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE turn_api_calls DROP COLUMN route_id_origin")
    store._connect().close()
    with sqlite3.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turn_api_calls)")}
    assert "route_id_origin" in cols


# ---- t_f2fc31f6: the aux scope keeps the relay's x-pool-served-by -----------

def test_aux_scope_records_served_by_of_the_served_response_only():
    with rh.aux_route_scope() as route:
        kw = ac._build_call_kwargs("claude-bpr", "m", [{"role": "user", "content": "x"}], task="compression")
        req = httpx.Request("POST", "http://127.0.0.1/v1/x", headers=dict(kw.get("extra_headers") or {}))
        rh.note_aux_http_response(httpx.Response(200, request=req, headers={"x-pool-served-by": "sub-vps-7"}))
        assert route.pool_headers() == {"x-pool-served-by": "sub-vps-7"}
        # a fallback attempt rebuilds kwargs: the failed attempt's seat must not leak onto it
        ac._build_call_kwargs("openrouter", "m", [{"role": "user", "content": "x"}], task="compression")
        assert route.pool_headers() == {}
        rh.note_aux_http_response(httpx.Response(200, request=req))
        assert route.pool_headers() == {}


def test_aux_call_hands_served_by_to_the_recorder(monkeypatch):
    got = []
    monkeypatch.setattr("agent.aux_accounting.record_aux_api_call",
                        lambda response, task, route_info, route_id=None, pool_headers=None: got.append(pool_headers))

    def impl(**kw):
        sent = ac._build_call_kwargs("claude-bpr", "m", kw["messages"], task=kw["task"])
        req = httpx.Request("POST", "http://127.0.0.1/v1/x", headers=dict(sent.get("extra_headers") or {}))
        rh.note_aux_http_response(httpx.Response(200, request=req, headers={"x-pool-served-by": "sub-vps-4"}))
        kw["route_info"]["provider"] = "claude-bpr"
        return SimpleNamespace(choices=[], usage=None)

    monkeypatch.setattr(ac, "_call_llm_impl", impl)
    ac.call_llm("title_generation", messages=[{"role": "user", "content": "x"}])
    assert got == [{"x-pool-served-by": "sub-vps-4"}]
