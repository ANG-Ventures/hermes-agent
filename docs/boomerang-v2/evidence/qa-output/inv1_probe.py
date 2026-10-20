"""INV-1 differential probe (Argus, t_230d37a3).

Drives the REAL delegate_task(...) entry point (sync, no new params) from a real AIAgent
parent on the tree given in argv[1], with only the provider HTTP call stubbed, and dumps the
child's provider kwargs (every request) as JSON. Run once on base and once on head; diff.

Usage: python inv1_probe.py <tree> <case> <out.json>
cases: plain | inherit_true | inherit_false
"""
import copy, json, os, re, sys, tempfile, time

tree, case, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, tree)
os.chdir(tree)
home = tempfile.mkdtemp(prefix="inv1-home-")
os.environ["HERMES_HOME"] = home
os.environ["HERMES_HOME"] = home
for k in list(os.environ):
    if k.startswith(("HERMES_KANBAN", "HERMES_KANBAN")):
        del os.environ[k]
os.environ["TZ"] = "UTC"

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from tools import delegate_tool
from tools.delegate_tool import delegate_task

assert os.path.realpath(delegate_tool.__file__).startswith(os.path.realpath(tree)), delegate_tool.__file__

_TOOLS = [{"type": "function", "function": {"name": n, "description": f"{n} tool",
           "parameters": {"type": "object", "properties": {}}}} for n in ("read_file", "terminal")]

captured = []  # (agent_session_id, kwargs)


def _resp(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           stop_sequence=None, model="claude-fable-5-1", usage=None)


def _create(self, kwargs, **_kw):
    captured.append((getattr(self, "session_id", None), copy.deepcopy(kwargs)))
    return _resp("Outcome: done. Changed: nothing.")


def _wire(agent):
    agent.provider, agent.model, agent.api_mode = "claude-alr", "claude-fable-5-1", "anthropic_messages"
    agent.base_url = agent._anthropic_base_url = "http://127.0.0.1:18810/anthropic"
    agent._anthropic_api_key = "test-key"  # gitleaks:allow (dummy)
    agent._anthropic_client = MagicMock()
    agent._is_anthropic_oauth = False
    agent.client = MagicMock()
    agent.save_trajectories = False
    return agent


with ExitStack() as st:
    st.enter_context(patch("model_tools.get_tool_definitions", return_value=copy.deepcopy(_TOOLS)))
    st.enter_context(patch("model_tools.check_toolset_requirements", return_value={}))
    st.enter_context(patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()))
    st.enter_context(patch("run_agent.OpenAI", return_value=MagicMock()))
    class _Stop(Exception):
        status_code = 400

    def _req_client(self, *a, **k):
        c = MagicMock()

        def _stream(**kw):
            captured.append((getattr(self, "session_id", None), copy.deepcopy(kw)))
            raise _Stop("argus-probe: captured first request")
        c.messages.stream.side_effect = _stream
        return c
    st.enter_context(patch.object(AIAgent, "_create_request_anthropic_client", _req_client))
    st.enter_context(patch.object(AIAgent, "_anthropic_messages_create", _create))
    st.enter_context(patch.object(AIAgent, "_persist_session", lambda self, *a, **k: None))
    st.enter_context(patch.object(AIAgent, "_save_trajectory", lambda self, *a, **k: None))
    st.enter_context(patch("tools.delegate_tool._load_config", return_value={}))
    parent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:18810/anthropic", provider="claude-alr",
                     model="claude-fable-5-1", api_mode="anthropic_messages", quiet_mode=True,
                     skip_context_files=True, skip_memory=True, session_id="20261018_200000_parent")
    _wire(parent)
    parent._cache_ttl = "1h"
    parent.last_served_by = "sub-vps-16"
    parent.last_served_at = time.time() - 60
    parent._last_cache_creation_split = {"1h": 1000, "5m": 0}
    r = parent.run_conversation("The Kestrel canary is at 12 percent.")
    parent._session_messages = r["messages"]
    n_parent = len(captured)

    real_build = delegate_tool._build_child_agent

    def _build_and_wire(*a, **k):
        c = real_build(*a, **k)
        return _wire(c) if getattr(c, "provider", None) == "claude-alr" else c

    st.enter_context(patch("tools.delegate_tool._build_child_agent", side_effect=_build_and_wire))
    kw = {"goal": "verify the canary", "parent_agent": parent, "background": False}
    if case == "inherit_true":
        kw["inherit_context"] = True
    elif case == "fork":
        kw["inherit_context"] = True
        kw["inherit_mode"] = "fork"
    elif case == "inherit_false":
        kw["inherit_context"] = False
    res = delegate_task(**kw)

child_reqs = [k for _, k in captured[n_parent:]]


def _norm(o):
    """Volatile values only: tmp home, session ids, delegation ids, timestamps."""
    s = json.dumps(o, sort_keys=True, default=str)
    s = s.replace(home, "<HOME>")
    s = re.sub(r"\d{8}_\d{6}_[0-9a-f]{6,}", "<SID>", s)
    s = re.sub(r"del-\d{8}-\d{6}-[0-9a-f]{4,}", "<DEL>", s)
    s = re.sub(r"\b20\d\d-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?", "<TS>", s)
    s = re.sub(r"[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, 20\d\d( \d{1,2}:\d\d ?[AP]M)?", "<DATE>", s)
    return json.loads(s)


try:
    parsed = json.loads(res)
except Exception:
    parsed = None
json.dump({"case": case, "n_child_requests": len(child_reqs),
           "parent_requests": [_norm(k) for _, k in captured[:n_parent]],
           "child_requests": [_norm(k) for k in child_reqs],
           "result_status": [r.get("status") for r in (parsed or {}).get("results", [])] if isinstance(parsed, dict) else None,
           "raw_result": parsed if parsed is not None else str(res)[:4000]},
          open(out, "w", encoding="utf-8"), indent=1, sort_keys=True, default=str)
print(case, "child_requests", len(child_reqs), "pin_headers",
      [(k.get("extra_headers") or {}).get("x-hermes-pin-sub") for k in child_reqs])
