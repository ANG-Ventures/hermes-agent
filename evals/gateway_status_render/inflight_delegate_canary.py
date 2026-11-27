"""Staged E2E for t_90e5c823: a long delegate_task stays visible in the chat.

Two modes:

  rehearse   Offline, no network, no gateway. Builds REAL ``AIAgent`` parent/child objects, attaches the
             child with the real ``tools.delegate_tool_child_run._attach_child``, backdates the in-flight
             ``delegate_task`` to 6 min, then drives the real heartbeat loop
             (``GatewayRunner._run_agent_notify_long_running``) and the real progress-bubble painter
             (``TurnRunner._paint_inflight_notice``) against a stub adapter. Prints the exact text a user
             would see. Run it on the canary host after deploy, before the live run:

                 HERMES_HOME=$(mktemp -d) python evals/gateway_status_render/inflight_delegate_canary.py rehearse

  live       Prints the operator runbook for the canary gateway (NOT Apollo): config, the exact prompt to
             send in a Discord thread, and what the bubble must show at each minute. The screenshot is the
             acceptance artifact; Ace must be able to see the elapsed + child line himself.

Exit code: rehearse returns 0 only when both rendered texts carry the elapsed time and the child line.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

LIVE_PROMPT = (
    "E2E t_90e5c823: call delegate_task exactly once with ONE task: \"run the terminal command "
    "`sleep 400 && echo slept` (timeout 600) and reply with its output\". Wait for it synchronously "
    "(not background). Then reply DONE. Do nothing else."
)

RUNBOOK = f"""\
Live E2E, t_90e5c823 (operator-run, canary gateway only, never Apollo)

1. Deploy the merged change to the canary profile's runtime and restart only that gateway.
2. Canary config.yaml (profile of the canary gateway), then restart it:
     agent:
       gateway_notify_interval: 60          # heartbeat every minute so the run takes ~7 min, not 21
     display:
       tool_inflight_notice_seconds: 300    # the default; keep it explicit for the record
       platforms:
         discord:
           tool_progress: all               # the progress bubble must exist on this channel
3. Rehearse on the canary host first (must exit 0):
     HERMES_HOME=$(mktemp -d) python evals/gateway_status_render/inflight_delegate_canary.py rehearse
4. In a fresh Discord thread with the canary bot, send exactly:
     {LIVE_PROMPT}
5. Expect, in the SAME messages (edited in place, no new bubbles per minute):
     t+1..4 min  heartbeat:  "⏳ Working — N min — iteration i/M, delegate_task Nm - 1 child: 0 done,
                              1 running (child <6 hex>: API call #k, in terminal Ns)"
     t+5..6 min  progress bubble gains ONE last line:
                              "⏳ still inside delegate_task for 5m - 1 child: 0 done, 1 running (...)"
                 and that line is re-edited each minute (5m -> 6m), never re-posted.
     t+~7 min    child finishes: the "still inside" line disappears from the bubble; final reply DONE.
6. Screenshot the thread at t+6 min (bubble + heartbeat both visible). Attach it to card t_90e5c823 and
   post it where Ace reads (the card's home chat). Telegram/Matrix: repeat step 4 on a canary chat there
   if one exists; both use the same edit_message path (no platform-specific code in this change).
"""


def _agent():
    from run_agent import AIAgent

    return AIAgent(base_url="http://127.0.0.1:9/v1", api_key="sk-dummy", model="dummy-model", quiet_mode=True,
                   enabled_toolsets=[], disabled_toolsets=["*"], max_iterations=300)


def _build_scene():
    from tools.delegate_tool_child_run import _attach_child

    parent, child = _agent(), _agent()
    now = time.time()
    # What agent.tool_executor stamps when a call starts, backdated so the call has run 6 min.
    parent._current_tool, parent._current_tool_started_at = "delegate_task", now - 6 * 60
    _attach_child(parent, child)
    parent._delegate_children_log[-1] = (now - 6 * 60 + 2, parent._delegate_children_log[-1][1])
    child._api_call_count = 3
    child._current_tool, child._current_tool_started_at = "terminal", now - 340
    return parent, child


async def _heartbeat_text(parent) -> str:
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext

    os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = "0.01"
    runner = object.__new__(GatewayRunner)
    runner._running_agents, runner._restart_requested, runner._draining = {"sess": parent}, False, False
    runner._running_agents_ts, runner._session_run_generation = {}, {}
    sends = []

    async def _send(chat_id, content, metadata=None):
        sends.append(content)
        runner._restart_requested = True
        return SimpleNamespace(success=True, message_id="hb-1")

    adapter = MagicMock()
    adapter.send, adapter.edit_message = _send, AsyncMock()
    runner._delivery_adapter_for = lambda source: adapter
    disp = MagicMock()
    disp._display_surface_mode.return_value = "on"
    disp.resolve_display_setting.return_value = True
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform="discord"), session_key="sess")
    ctx.agent_holder[0] = parent
    try:
        await asyncio.wait_for(runner._run_agent_notify_long_running(disp, ctx, [None]), 5)
    except Exception as exc:  # the bare runner may lack state a newer heartbeat reads
        return f"<heartbeat loop could not run in rehearsal: {exc!r}>"
    return sends[0] if sends else "<no heartbeat sent>"


async def _bubble_text(parent) -> str:
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

    adapter = ProgressCaptureAdapter(Platform.DISCORD)
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.DISCORD), progress_queue=queue.Queue(),
                      _run_still_current=lambda: True, progress_grouping="accumulate")
    ctx.agent_holder, ctx.user_config = [parent], {}
    from gateway.display_config import resolve_display_setting
    ctx.resolve_display_setting = resolve_display_setting
    tr = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda source: adapter), ctx)
    st = tr._progress_edit_state(adapter)
    st.progress_lines, st.progress_msg_id = ['🔀 delegate_task: "E2E t_90e5c823"'], "progress-1"
    await tr._paint_inflight_notice(st)
    return adapter.edits[-1]["content"] if adapter.edits else "<no bubble edit>"


def rehearse() -> int:
    parent, _child = _build_scene()
    heartbeat = asyncio.run(_heartbeat_text(parent))
    bubble = asyncio.run(_bubble_text(parent))
    print("heartbeat:", heartbeat)
    print("bubble:\n" + bubble)
    want = ("delegate_task 6m - 1 child: 0 done, 1 running (child ", "API call #3, in terminal 5m")
    ok_hb = all(w in heartbeat for w in want)
    ok_bubble = "⏳ still inside delegate_task for 6m - 1 child: 0 done, 1 running (child " in bubble and want[1] in bubble
    print(f"heartbeat_ok={ok_hb} bubble_ok={ok_bubble}")
    return 0 if ok_hb and ok_bubble else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("rehearse", "live"))
    args = ap.parse_args()
    if args.mode == "live":
        print(RUNBOOK)
        return 0
    return rehearse()


if __name__ == "__main__":
    sys.exit(main())
