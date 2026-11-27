"""A thinking tool loop on the a-family Claude lanes must read back its own conversation cache.

t_65e9cc2c (#317): with ``preserve_thinking`` on and ``thinking_window="latest"`` the converter sends
the latest assistant turn WITH its signed thinking and the same turn WITHOUT it one request later, so
#317 moved the message breakpoints before that turn. That still paid every thinking step twice as
fresh input (1.20% of prompt tokens missed live, 84-88% read back in a replay).

t_d785218c (Ace 2026-11-13, "fully fix that cache issue"): the fleet window is ``contiguous``
(``prompt_caching.preserve_thinking_window``). Every assistant turn is sent on every later request
exactly as it was sent while it was the latest, until a compaction; no turn's bytes ever change, so
each request reads the previous one back whole and the #317 hoist has nothing to move. Setting the
config value to ``latest`` is the kill switch back to the #317 behaviour.

The replay drives the real cache planner, the real lane resolution and the real Anthropic converter
over growing transcripts and applies Anthropic's cache rule: a breakpoint reads the longest prefix an
earlier request wrote that ends at the breakpoint or at one of the 20 block positions before it (a
run of parallel tool_use / tool_result blocks counts as one position); everything after is fresh.
"""

from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

import agent.anthropic_message_convert as amc
import agent.preserved_thinking as pt
from agent.anthropic_adapter import build_anthropic_kwargs
from agent.prompt_caching import build_prompt_cache_plan
from hermes_cli.config_defaults import DEFAULT_CONFIG

STATIC = "static system prompt. " * 200
SYSTEM = STATIC + "volatile tail. " * 20
TOOLS = [{"type": "function", "function": {
    "name": "terminal", "description": "run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
}}]
LOOKBACK_BLOCKS = 20  # Anthropic checks for a cache hit at most 20 blocks before a breakpoint
FLEET_WINDOW = DEFAULT_CONFIG["prompt_caching"]["preserve_thinking_window"]

# One lane per kind: the alr face (local relay door) and a pinned apx seat unit.
LANES = {
    "claude-alr": "http://127.0.0.1:18810/anthropic",
    "claude-apx-9": "http://100.92.54.25:18809/anthropic",
}


@pytest.fixture
def lane_registry(monkeypatch):
    """The lane carries ``preserve_thinking`` with the pin factory's own window ("latest"); the
    fleet config default decides the window. ``state["window"]`` is ``prompt_caching``'s value."""
    pt._logged_sources.clear()
    state = {"window": FLEET_WINDOW}
    monkeypatch.setattr(pt, "_load_providers_config", lambda: {})
    monkeypatch.setattr(pt, "_load_window_default", lambda: state["window"])
    import providers as reg
    profiles = {name: SimpleNamespace(api_mode="anthropic_messages", preserve_thinking=True,
                                      preserve_thinking_window="latest") for name in LANES}
    monkeypatch.setattr(reg, "get_provider_profile", lambda name: profiles.get(name))
    return state


def _step(k: int, *, think: bool, signed: bool, tools: int) -> list:
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"toolu_{k}_{j}", "type": "function",
         "function": {"name": "terminal", "arguments": json.dumps({"command": f"ls {k} {j}"})}}
        for j in range(tools)]}
    if think:
        block = {"type": "thinking", "thinking": f"step {k}: what to run next. " * 10}
        if signed:
            block["signature"] = f"sig-{k}-" + "x" * 40
        msg["reasoning_details"] = [block]
    out = [msg]
    for j in range(tools):
        out.append({"role": "tool", "tool_call_id": f"toolu_{k}_{j}", "content": f"output of step {k}.{j}. " * 40})
    return out


def _transcript(steps: int, *, think=lambda k: True, signed: bool = True, tools: int = 1, user_every: int = 0) -> list:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "fix the failing test"}]
    for k in range(steps):
        if user_every and k and k % user_every == 0:
            msgs.append({"role": "assistant", "content": f"done with part {k}."})
            msgs.append({"role": "user", "content": f"now do part {k}"})
        msgs += _step(k, think=think(k), signed=signed, tools=tools)
    return msgs


