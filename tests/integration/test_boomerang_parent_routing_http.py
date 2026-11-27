"""Synthetic HTTP providers, real boomerang entry and conversation fallback engine.

Only provider credential lookup and tool catalog discovery are substituted. No
production provider, platform delivery, card or gateway process is involved.
"""
from __future__ import annotations

import asyncio
import json
import signal
import threading
import uuid
from contextlib import nullcontext
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from openai import OpenAI

from gateway import kanban_boomerang_wake as wake
from run_agent import AIAgent
from tools import delegate_tool as dt


@contextmanager
def endpoints(sentinel, *, reply=None):
    trace = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "model" not in body:
                self.send_error(404)
                return
            lane = self.path.split("/")[1]
            trace.append({"ordinal": len(trace) + 1, "lane": lane, "model": body["model"],
                          "path": self.path, "auth": self.headers.get("Authorization") or self.headers.get("x-api-key")})
            response_text = reply(body) if reply else sentinel
            if lane != "cross-vendor":
                self.send_response(429)
                payload = {"type": "error", "error": {"message": "insufficient_quota: synthetic provider exhausted", "type": "insufficient_quota", "code": "insufficient_quota"}}
            else:
                self.send_response(200)
                payload = {"id": "synthetic-response", "object": "chat.completion", "created": 1,
                           "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
            if lane == "cross-vendor" and body.get("stream"):
                chunk = {"id": "synthetic-response", "object": "chat.completion.chunk", "created": 1,
                         "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}]}
                encoded = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                encoded = json.dumps(payload).encode()
                content_type = "application/json"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", trace
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def hermetic_http_runtime(monkeypatch):
    """Remove unrelated boot/probes and shorten the actual retry sleep, not failover."""
    import hermes_cli.plugins as plugins
    import agent.model_metadata as metadata
    import agent.turn_recovery as recovery
    import agent.relay_runtime as relay
    import socket
    connect = socket.socket.connect
    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("HTTP fixture attempted a non-loopback connection")
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(metadata, "get_model_context_length", lambda *a, **kw: 256000)
    monkeypatch.setattr(metadata, "_resolve_custom_endpoint_context_length", lambda *a, **kw: 256000)
    # These are private, newly-created DBs: no foreign process can hold an older WAL.
    # Keep the registry guard; do not enumerate every host /proc fd under home_io_guard.
    monkeypatch.setenv("HERMES_STATE_DELETED_WAL_CHECK", "registry")
    monkeypatch.setattr(relay._ProcessRelayPluginConfiguration, "_activate",
                        lambda *a, **kw: relay._RelayPluginConfigurationState.DISABLED)
    import agent.turn_api_error as api_error
    sleep = recovery.interruptible_backoff_sleep
    short_sleep = lambda agent, wait_time, *a, **kw: sleep(agent, min(wait_time, 0.001), *a, **kw)
    monkeypatch.setattr(recovery, "interruptible_backoff_sleep", short_sleep)
    monkeypatch.setattr(api_error, "interruptible_backoff_sleep", short_sleep)


@contextmanager
def bounded_alarm():
    def expired(*_):
        raise TimeoutError("boomerang HTTP case exceeded 30 seconds")
    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("scenario", ["summary", "downgrade", "busy", "cold", "detached", "active-fallback", "disabled", "auto-wake"])
def test_real_boomerang_fails_over_to_wire_sentinel(monkeypatch, tmp_path, scenario):
    import model_tools
    import agent.auxiliary_client as auxiliary
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore, SessionSource

    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr(model_tools, "check_toolset_requirements", lambda **kw: {})
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "wrong-worker-default", "fallback_providers": [], "max_iterations": 8})
    monkeypatch.setenv("SYNTHETIC_FALLBACK_KEY", "synthetic-key")
    parent = None
    sentinel = "BOOMERANG-WIRE-" + uuid.uuid4().hex
    with bounded_alarm(), endpoints(sentinel) as (base, trace):
        chain = [{"provider": "openrouter", "model": "second-model", "base_url": base + "/second/v1"},
                 {"provider": "openai", "model": "cross-model", "base_url": base + "/cross-vendor/v1"}]
        if scenario == "disabled":
            chain = []

        def resolve(provider, model=None, **kw):
            return OpenAI(api_key="synthetic-key", base_url=kw["explicit_base_url"], max_retries=0), model

        monkeypatch.setattr(auxiliary, "resolve_provider_client", resolve)
        config = GatewayConfig()
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        home = store.get_or_create_session(SessionSource(platform=Platform.DISCORD, chat_id="synthetic", chat_type="group"))
        store.set_model_override(home.session_key, {"provider": "openrouter", "model": "parent-pin"})
        parent = AIAgent(model="parent-pin", provider="openrouter", api_mode="chat_completions",
                         api_key="synthetic-key", base_url=base + "/primary/v1", fallback_model=chain,
                         enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True,
                         session_id=home.session_id)
        parent._session_messages = [{"role": "assistant", "content": "parent context"}]
        if scenario == "active-fallback":
            assert parent._try_activate_fallback()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config, runner.session_store = config, store
        runner._session_model_overrides = {}
        runner._refresh_fallback_model = lambda: chain
        runner._reresolve_model_override_credentials = lambda value: {
            "model": "parent-pin", "provider": "openrouter", "api_mode": "chat_completions",
            "api_key": "synthetic-key", "base_url": base + "/primary/v1"}
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *a, **kw: "wrong-global-default")
        monkeypatch.setattr(wake, "_live_agent", lambda *a: (None, False) if scenario == "cold" else (parent, scenario == "busy"))
        try:
            selected, mode = asyncio.run(wake.resolve_parent(runner, {"home_sid": parent.session_id, "mode": "fork" if scenario == "downgrade" else "summary"}, wait_s=0))
            if scenario == "auto-wake":
                state = wake.BoomerangWakeState(tmp_path / "wake-state.json")
                entry = {"key": "synthetic|nonce|1", "task_id": "synthetic-nonce", "home_sid": home.session_id,
                         "delegation_id": "synthetic-auto-wake", "mode": "summary", "row": "completed",
                         "wake_text": "synthetic nonce", "verdict_nonce": "abc123", "state": "claimed"}
                state.claim(entry["key"], entry)
                completed = []
                runner._async_profile_scope_for_source = lambda source: nullcontext()
                async def deliver(*a, **kw):
                    return 0
                monkeypatch.setattr(wake, "deliver_handbacks", deliver)
                monkeypatch.setattr(wake, "execute_verdict", lambda entry, final, **kw: (completed.append(final) or {"ok": True, "summary": final, "action": "complete"}))
                monkeypatch.setattr(wake, "deadman_owner_acted", lambda entry: False)
                monkeypatch.setattr(wake, "_breadcrumb", lambda *a: None)
                monkeypatch.setattr(wake, "ledger_append", lambda *a: None)
                asyncio.run(wake.run_entry(runner, entry, state))
                status, final = ("completed", completed[0]) if completed else ("failed", "no executor handback")
            elif scenario == "detached":
                from gateway.session_context import set_session_vars, restore_session_vars
                from tools.delegate_detached import build_parent_and_runner
                captured_children = []
                original_builder = dt._build_child_agent

                def capture(**kw):
                    child = original_builder(**kw)
                    captured_children.append(child)
                    return child

                monkeypatch.setattr(dt, "_build_child_agent", capture)
                status, final = wake.run_child(selected, "Return the provider's response verbatim.", mode)
                tokens = set_session_vars(platform="discord", source="gateway", chat_id="synthetic", session_key=home.session_key, session_id=parent.session_id)
                try:
                    record, _ = dt._build_durable_background_spec(
                        task_list=[{"goal": "Return the provider's response verbatim.", "inherit_mode": "summary"}],
                        shared_context=None, top_role="leaf", inherit_context=True,
                        cfg={"model": "wrong-worker-default", "fallback_providers": []},
                        creds={"model": None, "provider": None, "base_url": None, "api_mode": None},
                        parent_agent=parent, children=captured_children,
                        session_key=home.session_key, parent_session_id=parent.session_id,
                        origin_ui_session_id="", max_iterations=8,
                    )
                finally:
                    restore_session_vars(tokens)
                serialized = json.dumps(record)
                assert "synthetic-key" not in serialized
                restored = json.loads(serialized)
                assert restored["source"]["tasks"][0]["parent_route"]["source"] == "parent"
                # Simulate new defaults and a fresh credential resolution, not the original parent/client.
                def runtime(*a, **kw):
                    return {"provider": "openrouter", "api_mode": "chat_completions", "api_key": "synthetic-key", "base_url": base + "/primary/v1"}
                monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", runtime)
                recovered, _, rebuilt = build_parent_and_runner(restored, None, {"model": "new-worker-default"}, resolve_default=runtime, resolve_for_provider=runtime)
                trace.clear()
                try:
                    data = recovered()
                    item = data["results"][0]
                    status, final = item["status"], item["summary"]
                finally:
                    rebuilt.close()
            else:
                status, final = wake.run_child(selected, "Return the provider's response verbatim.", mode)
            (tmp_path / "wire-trace.json").write_text(json.dumps({"scenario": scenario, "trace": trace, "status": status, "final": final}, indent=2))
            print(json.dumps({"scenario": scenario, "trace": trace, "status": status, "sentinel": sentinel, "final": final}))
            if scenario == "disabled":
                assert status == "failed"
                assert sentinel not in final
                assert [row["lane"] for row in trace] == ["primary"]
                return
            assert status == "completed", "routing contract: completed: " + final
            assert sentinel in final, "routing contract: sentinel missing"
            expected_lanes = ["second", "cross-vendor"] if scenario == "active-fallback" else ["primary", "second", "cross-vendor"]
            expected_models = ["second-model", "cross-model"] if scenario == "active-fallback" else ["parent-pin", "second-model", "cross-model"]
            assert [row["lane"] for row in trace] == expected_lanes, "routing contract: lanes"
            assert [row["model"] for row in trace] == expected_models, "routing contract: models"
        finally:
            if parent is not None:
                parent.close()


