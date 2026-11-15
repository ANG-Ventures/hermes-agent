"""Real auto dispatch, child HTTP failover, verdict executor and durable handback."""
import asyncio
import json
import re
import signal
import threading
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as wake
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.kanban_wake_disposition import Disposition
from hermes_cli import kanban_db as kb
from run_agent import AIAgent
from tests.integration.test_boomerang_parent_routing_http import endpoints, hermetic_http_runtime


@pytest.mark.parametrize("scenario", ["busy", "cold", "snapshot-timeout", "owner-wake"])
def test_automatic_wake_dispatch_to_executor(monkeypatch, tmp_path, scenario):
    from openai import OpenAI
    import agent.auxiliary_client as auxiliary
    import model_tools
    from tools import delegate_tool as dt
    from gateway import kanban_home_route as hr

    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("auto wake 30s")))
    signal.alarm(30)
    parent = None
    release = threading.Event()
    holder = None
    try:
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
        kb.init_db()
        monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [])
        monkeypatch.setattr(model_tools, "check_toolset_requirements", lambda **kw: {})
        monkeypatch.setattr(dt, "_load_config", lambda: {"model": "wrong-worker", "fallback_providers": [], "max_iterations": 8})
        sentinel = "AUTO-WIRE-" + uuid.uuid4().hex
        def reply(body):
            text = json.dumps(body)
            nonce = re.search(r"verdict_nonce: ([a-f0-9]+)", text).group(1)
            return (sentinel + "\n<<<BOOMERANG-VERDICT nonce=" + nonce + ">>>\n" +
                    json.dumps({"action": "complete", "summary": sentinel, "evidence": ["local wire nonce"]}) +
                    "\n<<<END-VERDICT>>>")
        with endpoints(sentinel, reply=reply) as (base, trace):
            chain = [{"provider": "claude-alr", "model": "claude-second", "api_mode": "anthropic_messages", "base_url": base + "/second/anthropic"},
                     {"provider": "openai", "model": "cpa-sol", "api_mode": "chat_completions", "base_url": base + "/cross-vendor/v1"}]
            def resolve(provider, model=None, **kw):
                return OpenAI(api_key="SECOND-KEY" if provider == "claude-alr" else "CPA-KEY",
                              base_url=kw["explicit_base_url"], max_retries=0), model
            monkeypatch.setattr(auxiliary, "resolve_provider_client", resolve)
            config = GatewayConfig()
            store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
            home = store.get_or_create_session(SessionSource(platform=Platform.DISCORD, chat_id="scratch", chat_type="group"))
            store.set_model_override(home.session_key, {"provider": "claude-alr", "model": "claude-pin"})
            parent = AIAgent(model="claude-pin", provider="claude-alr", api_mode="anthropic_messages",
                api_key="ALR-KEY", base_url=base + "/primary/anthropic", fallback_model=chain,
                enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True, session_id=home.session_id)
            parent._session_messages = [{"role": "assistant", "content": "busy context"}]
            runner = GatewayRunner.__new__(GatewayRunner)
            runner.config, runner.session_store = config, store
            runner._session_model_overrides = {}
            runner._refresh_fallback_model = lambda: chain
            runner._reresolve_model_override_credentials = lambda value: {"model": "claude-pin", "provider": "claude-alr",
                "api_mode": "anthropic_messages", "api_key": "ALR-KEY", "base_url": base + "/primary/anthropic"}
            runner._running_agents = {} if scenario == "cold" else {home.session_key: parent}
            runner._agent_cache = {}
            runner._async_profile_scope_for_source = lambda source: nullcontext()
            monkeypatch.setattr(hr, "read_session_row", lambda sid: None)
            monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *a, **kw: "wrong-global")
            state = wake.BoomerangWakeState(tmp_path / "claims.json")
            monkeypatch.setattr(wake, "state_path", lambda: state.path)
            monkeypatch.setattr(wake, "_INFLIGHT", {})
            # Only wait duration is shortened; real busy/cold resolver and snapshot remain.
            async def resolve_parent(runner, entry):
                return await wake.resolve_parent(runner, entry, wait_s=0)
            monkeypatch.setattr(wake, "parent_resolver", resolve_parent)
            sent, turns = [], []
            class Adapter:
                async def send(self, chat_id, text, **kw):
                    sent.append(text)
                    return SimpleNamespace(success=True)
            runner._kanban_route_adapter = lambda *a: Adapter()
            async def deliver_turn(*a, **kw):
                turns.append(a[2])
                return False  # No admission: obligation must remain durable.
            monkeypatch.setattr("gateway.kanban_owner_wake.deliver_turn", deliver_turn)
            monkeypatch.setattr(wake, "post_logs", lambda *a: None)
            with kb.connect_closing() as conn:
                tid = kb.create_task(conn, title="isolated routing", assignee="worker", session_id=home.session_id, boomerang_wake=True)
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
                conn.commit()
                task = kb.get_task(conn, tid)
            if scenario == "snapshot-timeout":
                from agent.route_snapshot import route_lock
                entered = threading.Event()
                def hold():
                    with route_lock(parent):
                        entered.set()
                        release.wait(10)
                holder = threading.Thread(target=hold)
                holder.start()
                assert entered.wait(2)
            async def exercise():
                disp = wake.classify_event("completed", {}, task, 0)
                event = SimpleNamespace(id=1, kind="completed", created_at=1)
                if scenario == "owner-wake":
                    from gateway import kanban_owner_wake as ow
                    owner = ow.resolve_owner(store, home.session_id, hr.read_session_row)
                    remaining = await wake.owner_wake_filter(runner,
                        {"board": None, "task_id": tid, "title": "isolated routing", "home_sid": home.session_id},
                        [{"event_id": 1, "kind": "completed", "trigger": "completed", "event_ts": 1, "payload": {}}], owner, dispatch_on=True)
                    assert remaining == []
                else:
                    remaining = await wake.notifier_dispatch(runner, {"events": [event], "auto": {1: disp}},
                        {"platform": "discord", "chat_id": "scratch"}, None, task, {"completed"}, "original owner obligation")
                    assert remaining == set()
                await asyncio.wait_for(asyncio.gather(*wake._INFLIGHT.values()), timeout=25)
            asyncio.run(exercise())
            claim = next(iter(state.claims().values()))
            if scenario == "snapshot-timeout":
                assert not trace
                assert claim["outcome"] == "failed"
                assert "route activation is still in progress" in claim["reason"]
                assert claim["resurfaced"] and claim["resurface_delivered"] is False
                assert turns == ["original owner obligation"]
                assert state.handbacks()[claim["delegation_id"]]["parts"] == [wake.PART_TURN]
                async def accept_turn(*a, **kw):
                    turns.append(a[2])
                    return True
                monkeypatch.setattr("gateway.kanban_owner_wake.deliver_turn", accept_turn)
                monkeypatch.setattr(wake, "BACKOFF_BASE_S", 0.0)   # t_827d5940: retry on the next tick
                for _ in range(2):
                    asyncio.run(wake.reconcile(runner, state=state))
                assert turns == ["original owner obligation"] * 2
                assert state.handbacks() == {}
                assert state.get_claim(claim["key"])["resurface_delivered"] is True
            else:
                assert claim["outcome"] == "complete", claim
                assert [r["lane"] for r in trace] == ["primary", "second", "cross-vendor"], trace
                assert [r["model"] for r in trace] == ["claude-pin", "claude-second", "cpa-sol"]
                assert "/messages" in trace[0]["path"] and "/messages" in trace[1]["path"]
                assert trace[2]["path"].endswith("/chat/completions")
                assert [r["auth"] for r in trace] == ["ALR-KEY", "SECOND-KEY", "Bearer CPA-KEY"]
                assert len(sent) == 1 and sentinel in sent[0]
                # t_aad55db4 (#372): a complete verdict on a live chat home also starts ONE home turn.
                assert len(turns) == 1 and f"child completed {tid}" in turns[0], turns
                with kb.connect_closing() as conn:
                    receipts = [e for e in kb.list_events(conn, tid) if e.kind == wake.RECEIPT_KIND]
                assert len(receipts) == 1 and receipts[0].payload["action"] == "complete"
            (tmp_path / "auto-wire.json").write_text(json.dumps({"scenario": scenario, "trace": trace, "claim": claim, "sent": sent}, indent=2))
            print(json.dumps({"scenario": scenario, "trace": trace, "claim": claim, "sent": sent}))
    finally:
        release.set()
        if holder:
            holder.join(2)
        if parent:
            parent.close()
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
