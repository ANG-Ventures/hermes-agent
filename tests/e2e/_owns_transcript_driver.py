"""One "gateway process" lifetime for the owns_transcript restart E2E.

Run as a fresh interpreter per phase (``HERMES_HOME`` = scratch home) so the
second phase really is a restart: nothing survives but ``state.db``.

    phase ``interrupt``: build a real AIAgent over the scratch SessionDB, start a
        turn whose model reply is a ``terminal`` tool call, wait until the tool
        is running, then ``request_hard_interrupt`` the agent -- the exact call
        ``GatewayRunner._interrupt_running_agents`` makes on a restart -- and
        let the turn unwind (``close_interrupted_tool_sequence`` runs there).
    phase ``resume``: load the transcript the way ``SessionStore.load_transcript``
        does (``get_messages_as_conversation(..., repair_alternation=True)``)
        and send the next user turn with it as history.

argv: phase provider base_url session_id marker_path
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def _agent(provider: str, base_url: str, session_id: str, db):
    from run_agent import AIAgent

    return AIAgent(
        base_url=base_url,
        api_key="stub-key",
        provider=provider,
        api_mode="chat_completions",
        model="stub-model",
        enabled_toolsets=["terminal"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        session_id=session_id,
        session_db=db,
        platform="cli",
    )


def main() -> int:
    phase, provider, base_url, session_id, marker = sys.argv[1:6]
    from hermes_state import SessionDB
    from agent.interrupt_compat import request_hard_interrupt

    if os.environ.get("OWNS_TRANSCRIPT_E2E_SABOTAGE"):
        # Negative control only: the pre-#1101 send path (omit branch off).
        import agent.conversation_loop as _loop

        _loop.provider_owns_transcript = lambda _provider: False

    db = SessionDB()
    agent = _agent(provider, base_url, session_id, db)
    result: dict = {}

    if phase == "interrupt":
        prompt = f"RUN_TOOL:touch {marker} && sleep 60"

        def run():
            result["r"] = agent.run_conversation(prompt)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 60
        while not os.path.exists(marker) and time.time() < deadline and t.is_alive():
            time.sleep(0.1)
        if not os.path.exists(marker):
            print(json.dumps({"error": "tool never started"}))
            return 2
        time.sleep(0.3)
        request_hard_interrupt(agent, "Gateway restarting")
        t.join(timeout=60)
        if t.is_alive():
            print(json.dumps({"error": "turn did not unwind after interrupt"}))
            return 3
        r = result.get("r") or {}
        print(json.dumps({"interrupted": bool(r.get("interrupted"))}))
        return 0

    if phase == "resume":
        history = db.get_messages_as_conversation(
            session_id, include_timestamp=True, repair_alternation=True
        )
        r = agent.run_conversation("status?", conversation_history=history)
        print(json.dumps({"final": r.get("final_response")}))
        return 0

    raise SystemExit(f"unknown phase {phase}")


if __name__ == "__main__":
    sys.exit(main())