def _request(provider: str, messages: list, *, preserve: bool = True) -> dict:
    """The request the main loop sends: cache plan first, then the lane's Anthropic kwargs."""
    base_url = LANES[provider]
    enabled, window, _ = pt.resolve_preserve_thinking(provider, base_url=base_url) if preserve else (False, "latest", "off")
    plan = build_prompt_cache_plan(copy.deepcopy(messages), TOOLS, cache_ttl="5m", native_anthropic=True,
                                   static_system_prefix=STATIC, direct_native_tool_cache=False)
    return build_anthropic_kwargs("claude-opus-4-8", plan.messages, plan.tools, 4096,
                                  {"enabled": True, "effort": "medium"}, base_url=base_url,
                                  preserve_thinking=enabled, thinking_window=window)


def _blocks(kwargs: dict) -> list:
    """``(kind, serialized block without its marker, has_marker)`` in prompt order."""
    out = []
    for tool in kwargs.get("tools") or []:
        out.append(("tool", json.dumps({k: v for k, v in tool.items() if k != "cache_control"}, sort_keys=True),
                    "cache_control" in tool))
    system = kwargs.get("system")
    for block in system if isinstance(system, list) else [{"type": "text", "text": system}]:
        out.append(("system", json.dumps({k: v for k, v in block.items() if k != "cache_control"}, sort_keys=True),
                    "cache_control" in block))
    for msg in kwargs["messages"]:
        content = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
        for block in content:
            body = {k: v for k, v in block.items() if k != "cache_control"}
            out.append((msg["role"] + ":" + block.get("type", ""), msg["role"] + json.dumps(body, sort_keys=True),
                        "cache_control" in block))
    return out


def _lookback(blocks: list, q: int) -> list:
    """Positions a breakpoint at ``q`` checks, newest first; a parallel tool run is one position."""
    out, i = [], q
    while i >= 0 and len(out) < LOOKBACK_BLOCKS:
        out.append(i)
        kind = blocks[i][0]
        if kind in ("assistant:tool_use", "user:tool_result"):
            while i - 1 >= 0 and blocks[i - 1][0] == kind:
                i -= 1
        i -= 1
    return out


def _replay_pct(provider: str, steps: int = 14, **shape) -> float:
    """Percent of each request's prompt the next request reads back from cache, over the loop."""
    written: dict = {}
    read_total = sent_total = 0
    prev_len = None
    for n in range(steps + 1):
        blocks = _blocks(_request(provider, _transcript(n, **shape)))
        digest, hashes, cum, size = hashlib.sha256(), [], [], 0
        for _, body, _ in blocks:
            digest.update(body.encode())
            hashes.append(digest.copy().hexdigest())
            size += len(body)
            cum.append(size)
        read = 0
        for q, (_, _, marked) in enumerate(blocks):
            if marked:
                hit = next((p for p in _lookback(blocks, q) if hashes[p] in written), None)
                read = max(read, cum[hit]) if hit is not None else read
        if prev_len is not None:
            read_total += read
            sent_total += prev_len
        for q, (_, _, marked) in enumerate(blocks):
            if marked:
                written[hashes[q]] = cum[q]
        prev_len = cum[-1]
    return 100.0 * read_total / sent_total


SHAPES = {
    "signed-1tool": dict(),
    "signed-4parallel": dict(tools=4),
    "signed-8parallel": dict(tools=8),
    "unsigned": dict(signed=False),
    "signed-user-every-4": dict(user_every=4),
    "even-steps": dict(think=lambda k: k % 2 == 0),
    "even-steps-8parallel": dict(think=lambda k: k % 2 == 0, tools=8),
    "no-thinking": dict(think=lambda k: False),
}


def test_fleet_default_window_is_contiguous():
    assert FLEET_WINDOW == "contiguous"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_thinking_loop_reads_back_its_whole_prompt(lane_registry, shape):
    assert pt.resolve_preserve_thinking("claude-alr", base_url=LANES["claude-alr"]) == (True, "contiguous", "lane")
    assert _replay_pct("claude-alr", **SHAPES[shape]) >= 99.0


