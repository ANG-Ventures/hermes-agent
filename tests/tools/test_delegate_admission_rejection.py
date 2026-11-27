"""Interactive admission must never turn into a foreground child join."""

import asyncio
import contextvars
import json
import queue
import threading
from unittest.mock import Mock

import pytest

from gateway.session_context import restore_session_vars, set_session_vars
from tests.tools.test_delegate_apiserver_background import _fake_parent, _patch_delegate
from tools import async_delegation as ad


@pytest.fixture
def admission(monkeypatch):
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr("tools.async_delegation_kanban.ineligible_reason", lambda **kw: "test")
    tokens = set_session_vars(platform="telegram", chat_id="admission-test",
                              session_key="admission-test", session_id="parent-test",
                              async_delivery=True)
    release = threading.Event()
    entered = threading.Event()

    def sentinel(task_index, goal, child=None, parent_agent=None, **kwargs):
        entered.set()
        assert release.wait(30), "sentinel release timed out"
        return {"task_index": task_index, "status": "completed", "summary": "finished",
                "api_calls": 1, "duration_seconds": 0.0}

    monkeypatch.setattr(dt, "_run_single_child", sentinel)
    yield dt, release, entered
    release.set()
    ad._reset_for_tests()
    restore_session_vars(tokens)


@pytest.mark.parametrize("failure", ["durable_intent_unavailable", "registry_cap", "registry_error",
                                    "pool_capacity", "submission_failed"])
def test_interactive_admission_returns_without_executing_child(admission, monkeypatch, failure):
    dt, release, entered = admission
    # Exercise the post-construction race; main tests preflight refusals separately.
    monkeypatch.setattr(ad, "preflight_admission", lambda **kw: None)
    if failure == "durable_intent_unavailable":
        monkeypatch.setattr(dt, "_build_durable_background_spec", Mock(side_effect=OSError("intent")))
    elif failure in {"registry_cap", "registry_error"}:
        monkeypatch.setattr(ad._store, "persist_dispatch", lambda **kw: (None, failure))
    elif failure == "pool_capacity":
        monkeypatch.setattr(dt, "_get_max_async_children", lambda: 0)
    else:
        executor = Mock()
        executor.submit.side_effect = RuntimeError("submission refused")
        monkeypatch.setattr(ad, "_get_executor", lambda *a: executor)
    done = threading.Event()
    result = {}
    parent = _fake_parent()
    closed = []
    close_child = dt._close_child_persistence

    def record_close(child):
        closed.append(child)
        close_child(child)

    monkeypatch.setattr(dt, "_close_child_persistence", record_close)

    def invoke():
        try:
            result["value"] = json.loads(dt.delegate_task(goal="sentinel", background=True, parent_agent=parent))
        except BaseException as exc:
            result["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=contextvars.copy_context().run, args=(invoke,), daemon=True)
    worker.start()
    try:
        assert done.wait(8), "interactive parent blocked on a rejected child"
        assert not entered.is_set(), "rejected child executed"
        assert "error" not in result, result
        value = result["value"]
        assert value["status"] == "rejected"
        assert value["reason"] == ("durable_intent_error"
                                   if failure == "durable_intent_unavailable" else failure)
        assert value["started"] is False
        assert value["retryable"] is True
        assert value["count"] == 1
        assert value["goals"] == ["sentinel"]
        assert value["error"]
        assert "delegation_id" not in value
        assert parent._active_children == []
        assert len(closed) == 1
        assert not ad._records
        from tools.process_registry import process_registry
        assert process_registry.completion_queue.empty()
        assert ad.enqueue_pending_outbox(current_boot_id="replay") == 0
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


def test_background_dispatch_leaves_parent_free_and_delivers_once(admission):
    from tools.process_registry import process_registry

    dt, release, entered = admission
    parent = _fake_parent()
    result = json.loads(dt.delegate_task(goal="sentinel", background=True, parent_agent=parent))
    assert result["status"] == "dispatched"
    assert entered.wait(8)
    # The same parent can handle another tool request while the child is held.
    followup = json.loads(dt.delegate_task(action="list", parent_agent=parent))
    assert isinstance(followup, dict)
    assert not release.is_set()
    assert process_registry.completion_queue.empty()
    release.set()
    event = process_registry.completion_queue.get(timeout=8)
    assert event["delegation_id"] == result["delegation_id"]
    assert event["results"][0]["summary"] == "finished"
    assert process_registry.completion_queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True])
