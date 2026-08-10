"""Regression for #47967 — empty-name phantom tool calls.

Weak open models (mimo, nemotron-class) that see tool-call XML/JSON sitting in
file contents or tool output get *primed* and emit their own structured tool
calls that mimic the payload — usually with an empty/whitespace ``name``. Those
calls can't be fuzzy-repaired toward a real tool, so the dispatch loop returns an
error and the model retries. Before this fix, every empty-name error dumped the
full tool catalog back to the model, which fed the priming loop more names to
mimic and inflated context 3-4x across the retry budget.

The fix: a blank/whitespace-only tool name gets a terse anti-priming error that
tells the model in-context tool-call syntax is DATA, with NO catalog dump. A
genuinely-wrong-but-nonempty name (an actual typo) still gets the full catalog
so the model can self-correct.

These assert the *behavior contract* of the dispatch branch (what content goes
back to the model for each name shape), exercised end-to-end through
``AIAgent.run_conversation`` against an in-process mock provider — not a snapshot
of the message string.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import ModuleType

import pytest

# Repo root = three levels up from tests/agent/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_MISSING = object()


def _is_reload_target(module_name: str) -> bool:
    return (
        module_name == "run_agent"
        or module_name.startswith("agent.")
        or module_name.startswith("tools.")
        or module_name.startswith("hermes_")
    )


def _purge_reload_targets():
    modules = {
        name: module
        for name, module in list(sys.modules.items())
        if _is_reload_target(name)
    }
    parent_attrs = {}
    for name in modules:
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None:
            parent_attrs[(parent, child_name)] = parent.__dict__.get(
                child_name, _MISSING
            )

    for name in sorted(modules, key=lambda value: value.count("."), reverse=True):
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None:
            parent.__dict__.pop(child_name, None)
        sys.modules.pop(name, None)
    return modules, parent_attrs


def _restore_reload_targets(modules, parent_attrs):
    current_modules = {
        name: module
        for name, module in list(sys.modules.items())
        if _is_reload_target(name)
    }
    for name, module in sorted(
        current_modules.items(),
        key=lambda item: item[0].count("."),
        reverse=True,
    ):
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None and parent.__dict__.get(child_name) is module:
            parent.__dict__.pop(child_name, None)
        sys.modules.pop(name, None)

    sys.modules.update(modules)
    for (parent, child_name), value in parent_attrs.items():
        if value is _MISSING:
            parent.__dict__.pop(child_name, None)
        else:
            parent.__dict__[child_name] = value


@contextmanager
def _fresh_reload_scope():
    modules, parent_attrs = _purge_reload_targets()
    try:
        yield
    finally:
        _restore_reload_targets(modules, parent_attrs)


class _MockHandler(BaseHTTPRequestHandler):
    # Set by the fixture before each request cycle.
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):  # silence the default stderr logging
        pass


def _tc_resp(name: str, args: str = "{}") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _batch_tc_resp(calls: list[tuple[str, str]]) -> dict:
    """Multi-call batch response: calls = [(name, arguments), ...]."""
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": name, "arguments": args}}
                for i, (name, args) in enumerate(calls)
            ]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.fixture()
def agent_env():
    """Spin up the mock provider + an isolated HERMES_HOME, yield (agent, helpers)."""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_e2e_47967_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    # Import fresh so the patched conversation_loop is exercised even when the
    # module was imported earlier in the same worker.
    #
    # 🔴 This purge is the reason this file poisons later tests, so the teardown
    # below MUST restore what it removed. Deleting `agent.*` / `run_agent` /
    # `tools.*` / `hermes_*` from sys.modules makes every later importer get a
    # BRAND-NEW module object. Any subsequent test that captured a reference to
    # (or monkeypatched) one of these modules is then operating on a different
    # object than the code under test imports — measured 2026-08-09 as ~11
    # downstream failures in tests/agent/test_title_generator.py, whose provider
    # configuration silently applied to an orphaned module copy.
    try:
        with _fresh_reload_scope():
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
                provider="openai-compat", model="test-model",
                max_iterations=10, enabled_toolsets=[],
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                save_trajectories=False, platform="cli",
            )
            setattr(
                agent,
                "valid_tool_names",
                {"terminal", "read_file", "write_file", "execute_code", "session_search"},
            )

            try:
                yield agent, _MockHandler
            finally:
                # Tear down async logging before the fresh hermes_logging module
                # is removed or the temporary home is deleted. setup_logging()
                # parks its real file handlers in the module-global queue, not
                # on the public logger objects.
                try:
                    import hermes_logging as _hl

                    _hl._reset_queued_handlers()
                    _hl._logging_initialized = False
                except Exception:
                    pass
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def test_reload_scope_restores_modules_package_attrs_and_fresh_only_entries():
    """The E2E reload must leave the pre-collected module graph unchanged."""
    import agent
    import agent.auxiliary_client as original_auxiliary_client

    with _fresh_reload_scope():
        import agent.auxiliary_client as fresh_auxiliary_client

        assert fresh_auxiliary_client is not original_auxiliary_client
        fresh_only = ModuleType("agent._pollution_probe")
        sys.modules[fresh_only.__name__] = fresh_only
        agent.__dict__["_pollution_probe"] = fresh_only

    assert sys.modules["agent.auxiliary_client"] is original_auxiliary_client
    assert agent.__dict__["auxiliary_client"] is original_auxiliary_client
    assert "agent._pollution_probe" not in sys.modules
    assert "_pollution_probe" not in agent.__dict__


def _tool_results(handler) -> list[str]:
    out = []
    for req in handler.captured_requests:
        for m in req.get("messages", []):
            if m.get("role") == "tool":
                out.append(m.get("content", ""))
    return out


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_empty_tool_name_gets_terse_error_no_catalog(agent_env, blank):
    """A blank/whitespace tool name must NOT trigger a full tool-catalog dump."""
    agent, handler = agent_env
    handler.response_queue.append(_tc_resp(blank, "{}"))
    handler.response_queue.append(_text_resp("Recovered in plain text."))

    agent.run_conversation("read ./payload and report", conversation_history=[], task_id="t")

    joined = " ".join(_tool_results(handler))
    assert "tool name was empty" in joined
    # The whole point: do not feed the priming loop the catalog of names.
    assert "Available tools:" not in joined




# ── Mixed batches: valid calls execute, invalid calls get error results ──
#
# Degrading models (observed with gpt-5.6 past ~350K input; jonny's July 2026
# report) emit batches like 6 named calls + 1 blank-name call. Before the fix,
# the whole turn was voided ("Skipped: another tool call in this turn used an
# invalid name") and three such batches halted the session as partial even
# though most of the model's work was coherent.




def test_mixed_batch_preserves_tool_call_result_pairing(agent_env):
    """Every emitted tool_call keeps a matching tool result (provider invariant)."""
    agent, handler = agent_env
    agent.valid_tool_names = agent.valid_tool_names | {"todo"}
    handler.response_queue.append(_batch_tc_resp([("todo", "{}"), ("", "{}")]))
    handler.response_queue.append(_text_resp("done"))

    result = agent.run_conversation("track work", conversation_history=[], task_id="t")

    msgs = result["messages"]
    tc_ids = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            tc_ids.extend(tc["id"] for tc in m["tool_calls"])
    result_ids = [
        m.get("tool_call_id") or "" for m in msgs
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    # Both the valid and blank call must appear in the assistant message,
    # and each must have exactly one matching tool result.
    assert set(tc_ids) == {"call_0", "call_1"}
    assert sorted(result_ids) == sorted(tc_ids)






def test_invalid_tool_exhaustion_closes_tool_tail(agent_env):
    """Invalid-tool 3-strike partial must not leave a durable tool→user tail (#48879 class).

    Retries <3 append assistant+error tool rows, so the transcript already ends
    on ``tool`` before the exhaustion early-return. That return must close the
    sequence (same contract as interrupt aborts) so the next user turn is not
    ``tool → user`` for strict providers.
    """
    agent, handler = agent_env
    for _ in range(3):
        handler.response_queue.append(_tc_resp("frobnicate_xyz", "{}"))

    result = agent.run_conversation("degenerate", conversation_history=[], task_id="t")

    assert result.get("partial", False)
    msgs = result.get("messages") or []
    assert msgs, "expected persisted conversation messages"
    assert msgs[-1].get("role") == "assistant"
    assert "invalid tool call" in (msgs[-1].get("content") or "").lower()


