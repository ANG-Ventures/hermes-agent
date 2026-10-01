"""Prism on ANG-Ventures/hermes-agent#1595 (rounds 1-2) + Argus QA F3 (card t_c806b146).

Every test names the finding it pins. Each is RED on origin/main 2954cb40
(#1585 merged) unless it says "proof"; see the PR body for the run.

- r1 7dac78ce253e (:377 unbounded recursion): ``_tile`` recursed per entry.
- r1 ae8c65431bfe (:350 ambiguous delivery) and #1585 r0 1e4e955ac170: delivery
  settled by text. Drained batches now carry acceptance ids.
- r1 d066ae096b35 (:62 unredacted ledger): steer text hit the sandbox-mounted
  live dir raw.
- r1 26b4daf043fb + Argus F3 t_race1: a steer written into a tool result was
  settled though the turn exited by interrupt before any model read it.
- r1 f983a90e (:4370 door before hold): a parent close could win before the
  run took its hold; the run then used a closed child.
- r1 2910f31654b7 (:4702 steers outside steer_subagent): a direct
  ``child.steer`` (late-result notification) bypassed the ledger.
- r2 32e5dda430de (:374 trailing separator): ``a\\n`` tiled by ``a``.
"""
from __future__ import annotations

import ast
import inspect
import threading
import types
from unittest.mock import MagicMock


def _real_slot_child(sid, tmp_path):
    from run_agent import AIAgent
    from tools.delegate_tool import _SteerLedger

    child = types.SimpleNamespace(_pending_steer=None, _pending_steer_lock=threading.Lock())
    for name in ("steer", "_drain_pending_steer"):
        setattr(child, name, types.MethodType(getattr(AIAgent, name), child))
    led = _SteerLedger.for_child(child, sid)
    led.path = tmp_path / "steer.jsonl"
    return child, led


# r1 7dac78ce253e -------------------------------------------------------------
def test_r1_7dac_thousands_of_steers_in_one_batch_do_not_hit_the_recursion_limit(tmp_path):
    from tools.delegate_tool import _SteerLedger

    led = _SteerLedger(None)
    texts = [f"steer-{i}" for i in range(3000)]
    for t in texts:
        led.accept(t)
    led.deliver("\n".join(texts))  # base: RecursionError escapes deliver
    assert led.missed() is None


# r1 ae8c65431bfe / r0 1e4e955ac170 ---------------------------------------------
def test_r1_ae8c_earlier_batch_delivered_late_settles_its_own_acceptances(tmp_path):
    """Prism's example: "first\\nsecond" drained, then other/first/second
    accepted, then the earlier batch delivered and the rest discarded."""
    from agent.agent_runtime_helpers import note_steer_delivered

    child, led = _real_slot_child("sa-ae8c", tmp_path)
    assert child.steer("first\nsecond")
    batch = child._drain_pending_steer()
    for t in ("other", "first", "second"):
        assert child.steer(t)
    note_steer_delivered(child, batch)
    child._drain_pending_steer()  # discarded (finalizer / closure)
    assert led.missed() == "other\nfirst\nsecond"


def test_r0_1e4e_multiline_drop_then_separate_steers_delivered(tmp_path):
    from agent.agent_runtime_helpers import note_steer_delivered

    child, led = _real_slot_child("sa-1e4e", tmp_path)
    assert child.steer("foo\nbar")
    child._drain_pending_steer()  # dropped
    assert child.steer("foo")
    assert child.steer("bar")
    note_steer_delivered(child, child._drain_pending_steer())
    assert led.missed() == "foo\nbar"
    assert led.counts() == {"accepted": 1, "delivered": 2, "withdrawn": 0}


def test_r1_ae8c_requeued_batch_keeps_its_ids(tmp_path):
    """A batch put back behind newer text (no tool result to land in) and
    delivered with it settles exactly the steers in that delivery."""
    from agent.agent_runtime_helpers import note_steer_delivered, requeue_pending_steer

    child, led = _real_slot_child("sa-requeue", tmp_path)
    assert child.steer("a")
    held = child._drain_pending_steer()
    assert child.steer("b")
    requeue_pending_steer(child, held)
    assert child._pending_steer == "b\na"
    note_steer_delivered(child, child._drain_pending_steer())
    assert led.missed() is None


def test_r1_ae8c_interrupt_clear_drops_the_slot_and_those_steers_stay_missed(tmp_path):
    from agent.agent_runtime_helpers import note_steer_delivered

    child, led = _real_slot_child("sa-clear", tmp_path)
    assert child.steer("x")
    child._pending_steer = None  # AIAgent.interrupt clears the slot directly
    assert child.steer("x")
    note_steer_delivered(child, child._drain_pending_steer())
    assert led.missed() == "x"
    assert led.counts()["delivered"] == 1