def test_kill_switch_returns_the_lane_to_latest_and_the_hoist(lane_registry):
    """``prompt_caching.preserve_thinking_window: latest`` is one config edit back to #317."""
    lane_registry["window"] = "latest"
    assert pt.resolve_preserve_thinking("claude-alr", base_url=LANES["claude-alr"]) == (True, "latest", "lane")
    assert _replay_pct("claude-alr") < 95.0  # the gap this card closes is back
    kwargs = _request("claude-alr", _transcript(3))
    assistants = [m for m in kwargs["messages"] if m["role"] == "assistant"]
    assert [b["type"] for b in assistants[-1]["content"]][0] == "thinking"
    assert all(b["type"] != "thinking" for m in assistants[:-1] for b in m["content"])
    # the #317 hoist: no breakpoint on or after the latest thinking turn
    last_idx = max(i for i, m in enumerate(kwargs["messages"]) if m["role"] == "assistant")
    assert not any("cache_control" in b for m in kwargs["messages"][last_idx:] for b in m["content"])


@pytest.mark.parametrize("provider", sorted(LANES))
def test_every_turn_keeps_its_thinking_across_user_turns(lane_registry, provider):
    """No turn is ever stripped between compactions, and the request after a real user message
    repeats the request before it byte-for-byte, thinking included."""
    shape = dict(user_every=4)
    for n in (4, 8, 12):
        before = _transcript(n, **shape)
        after = _transcript(n + 1, **shape)  # carries the next real user message + one step
        prev, nxt = _blocks(_request(provider, before)), _blocks(_request(provider, after))
        assert [b for _, b, _ in nxt[:len(prev)]] == [b for _, b, _ in prev]
        thinking = [b for kind, b, _ in nxt if kind == "assistant:thinking"]
        assert len(thinking) == n + 1


@pytest.mark.parametrize("provider", sorted(LANES))
def test_kept_block_is_byte_identical_to_the_block_the_api_returned(lane_registry, provider):
    returned = {"type": "thinking", "thinking": "plan the fix.", "signature": "EqMBCkYICxIM" + "z" * 64}
    msgs = _transcript(5)
    msgs[2]["reasoning_details"] = [copy.deepcopy(returned)]
    wire = [b for m in _request(provider, msgs)["messages"] if m["role"] == "assistant" for b in m["content"]
            if b["type"] == "thinking"]
    assert len(wire) == 5
    assert json.dumps(wire[0], sort_keys=True) == json.dumps(returned, sort_keys=True)


@pytest.mark.parametrize("provider", sorted(LANES))
def test_breakpoints_stay_where_the_planner_put_them(lane_registry, provider, monkeypatch):
    """With nothing re-rendered the #317 hoist is inert: the last message block keeps its marker."""
    calls = []
    monkeypatch.setattr(amc, "_hoist_markers_before", lambda *a: calls.append(a))
    kwargs = _request(provider, _transcript(6))
    assert calls == []
    assert "cache_control" in kwargs["messages"][-1]["content"][-1]
    assert sum(1 for _, _, marked in _blocks(kwargs) if marked) <= 4


# Requests of routes that carry no preserved thinking (b/c/d families, direct anthropic.com): the
# wire must not move. sha256 of the request JSON, recorded on main eba69f24f7 before this change.
_GOLDEN = {
    "flag-off": "072b22a767e27b8541d431da3dcc511166432ca96718f2873b1d26fdf0bf9d9d",
    "direct": "5d813608020e547b2ee79ae4769a0ff7fad055e743088e4aaac129967cc17d4d",
}


def _sha(kwargs: dict) -> str:
    return hashlib.sha256(json.dumps(kwargs, sort_keys=True, default=str).encode()).hexdigest()


def test_routes_without_preserved_thinking_send_the_same_bytes(lane_registry):
    msgs = _transcript(5, user_every=2)
    assert _sha(_request("claude-alr", msgs, preserve=False)) == _GOLDEN["flag-off"]
    plan = build_prompt_cache_plan(copy.deepcopy(msgs), TOOLS, cache_ttl="5m", native_anthropic=True,
                                   static_system_prefix=STATIC, direct_native_tool_cache=False)
    direct = build_anthropic_kwargs("claude-opus-4-8", plan.messages, plan.tools, 4096,
                                    {"enabled": True, "effort": "medium"}, base_url="https://api.anthropic.com")
    assert _sha(direct) == _GOLDEN["direct"]


