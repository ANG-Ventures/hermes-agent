#!/usr/bin/env python3
"""Boomerang v2 P2 live checks (spec AC-4 / AC-4b): real AIAgent parent on claude-alr, real
``_build_child_agent(inherit_mode=...)`` children, real relay.

Dev probe, not a tool (no registry.register; tools/dev_probes/ has no __init__.py).

Run from the repo root with HERMES_HOME pointing at a SCRATCH home (never ~/.hermes) that
carries the claude-alr provider registration:

  cache   AC-4: build a >= --prefix-tokens parent transcript, run ONE parent turn (the warm
          1h write, served seat captured by P1), then N fork children; each child's FIRST
          request usage is recorded. Writes one JSONL row per trial.
  cost    AC-4b: same warm parent; for each task run a fork child and a fold child to
          completion (bounded iterations, read-only toolsets) and sum usage over EVERY
          request. Writes one JSONL row per pair.

Usage is read off the provider response (``usage.input_tokens / cache_read_input_tokens /
cache_creation_input_tokens``), the same numbers the relay ledger records. Never prints keys.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Price weights relative to uncached input (Anthropic list ratios): cost is reported in
# "input-token equivalents" so fork and fold compare on one axis without a $ table.
W_INPUT, W_READ, W_WRITE_1H, W_WRITE_5M, W_OUTPUT = 1.0, 0.1, 2.0, 1.25, 5.0

_CALLS: list = []


def _install_capture():
    """Every successful main-loop call (streaming and not) passes through
    ``_record_successful_api_call(agent, response, api_kwargs)``; wrap it."""
    from agent import chat_completion_helpers as cch

    real = cch._record_successful_api_call

    def _capture(agent, response, api_kwargs=None):
        if response is not None and not getattr(response, "_api_call_success_recorded", False):
            _CALLS.append({"agent": id(agent), "headers": dict((api_kwargs or {}).get("extra_headers") or {}),
                           "resp": response, "t": time.time()})
        return real(agent, response, api_kwargs)

    cch._record_successful_api_call = _capture


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    cc = getattr(u, "cache_creation", None)
    get = (lambda o, k: (o.get(k) if isinstance(o, dict) else getattr(o, k, None)) or 0)
    return {
        "input": get(u, "input_tokens"), "cache_read": get(u, "cache_read_input_tokens"),
        "cache_creation": get(u, "cache_creation_input_tokens"), "output": get(u, "output_tokens"),
        "write_1h": get(cc, "ephemeral_1h_input_tokens") if cc is not None else 0,
        "write_5m": get(cc, "ephemeral_5m_input_tokens") if cc is not None else 0,
    }


def _cost(u: dict) -> float:
    w5 = u["write_5m"] if (u["write_1h"] or u["write_5m"]) else u["cache_creation"]
    return (u["input"] * W_INPUT + u["cache_read"] * W_READ + u["write_1h"] * W_WRITE_1H
            + w5 * W_WRITE_5M + u["output"] * W_OUTPUT)


def _calls_for(agent) -> list:
    return [c for c in _CALLS if c["agent"] == id(agent)]


def _served_by(resp):
    h = getattr(resp, "pool_headers", None) or {}
    return h.get("x-pool-served-by") if isinstance(h, dict) else None


def _build_history(target_tokens: int) -> list:
    """A real-text transcript: repo source files fed as user turns, short assistant acks.
    Ends on a complete text-only assistant turn (spec D-8)."""
    files = sorted(p for p in (REPO / "tools").glob("*.py") if p.stat().st_size > 40_000)
    files += sorted(p for p in (REPO / "agent").glob("*.py") if p.stat().st_size > 40_000)
    history, chars = [], 0
    for p in files:
        if chars // 4 >= target_tokens:
            break
        text = p.read_text(encoding="utf-8", errors="replace")[:120_000]
        history.append({"role": "user", "content": f"Read this file for later reference: {p.relative_to(REPO)}\n\n{text}"})
        history.append({"role": "assistant", "content": f"Read {p.relative_to(REPO)} ({len(text)} chars). Ready."})
        chars += len(text) + 80
    return history


def _make_parent(model: str, toolsets):
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider

    rp = resolve_runtime_provider(requested="claude-alr", target_model=model)
    parent = AIAgent(
        model=model, provider=rp.get("provider") or "claude-alr", base_url=rp.get("base_url"),
        api_key=rp.get("api_key"), api_mode=rp.get("api_mode"), quiet_mode=True, skip_context_files=True,
        skip_memory=True, platform="cli", enabled_toolsets=toolsets, max_iterations=4,
    )
    parent._cache_ttl = "1h"
    return parent


def _warm(parent, prefix_tokens: int) -> dict:
    history = _build_history(prefix_tokens)
    t0 = time.time()
    result = parent.run_conversation("Reply with exactly the single word: ack. Do not call any tools.",
                                     conversation_history=history)
    parent._session_messages = result["messages"]
    call = _calls_for(parent)[-1]
    u = _usage(call["resp"])
    return {"usage": u, "served_by": getattr(parent, "last_served_by", None), "seconds": round(time.time() - t0, 1),
            "history_rows": len(history), "prompt_tokens": u["input"] + u["cache_read"] + u["cache_creation"],
            "split": getattr(parent, "_last_cache_creation_split", None)}


def _child(parent, goal: str, mode: str, max_iterations: int):
    from tools.delegate_tool import _build_child_agent

    child = _build_child_agent(
        task_index=0, goal=goal, context=None, toolsets=None, model=None, max_iterations=max_iterations,
        task_count=1, parent_agent=parent, inherit_mode=mode,
    )
    return child


def _run_child(child, goal: str, parent=None):
    """Run the child through the LIVE supervisor ``delegate_tool._run_single_child``.

    QA F-1 (t_230d37a3): the first AC-4/AC-4b pack called ``fork_run_inputs`` here itself, so it
    measured a path production never took (the live supervisor ran forks on the bare goal). Going
    through ``_run_single_child`` measures exactly what a delegate_task fork sends."""
    from tools import delegate_tool

    out = delegate_tool._run_single_child(0, goal, child=child, parent_agent=parent)
    return {"final_response": (out or {}).get("summary") or ""}


def cmd_cache(a):
    parent = _make_parent(a.model, a.toolsets)
    warm = _warm(parent, a.prefix_tokens)
    print(json.dumps({"warm": warm}), flush=True)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    passed = 0
    for i in range(1, a.trials + 1):
        goal = f"Trial {i}: reply with exactly the single word: ack. Do not call any tools."
        child = _child(parent, goal, "fork", 1)
        mode = getattr(child, "_inherit_mode", None)
        try:
            _run_child(child, goal, parent)
        finally:
            calls = _calls_for(child)
            child.close()
        first = calls[0] if calls else None
        u = _usage(first["resp"]) if first else {}
        total = (u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_creation", 0)) or 1
        share = round(u.get("cache_read", 0) / total, 4)
        row = {
            "trial": i, "ts": time.time(), "mode": mode, "model": a.model, "lane": "claude-alr",
            "parent_prompt_tokens": warm["prompt_tokens"], "parent_served_by": warm["served_by"],
            "pin": (first or {}).get("headers", {}).get("x-hermes-pin-sub"),
            "served_by": _served_by(first["resp"]) if first else None, "usage": u, "read_share": share,
            "age_s": round(time.time() - parent.last_served_at, 1), "pass": mode == "fork" and share >= 0.85,
        }
        passed += row["pass"]
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
    print(json.dumps({"summary": {"trials": a.trials, "passed": passed}}), flush=True)


_TASKS = {
    "completed_verify": "A worker reported it finished adding inherit_mode to delegate_task. Verify the claim from "
                        "what you already know in this conversation and by reading tools/delegate_tool_fork.py: does "
                        "fork_eligible check the warm window? Answer yes/no with the line.",
    "red_review": "CI on the delegate_tool change is red on test_delegate_tool_fork_mode.py. Read that test file and "
                  "name the single test most likely to break if apply_fork_overlay stopped copying tools[]. One line.",
    "changes_requested": "A reviewer requested changes: 'fork_prefix_messages must drop an in-flight user row'. Read "
                         "tools/delegate_tool_fork.py and say whether the current code already does that. One line.",
    "first_crash_retry": "A worker crashed once importing tools/delegate_tool_child_run.py. Read the top 40 lines of "
                         "that file and say whether any import there is obviously broken. One line.",
    "long_investigation": "Investigate how the fork child's first-request seat pin flows: read "
                          "agent/fork_ext/relay_headers.py (consume_first_request_headers) and tools/delegate_tool.py "
                          "(_arm_first_request_pin) and explain in 5 lines when the pin is sent and when it is not.",
}


def _abs_paths(goal: str) -> str:
    """Task text names repo files relatively; the scratch home's terminal cwd is /tmp."""
    import re

    return re.sub(r"\b((?:tools|agent|tests)/[\w/]+\.py)\b", lambda m: str(REPO / m.group(1)), goal)