@pytest.mark.platforms("posix")
def test_http_setup_exception_restores_alarm(monkeypatch, tmp_path):
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    @contextmanager
    def broken_endpoints(*a, **kw):
        raise RuntimeError("endpoint setup failed")
        yield
    monkeypatch.setattr(__name__ + ".endpoints", broken_endpoints)
    with pytest.raises(RuntimeError, match="endpoint setup failed"):
        test_real_boomerang_fails_over_to_wire_sentinel(monkeypatch, tmp_path, "summary")
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    restored = signal.getitimer(signal.ITIMER_REAL)
    assert restored[1] == previous_timer[1]
    assert 0 <= previous_timer[0] - restored[0] < 2


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mutation", ["drop-chain", "restore-defaults"])
def test_route_mutants_cannot_return_sentinel(monkeypatch, tmp_path, mutation):
    original = dt._build_child_agent

    def mutated(**kw):
        if mutation == "restore-defaults":
            kw["inherit_parent_route"] = False
        child = original(**kw)
        if mutation == "drop-chain":
            child._fallback_chain = []
        return child

    monkeypatch.setattr(dt, "_build_child_agent", mutated)
    with pytest.raises(AssertionError, match="routing contract: (completed|sentinel|lanes|models)"):
        test_real_boomerang_fails_over_to_wire_sentinel(monkeypatch, tmp_path, "downgrade")
