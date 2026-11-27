"""Route snapshot survives a real constructor-time fingerprint downgrade."""
import copy
import threading
from contextlib import ExitStack
from unittest.mock import patch

from run_agent import AIAgent
from tools import delegate_tool as dt
from tools import delegate_tool_fork as fork
from tests.tools.test_delegate_tool_fork_mode import _make_parent, _patches
from tests.integration.test_boomerang_parent_routing_http import hermetic_http_runtime
import pytest


@pytest.mark.parametrize("mode,status,header,sent", [
    ("fork", 503, "drain", True), ("fork", 429, "quota", True),
    ("summary", 503, "drain", True), ("fork", 503, "", True),
    ("fork", 503, "drain", False)])
def test_pin_refusal_rebuild_requires_explicit_first_request_refusal(mode, status, header, sent):
    from types import SimpleNamespace
    agent = SimpleNamespace(_inherit_mode=mode, provider="claude-alr", _fork_rebuild_args={"x": 1}, _fork_pin_sent=sent)
    error = SimpleNamespace(status_code=status, response=SimpleNamespace(headers={"x-pool-pin-refused": header}))
    if mode == "fork" and status == 503 and header and sent:
        with pytest.raises(fork.ForkPinRefused):
            fork.raise_if_fork_pin_refused(agent, error)
    else:
        fork.raise_if_fork_pin_refused(agent, error)


def test_fingerprint_downgrade_does_not_resnapshot_switched_parent(monkeypatch):
    parent = _make_parent()
    parent._session_messages = [{"role": "assistant", "content": "parent context"}]
    parent._fallback_chain = [{"provider": "openai", "model": "old-backup"}]
    parent.request_overrides = {"extra_body": {"nonce": "old"}}
    before = (parent.model, parent.provider, parent.base_url, parent.api_key)
    captured = []
    real = AIAgent
    def construct(**kw):
        captured.append(copy.deepcopy({k: kw[k] for k in ("model", "provider", "base_url", "api_mode", "fallback_model", "request_overrides")}))
        child = real(**kw)
        if len(captured) == 1:
            from agent.route_snapshot import route_lock
            def switch():
                with route_lock(parent):
                    parent.model, parent.provider, parent.base_url, parent.api_key = "changed", "openai", "http://changed/v1", "CHANGED"
                    parent._fallback_chain = []
                    parent.request_overrides = {}
            writer = threading.Thread(target=switch)
            writer.start()
            writer.join(5)
            assert not writer.is_alive()
        return child
    monkeypatch.setattr(fork, "fork_eligible", lambda *a: (True, ""))
    monkeypatch.setattr(fork, "request_fingerprint", lambda agent: {"system": "parent" if agent is parent else "different"})
    try:
        with ExitStack() as stack:
            _patches(stack)
            stack.enter_context(patch("tools.delegate_tool._load_config", return_value={"model": "wrong-worker", "fallback_providers": []}))
            stack.enter_context(patch("run_agent.AIAgent", construct))
            child = dt._build_child_agent(task_index=0, goal="g", context=None, toolsets=None, model=None,
                max_iterations=4, task_count=1, parent_agent=parent, inherit_mode="fork", inherit_parent_route=True)
        try:
            assert len(captured) == 2
            assert captured[0] == captured[1], "fingerprint downgrade recaptured the changed live route"
            assert (child.model, child.provider, child.base_url, child.api_key) == before
            assert child._context_inherited == "summary"
            assert not getattr(child, "_first_request_extra_headers", None)
            assert child._fallback_chain == [{"provider": "openai", "model": "old-backup"}]
        finally:
            child.close()
    finally:
        parent.close()


def test_pin_refused_fails_without_unowned_replacement(monkeypatch):
    import httpx
    import anthropic
    from tests.tools.test_delegate_tool_fork_mode import _anthropic_response
    parent = _make_parent()
    parent._session_messages = [{"role": "assistant", "content": "original parent context"}]
    parent._fallback_chain = [{"provider": "openai", "model": "permitted-backup"}]
    captured, requests = [], []
    before = (parent.model, parent.provider, parent.base_url, parent.api_key)
    class CapturedAgent(AIAgent):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._disable_streaming = True
            captured.append(self)
    def send(agent, kwargs, **kw):
        requests.append(copy.deepcopy(kwargs))
        if len(requests) == 1:
            assert kwargs["extra_headers"]["x-hermes-pin-sub"] == parent.last_served_by
            parent.model, parent.provider, parent.base_url, parent.api_key = "changed", "openai", "http://changed/v1", "CHANGED"
            parent._fallback_chain = []
            response = httpx.Response(503, headers={"x-pool-pin-refused": "drain"}, request=httpx.Request("POST", before[2]))
            raise anthropic.APIStatusError("pinned sub unavailable: drain", response=response, body={"error": "pinned sub unavailable: drain"})
        assert "x-hermes-pin-sub" not in kwargs.get("extra_headers", {})
        return _anthropic_response("PIN-REFUSED-SNAPSHOT-SENTINEL")
    monkeypatch.setattr(fork, "fork_eligible", lambda *a: (True, ""))
    monkeypatch.setattr(fork, "request_fingerprint", lambda *a: {"system": "fixed"})
    try:
        with ExitStack() as stack:
            _patches(stack)
            stack.enter_context(patch("run_agent.AIAgent", CapturedAgent))
            stack.enter_context(patch("tools.delegate_tool._load_config", return_value={"model": "wrong-worker", "fallback_providers": []}))
            stack.enter_context(patch.object(AIAgent, "_anthropic_messages_create", send))
            child = dt._build_child_agent(task_index=0, goal="verify", context=None, toolsets=None, model=None,
                max_iterations=4, task_count=1, parent_agent=parent, inherit_mode="fork", inherit_parent_route=True)
            result = dt._run_single_child(0, "verify", child, parent)
        assert result["status"] == "error", result
        assert len(captured) == 1 and len(requests) == 1
        from tools import delegate_tool_registry as registry
        with registry._active_subagents_lock:
            assert child._subagent_id not in registry._active_subagents
        assert child not in parent._active_children
    finally:
        for child in captured:
            child.close()
        parent.close()
