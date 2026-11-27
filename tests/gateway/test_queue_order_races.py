"""Two older /queue order faults (Prism 4f2cff4414e2 and 03e7f19cf6dc on hermes-agent PR 572).

B: five or more prompts queued during one successful turn must run in arrival order across the
interrupt recursion cap (the capped head goes back to the adapter slot; the orphan-overflow rescue
must not run the overflow ahead of it).
A: two idle ``/queue <text>`` prompts arriving together must both run once, in arrival order.
Both are driven through the real adapter door of ``test_queue_release_real_adapter``.
"""

import asyncio
import threading

import pytest

from tests.gateway.test_queue_release_real_adapter import KEY, _msg, _Rig, _wait


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [5, 6, 8])
async def test_many_queued_prompts_run_in_order_across_recursion_cap(monkeypatch, tmp_path, n):
    rig = _Rig(monkeypatch, tmp_path)
    rig.gates = {"first": threading.Event()}
    rig.gate_completes.add("first")
    await rig.start_first_turn()
    items = [f"z{i}" for i in range(1, n + 1)]
    for text in items:
        await rig.adapter.handle_message(_msg(f"/queue {text}"))
    rig.gates["first"].set()
    await rig.settle(expect=n + 1)
    await asyncio.sleep(0.3)
    assert rig.calls == ["first", *items]
    assert rig.max_live == 1


@pytest.mark.asyncio
async def test_new_queue_item_during_capped_head_runs_after_the_older_backlog(monkeypatch, tmp_path):
    """Prism e40b259b1e29: while the capped head z4 runs, the slot is empty and z5 is in overflow;
    a new `/queue z6` must still run after z5."""
    rig = _Rig(monkeypatch, tmp_path)
    rig.gates = {"first": threading.Event(), "z4": threading.Event()}
    rig.gate_completes.update({"first", "z4"})
    await rig.start_first_turn()
    for text in ("z1", "z2", "z3", "z4", "z5"):
        await rig.adapter.handle_message(_msg(f"/queue {text}"))
    rig.first_started.clear()
    rig.gates["first"].set()
    for _ in range(500):
        if "z4" in rig.calls:
            break
        await asyncio.sleep(0.01)
    await rig.adapter.handle_message(_msg("/queue z6"))
    rig.gates["z4"].set()
    await rig.settle(expect=7)
    await asyncio.sleep(0.3)
    assert rig.calls == ["first", "z1", "z2", "z3", "z4", "z5", "z6"]
    assert rig.max_live == 1


@pytest.mark.asyncio
async def test_two_idle_queue_prompts_racing_to_the_claim_both_run_in_order(monkeypatch, tmp_path):
    """Fault A through the real ``_handle_message``: `/queue a` and `/queue b` both pass the idle
    check; a takes the sentinel and is held at the lease claim; b reaches the claim while a holds
    it. b must be queued behind a, never bump a's generation and cancel it."""
    from unittest.mock import AsyncMock, MagicMock

    from tests.gateway.test_max_concurrent_sessions import (
        _make_event, _make_runner, _silence_global_gateway_hooks,
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(None)
    adapter = runner.adapters[next(iter(runner.adapters))]
    runner._delivery_adapter_for = lambda source: adapter
    runner._persist_active_agents = MagicMock()
    runner._post_turn_hooks_after_delivery = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    calls = []

    async def agent(ev, src, key, generation):
        calls.append(ev.text)
        return "ok"

    runner._handle_message_with_agent = agent
    flush_gates = {"/queue a": asyncio.Event(), "/queue b": asyncio.Event()}
    real_flush = runner._hm_queue_during_lease_flush

    async def flush(event, *args, **kwargs):
        gate = flush_gates.get(event.text)
        if gate is not None:
            await gate.wait()
        return await real_flush(event, *args, **kwargs)

    runner._hm_queue_during_lease_flush = flush
    claim_entered, claim_gate, claims = asyncio.Event(), asyncio.Event(), []
    real_claim = runner._claim_active_session_slot_off_loop

    async def claim(*args, **kwargs):
        claims.append(1)
        if len(claims) == 1:
            claim_entered.set()
            await claim_gate.wait()
        return await real_claim(*args, **kwargs)

    runner._claim_active_session_slot_off_loop = claim
    t_a = asyncio.create_task(runner._handle_message(_make_event("/queue a")))
    t_b = asyncio.create_task(runner._handle_message(_make_event("/queue b")))
    await asyncio.sleep(0.05)                       # both past the idle check, parked before the claim
    flush_gates["/queue a"].set()
    await _wait(claim_entered)
    flush_gates["/queue b"].set()
    await asyncio.sleep(0.05)
    claim_gate.set()
    await asyncio.gather(t_a, t_b, return_exceptions=True)
    key = next(iter(runner._session_run_generation))
    for _ in range(4):                              # the adapter's end-of-task drain
        nxt = adapter._pending_messages.pop(key, None)
        if nxt is None:
            break
        await runner._handle_message(nxt)
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_orphaned_overflow_is_still_rescued_ahead_of_a_new_message(monkeypatch, tmp_path):
    """The rescue keeps working for a genuinely orphaned overflow item (unmarked)."""
    rig = _Rig(monkeypatch, tmp_path)
    rig.runner._session_state(KEY).conversation.queued_events.append(_msg("orphan"))
    await rig.adapter.handle_message(_msg("new"))
    await rig.settle(expect=2)
    await asyncio.sleep(0.3)
    assert rig.calls == ["orphan", "new"]