async def test_http_parent_followup_before_child_release(admission, monkeypatch, tmp_path, refuse):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.wake import persist_delegation_delivery
    from hermes_state import SessionDB
    from tools.process_registry import process_registry

    dt, release, entered = admission
    if refuse:
        monkeypatch.setattr(dt, "_get_max_async_children", lambda: 0)
    db = SessionDB(db_path=tmp_path / "http-state.db")
    db.create_session("http-parent", source="api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    adapter._session_db = db
    replies = []

    def create_agent(**kwargs):
        parent = _fake_parent()
        parent.session_id = "http-parent"
        parent.session_prompt_tokens = parent.session_completion_tokens = parent.session_total_tokens = 0

        def run(**turn):
            reply = (json.loads(dt.delegate_task(goal="http sentinel", background=True, parent_agent=parent))
                     if not replies else {"followup": "handled"})
            replies.append(reply)
            return {"final_response": json.dumps(reply), "session_id": "http-parent",
                    "messages": [], "api_calls": 1}

        parent.run_conversation.side_effect = run
        return parent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)

    async def exercise():
        async with TestClient(TestServer(app)) as client:
            headers = {"Authorization": "Bearer fixture-key", "X-Hermes-Session-Id": "http-parent",
                       "X-Hermes-Session-History-Delivery": "1"}
            for text in ("delegate", "second parent request"):
                response = await client.post("/v1/chat/completions", headers=headers,
                                             json={"messages": [{"role": "user", "content": text}]})
                assert response.status == 200, await response.text()
                await response.read()
            assert replies[0]["status"] == ("rejected" if refuse else "dispatched")
            assert replies[1] == {"followup": "handled"}
            assert not release.is_set()
            if refuse:
                assert not entered.is_set()
                assert process_registry.completion_queue.empty()
                return
            release.set()
            event = await asyncio.to_thread(process_registry.completion_queue.get, True, 8)
            for _ in range(2):
                await persist_delegation_delivery(adapter, text="CHILD_COMPLETION",
                                                  session_id="http-parent", evt=event)
            assert sum(m["content"] == "CHILD_COMPLETION" for m in db.get_messages("http-parent")) == 1
            assert process_registry.completion_queue.empty()

    try:
        await asyncio.wait_for(exercise(), timeout=45)
    finally:
        release.set()
        db.close()


def test_exact_cap_pending_obligation_is_preserved_on_refusal(admission, monkeypatch):
    from tests.tools.test_async_delegation_terminal_receipts import dispatch, registry_record
    from tools.process_registry import process_registry

    dt, release, entered = admission
    identifier, worker, _ = dispatch(monkeypatch, session_key="another-session")
    worker()
    process_registry.completion_queue.get_nowait()
    before = registry_record(identifier)
    monkeypatch.setattr(ad, "_MAX_REGISTRY_RECORDS", 1)
    result = json.loads(dt.delegate_task(goal="cap sentinel", background=True, parent_agent=_fake_parent()))
    assert result["status"] == "rejected"
    assert result["reason"] == "registry_cap"
    assert not entered.is_set()
    assert registry_record(identifier) == before
    assert ad.enqueue_pending_outbox(current_boot_id="other-replay") == 1


def test_exact_cap_settled_history_makes_room_without_losing_pending_obligation(admission, monkeypatch):
    from tests.tools.test_async_delegation_terminal_receipts import dispatch, registry_record
    from tools.process_registry import process_registry

    dt, release, entered = admission
    # Restore the real executor before admitting the sentinel child.
    with monkeypatch.context() as seed:
        settled, worker, _ = dispatch(seed, session_key="settled-session")
        worker()
        event = process_registry.completion_queue.get_nowait()
        claim = ad.claim_event_delivery(event, "other-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        pending, worker, _ = dispatch(seed, session_key="pending-session")
        worker()
        process_registry.completion_queue.get_nowait()
    before = registry_record(pending)
    monkeypatch.setattr(ad, "_MAX_REGISTRY_RECORDS", 2)
    result = json.loads(dt.delegate_task(goal="cap sentinel", background=True, parent_agent=_fake_parent()))
    assert result["status"] == "dispatched"
    assert entered.wait(8)
    assert not release.is_set()
    records = json.loads(ad._registry_path().read_text(encoding="utf-8"))["records"]
    assert set(records) == {pending, result["delegation_id"]}
    assert settled not in records
    assert registry_record(pending) == before
    assert ad.enqueue_pending_outbox(current_boot_id="other-replay") == 1
    assert process_registry.completion_queue.get_nowait()["delegation_id"] == pending
    release.set()
    assert process_registry.completion_queue.get(timeout=8)["delegation_id"] == result["delegation_id"]
    assert process_registry.completion_queue.empty()


@pytest.mark.parametrize("target,label", [
    ("tools.delegate_tool._teardown", "child_0"),
    ("tools.delegation_live_log.update_manifest_statuses", "manifest"),
    ("hermes_cli.observability.shared_metrics_loop.finish_delegation_unit", "metrics"),
    ("tools.async_delegation.acknowledge_outbox_event", "outbox_ack"),
])
def test_cleanup_fault_preserves_truthful_nonstart(admission, monkeypatch, target, label):
    dt, release, entered = admission
    executor = Mock()
    executor.submit.side_effect = RuntimeError("submission refused")
    monkeypatch.setattr(ad, "_get_executor", lambda *a: executor)
    monkeypatch.setattr(target, Mock(side_effect=OSError("cleanup unavailable")))
    result = json.loads(dt.delegate_task(goal="cleanup sentinel", background=True, parent_agent=_fake_parent()))
    assert result["status"] == "rejected"
    assert result["started"] is False
    assert result["reason"] == "submission_failed"
    assert result["cleanup_pending"] == [label]
    assert not entered.is_set()
    assert not ad._records


@pytest.mark.parametrize("held", [False, True])
def test_rejection_cleanup_distinguishes_closed_from_deferred(monkeypatch, held):
    from tools import delegate_tool as dt
    from tools.delegate_tool_admission import reject_background_admission

    child = Mock()
    if held:
        assert dt._hold_run(child)
    else:
        assert dt._teardown(child, "already_closed")
    try:
        result = json.loads(reject_background_admission(
            reason="pool_capacity", children=[child], task_list=[{"goal": "test"}],
            live_deleg_id=None, live_writers=[]))
        assert result.get("cleanup_pending", []) == (["child_0"] if held else [])
        assert child.close.call_count == (0 if held else 1)
    finally:
        if held:
            dt._release_hold(child, run=True)
    assert child.close.call_count == 1
