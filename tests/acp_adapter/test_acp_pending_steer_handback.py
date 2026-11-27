"""ACP must run a steer the turn handed back (t_2b16f0e6, Prism b472c065ea57).

``run_conversation`` closes steering as the turn returns and moves a steer that landed after the
finalizer's last drain into ``result["pending_steer"]``. ACP acked that steer ("Steer queued for
the active turn") but ``_finish_turn`` drained only ``state.queued_prompts``, so the text was lost.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionState


async def _noop_coro(*args, **kwargs):
    return None


def _make_server():
    server = HermesACPAgent.__new__(HermesACPAgent)
    server.session_manager = SimpleNamespace(save_session=lambda sid: None)
    server._conn = None
    drained = []

    async def fake_prompt(*, prompt, session_id, **kwargs):
        from acp.schema import PromptResponse

        drained.append([getattr(b, "text", None) for b in prompt])
        return PromptResponse(stop_reason="end_turn")

    server.prompt = fake_prompt  # type: ignore[method-assign]
    server._send_usage_update = _noop_coro  # type: ignore[method-assign]
    return server, drained


def _state(queued):
    return SessionState(
        session_id="s1", agent=SimpleNamespace(session_id="h1"), history=[], cancel_event=None,
        is_running=True, queued_prompts=list(queued), runtime_lock=threading.Lock(),
        current_prompt_text="hello",
    )


def test_handed_back_steer_runs_as_the_next_prompt():
    server, drained = _make_server()
    state = _state([])
    result = {"final_response": "done", "messages": [], "pending_steer": "use the revised requirements"}
    asyncio.run(server._finish_turn(state, "s1", None, result, "h1", False))
    assert drained == [["use the revised requirements"]], "the accepted steer was dropped"


def test_handed_back_steer_runs_before_prompts_queued_for_the_next_turn():
    """The steer was acked for the turn that just ended, so it runs before /queue'd prompts."""
    server, drained = _make_server()
    state = _state(["queued-one"])
    result = {"final_response": "done", "messages": [], "pending_steer": "late guidance"}
    asyncio.run(server._finish_turn(state, "s1", None, result, "h1", False))
    assert drained == [["late guidance"], ["queued-one"]]


def test_no_handback_leaves_the_queue_alone():
    server, drained = _make_server()
    state = _state(["queued-one"])
    asyncio.run(server._finish_turn(state, "s1", None, {"final_response": "done", "messages": []}, "h1", False))
    assert drained == [["queued-one"]]