# ── compaction counts the kept thinking ─────────────────────────────────────────────────────────────

def _thinking_session(turns: int) -> list:
    """A stored transcript as the agent holds it: ``reasoning`` text + the signed block per turn."""
    think = "weighing the next command against the failing assertion. " * 60  # ~1K tokens
    msgs = [{"role": "system", "content": "You are Hermes."}, {"role": "user", "content": "fix it"}]
    for k in range(turns):
        msgs.append({"role": "assistant", "content": "", "reasoning": think,
                     "reasoning_details": [{"type": "thinking", "thinking": think, "signature": f"sig-{k}"}],
                     "tool_calls": [{"id": f"t{k}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{k}", "content": f"ok {k}"})
    return msgs


def test_a_session_carrying_kept_thinking_compacts_before_the_limit(lane_registry):
    """Every kept block is on the wire, so the compaction trigger and the tail walk charge it:
    the session crosses the threshold (and compacts) while its real prompt is still under the
    model limit, never a request over the limit. On the kill switch only the newest turn is
    charged, as before."""
    from agent.context_compressor import ContextCompressor
    from agent.turn_context import _preflight_rough_request_tokens

    agent = SimpleNamespace(api_mode="anthropic_messages", provider="claude-alr", requested_provider="",
                            model="claude-opus-5-5", base_url=LANES["claude-alr"], tools=None)
    msgs = _thinking_session(60)
    kept = _preflight_rough_request_tokens(agent, msgs, "")
    lane_registry["window"] = "latest"
    latest_only = _preflight_rough_request_tokens(agent, msgs, "")
    assert kept > 2 * latest_only  # the kept thinking is charged, not just the newest turn's
    lane_registry["window"] = "contiguous"

    limit = kept * 2  # the real prompt (thinking included) is half the model window
    cc = ContextCompressor(model="claude-opus-5-5", provider="claude-alr", api_mode="anthropic_messages",
                           base_url=LANES["claude-alr"], quiet_mode=True, config_context_length=limit)
    cc.threshold_tokens = (kept + latest_only) // 2  # between the two charges
    assert cc._stale_thinking_on_wire() is True
    assert cc.should_compress(kept) is True and kept < limit
    assert cc.should_compress(latest_only) is False  # charging the newest turn only would not fire


def test_tail_walk_and_preflight_agree_when_only_requested_provider_names_the_lane(lane_registry):
    """A named relay resolves as ``provider="custom"`` with the lane in ``requested_provider``.
    The compressor holds no ``requested_provider``; bound to its agent it must still give the
    preflight's answer, or a thinking-heavy session looks over threshold to preflight and fully
    tail-protected to the walk (Prism e401e23b1d4b)."""
    import weakref
    from unittest.mock import patch

    from agent.context_compressor import ContextCompressor
    from agent.turn_context import _agent_stale_thinking_on_wire
    from run_agent import AIAgent

    route = dict(api_mode="anthropic_messages", provider="custom", model="claude-opus-5-5",
                 base_url="http://127.0.0.1:1/unregistered")
    class _Agent(SimpleNamespace):  # weak-referenceable, as AIAgent is
        __slots__ = ("__weakref__",)

    agent = _Agent(requested_provider="claude-alr", tools=None, **route)
    assert _agent_stale_thinking_on_wire(agent) is True
    cc = ContextCompressor(quiet_mode=True, config_context_length=200_000, **route)
    cc._route_owner = weakref.ref(agent)
    assert cc._stale_thinking_on_wire() is True
    lane_registry["window"] = "latest"  # kill switch: both sides flip together
    assert _agent_stale_thinking_on_wire(agent) is False and cc._stale_thinking_on_wire() is False

    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        real = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",  # gitleaks:allow
                       quiet_mode=True, skip_context_files=True, skip_memory=True)
    assert real.context_compressor._route_owner() is real  # init binds the walk to its agent