def cmd_cost(a):
    parent = _make_parent(a.model, a.toolsets)
    warm = _warm(parent, a.prefix_tokens)
    print(json.dumps({"warm": warm}), flush=True)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for name in a.tasks or list(_TASKS):
        goal = _abs_paths(_TASKS[name])
        row = {"task": name, "ts": time.time(), "model": a.model, "lane": "claude-alr",
               "parent_prompt_tokens": warm["prompt_tokens"]}
        for mode in ("fork", "summary"):
            child = _child(parent, goal, mode, a.max_iterations)
            try:
                res = _run_child(child, goal, parent)
            finally:
                calls = _calls_for(child)
                child.close()
            usages = [_usage(c["resp"]) for c in calls]
            row[mode] = {
                "effective_mode": getattr(child, "_inherit_mode", None) or "summary",
                "requests": len(usages), "cost_input_equiv": round(sum(_cost(u) for u in usages), 1),
                "usage_total": {k: sum(u[k] for u in usages) for k in usages[0]} if usages else {},
                "first_read_share": round(usages[0]["cache_read"] / max(1, usages[0]["input"] + usages[0]["cache_read"]
                                                                       + usages[0]["cache_creation"]), 4) if usages else None,
                "final": ((res or {}).get("final_response") or "")[:300],
            }
            # Keep the parent warm between children: reads within the 1h window refresh it.
        row["fork_le_fold"] = row["fork"]["cost_input_equiv"] <= row["summary"]["cost_input_equiv"]
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)


def main():
    if "/.hermes" in os.environ.get("HERMES_HOME", "/.hermes") and "scratch" not in os.environ.get("HERMES_HOME", ""):
        raise SystemExit("refusing: HERMES_HOME must be a scratch home")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("cache", "cost"):
        p = sub.add_parser(name)
        p.add_argument("--model", default="claude-sonnet-5-5")
        p.add_argument("--prefix-tokens", type=int, default=210_000)
        p.add_argument("--toolsets", nargs="*", default=["file"])
        p.add_argument("--out", required=True)
    sub.choices["cache"].add_argument("--trials", type=int, default=16)
    sub.choices["cost"].add_argument("--tasks", nargs="*")
    sub.choices["cost"].add_argument("--max-iterations", type=int, default=6)
    a = ap.parse_args()
    _install_capture()
    {"cache": cmd_cache, "cost": cmd_cost}[a.cmd](a)


if __name__ == "__main__":
    main()