# r1 d066ae096b35 -------------------------------------------------------------
def test_r1_d066_durable_ledger_copy_is_redacted(tmp_path):
    from tools.delegate_tool import _SteerLedger

    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
    led = _SteerLedger(tmp_path / "steer.jsonl")
    led.accept(f"use OPENAI_API_KEY={secret} for the retry")
    assert secret not in (tmp_path / "steer.jsonl").read_text()
    assert secret in led.missed()  # memory stays authoritative


# r1 26b4daf043fb + Argus F3 t_race1 ------------------------------------------
def _inject_after_tool_batch(child, text):
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results

    assert child.steer(text)
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    child._persist_user_message_idx = 0
    apply_pending_steer_to_tool_results(child, messages, 1)
    assert text in messages[-1]["content"]  # it IS in the tool result


def test_f3_t_race1_steer_in_tool_result_then_interrupt_exit_is_missed(tmp_path):
    child, led = _real_slot_child("sa-race1", tmp_path)
    _inject_after_tool_batch(child, "check the replica")
    # The live turn exits by interrupt here: no further model request.
    assert led.missed() == "check the replica"  # base: None (settled at append)


def test_f3_control_model_reads_it_then_it_is_delivered(tmp_path):
    from agent.agent_runtime_helpers import note_steer_consumed

    child, led = _real_slot_child("sa-race1-ctl", tmp_path)
    _inject_after_tool_batch(child, "check the replica")
    note_steer_consumed(child)  # conversation_loop: the next response came back
    assert led.missed() is None


def test_f3_the_loop_settles_only_after_a_model_response():
    """conversation_loop marks steers consumed after the API call returned,
    and both injection sites call note_steer_injected, not _delivered."""
    import agent.conversation_loop as cl
    import agent.agent_runtime_helpers as arh

    src = inspect.getsource(cl)
    assert "note_steer_consumed(agent)" in src
    assert src.index("note_steer_consumed(agent)") > src.index("run_llm_execution_middleware(")
    assert "note_steer_delivered(" not in src
    helper = inspect.getsource(arh.apply_pending_steer_to_tool_results)
    assert "note_steer_injected(agent, steer_text)" in helper


# r1 f983a90e -----------------------------------------------------------------
def test_r1_f983_parent_close_before_the_run_starts_fails_the_run():
    import run_agent
    from tools import delegate_tool as dt

    child = MagicMock()
    child._subagent_id = "sa-f983"
    child._delegate_depth = 1
    child.model = "m"
    child._credential_pool = None
    child.tool_progress_callback = None
    closes = []
    child.close = lambda: closes.append(1)
    dt._attach_owner_teardown(child)  # what _build_child_agent now does pre-publish
    assert run_agent._close_delegated_child(child, "parent_close") is True
    assert closes == [1]  # unheld: the door closed it
    entry = dt._run_single_child(0, "goal", child=child, parent_agent=MagicMock())
    assert entry["status"] == "error"
    assert "closed by its parent" in entry["error"]
    child.run_conversation.assert_not_called()  # base: ran on the closed child


def test_r1_f983_build_attaches_the_door_before_publishing_the_child():
    from tools import delegate_tool as dt

    tree = ast.parse(inspect.getsource(dt._build_child_agent))
    door = publish = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "_attach_owner_teardown":
                door = min(door or node.lineno, node.lineno)
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "append"
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "_active_children"
            ):
                publish = min(publish or node.lineno, node.lineno)
    assert door is not None and publish is not None and door < publish


# r1 2910f31654b7 -------------------------------------------------------------
def test_r1_2910_direct_child_steer_is_ledgered_and_reported_missed(tmp_path):
    """_start_late_completion calls parent_agent.steer() directly; on a
    delegated orchestrator the finalizer drains it after the last model call."""
    child, led = _real_slot_child("sa-2910", tmp_path)
    assert child.steer("[late result from sa-x] done")
    child._drain_pending_steer()  # turn_finalizer drain into pending_steer
    assert led.missed() == "[late result from sa-x] done"  # base: None


# r2 32e5dda430de -------------------------------------------------------------
def test_r2_32e5_trailing_separator_is_not_an_exact_tiling():
    from tools.delegate_tool import _SteerLedger

    open_ = [{"seq": 1, "text": "a", "state": "accepted"}]
    assert _SteerLedger._tile("a\n", open_) is None  # base: settles "a"
    assert _SteerLedger._tile("a", open_) == open_
