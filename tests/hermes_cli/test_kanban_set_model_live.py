"""``hermes kanban set-model --live`` (t_033a3bb1).

A live switch moves a RUNNING worker onto the card's new provider/model/effort
at its next loop iteration without aborting it: same process, same message
list, same workspace. The CLI writes the route plus a run-scoped
``route_changed`` event in one transaction; the worker's conversation loop
polls for it and swaps in place via ``AIAgent.switch_model``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_route as kwr


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    kb.init_db()
    (home / "config.yaml").write_text(
        "providers:\n"
        "  batch-provider:\n"
        "    base_url: http://127.0.0.1:9999/v1\n"
        "    api_key: test\n",
        encoding="utf-8",
    )
    return home


@pytest.fixture(autouse=True)
def _fresh_live_state():
    kwr._live_state.update(agent=None, cursor=0)
    kwr._card_pin_cache.clear()
    yield
    kwr._live_state.update(agent=None, cursor=0)
    kwr._card_pin_cache.clear()


def _create(title: str, assignee: str = "worker") -> str:
    out = kc.run_slash(f"create '{title}' --assignee {assignee}")
    match = re.search(r"t_[a-f0-9]+", out)
    assert match, out
    return match.group(0)


def _make_running(task_id: str) -> int:
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'worker', 'running', ?)", (task_id, int(time.time())),
            )
            run_id = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=?, "
                "claim_lock='host:1', worker_pid=1 WHERE id=?", (run_id, task_id),
            )
    return run_id


def _events(task_id: str, kind: str):
    with kb.connect_closing() as conn:
        return [(r[0], json.loads(r[1] or "{}")) for r in conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (task_id, kind))]


# ---------------------------------------------------------------------------
# CLI: the write side
# ---------------------------------------------------------------------------


def test_live_on_running_card_writes_route_and_run_scoped_event(kanban_home):
    task_id = _create("live")
    run_id = _make_running(task_id)

    out = kc.run_slash(f"set-model {task_id} model-b --provider batch-provider --effort high --live")

    assert f"live: run {run_id} switches at its next turn" in out
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert (task.model_override, task.provider_override, task.reasoning_effort) == (
        "model-b", "batch-provider", "high")
    events = _events(task_id, kb.ROUTE_CHANGED_EVENT)
    assert [r for r, _ in events] == [run_id]
    assert events[0][1]["live"] is True
    assert events[0][1]["model"] == "model-b"
    assert events[0][1]["reasoning_effort"] == "high"


def test_live_on_card_that_is_not_running_is_a_plain_next_dispatch_write(kanban_home):
    task_id = _create("ready")

    out = kc.run_slash(f"set-model {task_id} model-b --provider batch-provider --live")

    assert "applies on next dispatch" in out
    assert _events(task_id, kb.ROUTE_CHANGED_EVENT) == []


def test_live_batch_receipt_names_the_live_run(kanban_home):
    live = _create("live")
    idle = _create("idle")
    run_id = _make_running(live)

    out = kc.run_slash(f"set-model {live} {idle} --effort low --live")

    assert f"{live}: effort=low applies=live(run {run_id})" in out
    assert f"{idle}: effort=low applies=next-dispatch" in out


@pytest.mark.parametrize("extra", [
    "model-b --provider batch-provider --live --reclaim",
    "none --live",
    "--clear-effort --live",
])
def test_live_refusals_write_nothing(kanban_home, extra):
    task_id = _create("refused")
    _make_running(task_id)

    out = kc.run_slash(f"set-model {task_id} {extra}")

    assert "--live" in out
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task.model_override is None and task.reasoning_effort is None
    assert task.status == "running"
    assert _events(task_id, kb.ROUTE_CHANGED_EVENT) == []


def test_reclaim_path_is_unchanged_and_writes_no_live_event(kanban_home, monkeypatch):
    task_id = _create("reclaim")
    _make_running(task_id)
    signaled = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda pid, lock, **_kw: signaled.append(pid) or {})

    out = kc.run_slash(f"set-model {task_id} model-b --provider batch-provider --reclaim")

    assert "reclaimed; redispatches now" in out
    assert signaled == [1]
    assert _events(task_id, kb.ROUTE_CHANGED_EVENT) == []


# ---------------------------------------------------------------------------
# Worker: the apply side, through the REAL conversation loop
# ---------------------------------------------------------------------------


def _tool_defs(*names):
    return [{"type": "function", "function": {
        "name": n, "description": "t", "parameters": {"type": "object", "properties": {}}}}
        for n in names]


def _response(*, content, finish_reason, tool_calls=None, model="m"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


def _tool_call(call_id):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name="web_search", arguments="{}"))


def _worker_agent():
    from run_agent import AIAgent

    with patch("model_tools.get_tool_definitions", return_value=_tool_defs("web_search")), \
            patch("run_agent.check_toolset_requirements", return_value={}), \
            patch("run_agent.OpenAI"):
        agent = AIAgent(
            api_key="key-a", base_url="https://a.example/v1", provider="custom",
            model="model-a", quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    agent._cached_system_prompt = "You are helpful.\nModel: model-a\nProvider: custom"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = {"web_search"}
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    agent.client = MagicMock(name="client-a")
    return agent


@pytest.fixture
def worker_card(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="w", assignee="worker",
                                 model_override="model-a", provider_override="custom")
    run_id = _make_running(task_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return task_id, run_id


def _set_live(task_id, **kw):
    with kb.connect_closing() as conn:
        live_runs = {}
        kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(task_id=task_id, live=True, **kw)],
                                    live_runs=live_runs)
    return live_runs


def _run(agent, tool_side_effect):
    with patch("run_agent.handle_function_call", side_effect=tool_side_effect), \
            patch.object(agent, "_persist_session"), \
            patch.object(agent, "_save_trajectory"), \
            patch.object(agent, "_cleanup_task_resources"):
        return agent.run_conversation("do the task")


def test_live_provider_switch_midrun_keeps_conversation_and_next_call_uses_new_route(
    worker_card, tmp_path,
):
    task_id, run_id = worker_card
    agent = _worker_agent()
    agent.client.chat.completions.create.side_effect = [
        _response(content="", finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
    ]
    client_b = MagicMock(name="client-b")
    client_b.chat.completions.create.side_effect = [
        _response(content="done on b", finish_reason="stop", model="model-b"),
    ]
    workspace_file = tmp_path / "ws.txt"

    def tool(*_a, **_k):
        workspace_file.write_text("written before the switch")
        _set_live(task_id, touch_model=True, model="model-b", provider="custom-b",
                  touch_effort=True, effort="high")
        return "ok"

    resolved = SimpleNamespace(success=True, new_model="model-b", target_provider="custom-b",
                               api_key="key-b", base_url="https://b.example/v1",
                               api_mode="chat_completions", error_message="")
    with patch(f"{kc.__name__.rsplit('.', 1)[0]}.model_switch.switch_model",
               return_value=resolved), \
            patch.object(agent, "_create_openai_client", return_value=client_b), \
            patch("agent.credential_pool.load_pool", return_value=None):
        result = _run(agent, tool)

    assert result["final_response"] == "done on b", (
        result["final_response"], _events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT),
        _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT))
    # Same agent, same process: the first call went to A, the second to B.
    assert agent.client is client_b
    assert client_b.chat.completions.create.call_count == 1
    sent = client_b.chat.completions.create.call_args.kwargs
    assert sent["model"] == "model-b"
    # Conversation kept: the request to B carries the whole history
    # (system, user, assistant tool call, tool result).
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert sent["messages"][-1]["tool_call_id"] == "c1"
    assert "Model: model-b" in sent["messages"][0]["content"]
    assert "Provider: custom-b" in sent["messages"][0]["content"]
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert workspace_file.read_text() == "written before the switch"
    # Board ledger: one route_switched on THIS run, at iteration 2.
    switched = _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)
    assert [r for r, _ in switched] == [run_id]
    payload = switched[0][1]
    assert payload["iteration"] == 2
    assert payload["kind"] == "route"
    assert payload["from"] == {"provider": "custom", "model": "model-a", "effort": "medium"}
    assert payload["to"] == {"provider": "custom-b", "model": "model-b", "effort": "high"}
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert agent._primary_runtime["provider"] == "custom-b"


def test_live_effort_only_switch_changes_only_the_request_effort(worker_card):
    task_id, _run_id = worker_card
    agent = _worker_agent()
    client_a = agent.client
    client_a.chat.completions.create.side_effect = [
        _response(content="", finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
        _response(content="done", finish_reason="stop"),
    ]

    def tool(*_a, **_k):
        _set_live(task_id, touch_effort=True, effort="low")
        return "ok"

    with patch.object(agent, "switch_model") as switch:
        result = _run(agent, tool)

    assert result["final_response"] == "done"
    switch.assert_not_called()  # no client rebuild, no cache-busting swap
    assert agent.client is client_a and agent.model == "model-a" and agent.provider == "custom"
    first, second = (c.kwargs for c in client_a.chat.completions.create.call_args_list)
    assert first["model"] == second["model"] == "model-a"
    assert first["messages"][0] == second["messages"][0]  # system prompt byte-identical

    def _strip(kwargs):
        return {k: v for k, v in kwargs.items() if k not in {"messages", "extra_body", "reasoning_effort"}}

    assert _strip(first) == _strip(second)
    assert json.dumps(first, default=str).count("medium") >= 1
    assert "low" in json.dumps(second.get("extra_body") or second.get("reasoning_effort"), default=str)
    ev = _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)
    assert len(ev) == 1 and ev[0][1]["kind"] == "effort" and ev[0][1]["to"]["effort"] == "low"


def test_live_flagship_route_without_override_comment_is_refused(worker_card, monkeypatch):
    task_id, run_id = worker_card
    agent = _worker_agent()
    agent.client.chat.completions.create.side_effect = [
        _response(content="", finish_reason="tool_calls", tool_calls=[_tool_call("c1")]),
        _response(content="done", finish_reason="stop"),
    ]
    from hermes_cli import model_policy

    monkeypatch.setattr(model_policy, "flagship_model_match",
                        lambda model, config=None: "flag" if "flag" in (model or "") else None)

    def tool(*_a, **_k):
        # Written straight to the row, as if the audit comment were deleted.
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET model_override='flag-model' WHERE id=?", (task_id,))
                kb._append_event(conn, task_id, kb.ROUTE_CHANGED_EVENT, {"live": True}, run_id=run_id)
        return "ok"

    with patch.object(agent, "switch_model") as switch:
        result = _run(agent, tool)

    assert result["final_response"] == "done"
    switch.assert_not_called()
    assert agent.model == "model-a"
    refused = _events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT)
    assert [r for r, _ in refused] == [run_id]
    assert "flagship" in refused[0][1]["reason"]


def test_live_event_for_another_run_is_ignored(worker_card):
    task_id, run_id = worker_card
    agent = _worker_agent()
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET reasoning_effort='low' WHERE id=?", (task_id,))
            kb._append_event(conn, task_id, kb.ROUTE_CHANGED_EVENT, {"live": True}, run_id=run_id - 1 if run_id > 1 else 999)

    assert kwr.apply_pending_live_route(agent, iteration=1, active_system_prompt="sp") == "sp"
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}


def test_live_poll_is_inert_outside_a_kanban_worker(kanban_home):
    agent = _worker_agent()
    with patch.object(kb, "connect") as connect:
        assert kwr.apply_pending_live_route(agent, iteration=1, active_system_prompt="sp") == "sp"
    connect.assert_not_called()


def test_live_switch_only_follows_the_first_top_level_agent(worker_card):
    task_id, _ = worker_card
    worker = _worker_agent()
    kwr.apply_pending_live_route(worker, iteration=1)  # binds the worker
    helper = _worker_agent()
    _set_live(task_id, touch_effort=True, effort="low")

    kwr.apply_pending_live_route(helper, iteration=1)
    assert helper.reasoning_config == {"enabled": True, "effort": "medium"}
    kwr.apply_pending_live_route(worker, iteration=2)
    assert worker.reasoning_config == {"enabled": True, "effort": "low"}


# ---------------------------------------------------------------------------
# FleetReview #1331 follow-ups (t_14dcd770)
# ---------------------------------------------------------------------------


def test_live_event_survives_a_transient_card_read_failure(worker_card):
    """A failed read must not advance the cursor past the live event."""
    task_id, _ = worker_card
    agent = _worker_agent()
    _set_live(task_id, touch_effort=True, effort="low")
    kwr.card_pinned_route()  # the poll's pin snapshot; the flaky read is the poll's own
    real_get_task = kb.get_task
    calls = []

    def flaky(conn, tid):
        calls.append(tid)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_get_task(conn, tid)

    with patch.object(kb, "get_task", side_effect=flaky):
        kwr.apply_pending_live_route(agent, iteration=1)
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
        kwr.apply_pending_live_route(agent, iteration=2)
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert len(_events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)) == 1


def test_repeating_a_refused_live_request_retries_the_switch(worker_card):
    """Same set-model --live twice: the second appends a new event and switches."""
    task_id, _ = worker_card
    agent = _worker_agent()
    refused = SimpleNamespace(success=False, error_message="no credentials for custom-b")
    resolved = SimpleNamespace(success=True, new_model="model-b", target_provider="custom-b",
                               api_key="key-b", base_url="https://b.example/v1",
                               api_mode="chat_completions", error_message="")
    kw = dict(touch_model=True, model="model-b", provider="custom-b")
    target = f"{kc.__name__.rsplit('.', 1)[0]}.model_switch.switch_model"
    _set_live(task_id, **kw)
    with patch(target, return_value=refused), patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=1)
    switch.assert_not_called()
    assert len(_events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT)) == 1

    _set_live(task_id, **kw)  # identical route: still a fresh run-scoped event
    assert len(_events(task_id, kb.ROUTE_CHANGED_EVENT)) == 2
    with patch(target, return_value=resolved), patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=2)
    switch.assert_called_once()
    assert switch.call_args.kwargs["new_model"] == "model-b"


def test_effort_only_live_event_does_not_activate_a_next_dispatch_model(worker_card):
    task_id, _ = worker_card
    agent = _worker_agent()
    with kb.connect_closing() as conn:  # next-dispatch model write (no --live)
        kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
            task_id=task_id, touch_model=True, model="model-b", provider="custom-b")])
    assert _events(task_id, kb.ROUTE_CHANGED_EVENT) == []
    _set_live(task_id, touch_effort=True, effort="high")

    with patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=1)

    switch.assert_not_called()
    assert (agent.model, agent.provider) == ("model-a", "custom")
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    ev = _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)
    assert len(ev) == 1 and ev[0][1]["kind"] == "effort"


def test_pending_live_events_coalesce_to_their_own_values(worker_card):
    """live model-b, then next-dispatch model-c, then live effort: run gets b + high."""
    task_id, _ = worker_card
    agent = _worker_agent()
    _set_live(task_id, touch_model=True, model="model-b", provider="custom-b")
    with kb.connect_closing() as conn:
        kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
            task_id=task_id, touch_model=True, model="model-c", provider="custom-c")])
    _set_live(task_id, touch_effort=True, effort="high")
    resolved = SimpleNamespace(success=True, new_model="model-b", target_provider="custom-b",
                               api_key="key-b", base_url="https://b.example/v1",
                               api_mode="chat_completions", error_message="")
    with patch(f"{kc.__name__.rsplit('.', 1)[0]}.model_switch.switch_model",
               return_value=resolved) as resolve, \
            patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=1)

    assert resolve.call_args.kwargs["raw_input"] == "model-b"
    assert resolve.call_args.kwargs["explicit_provider"] == "custom-b"
    assert switch.call_args.kwargs["session_reasoning_config"] == {"enabled": True, "effort": "high"}


def test_app_server_worker_marks_run_and_live_write_is_not_promised(worker_card):
    task_id, run_id = worker_card
    agent = _worker_agent()
    _set_live(task_id, touch_effort=True, effort="low")  # pending before the turn
    agent.api_mode = "codex_app_server"
    with patch.object(agent, "_run_codex_app_server_turn",
                      return_value={"final_response": "x", "messages": []}) as turn:
        agent.run_conversation("do the task")
    turn.assert_called_once()
    assert [r for r, _ in _events(task_id, kb.ROUTE_LIVE_UNSUPPORTED_EVENT)] == [run_id]
    refused = _events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT)
    assert [r for r, _ in refused] == [run_id]
    assert "codex_app_server" in refused[0][1]["reason"]

    out = kc.run_slash(f"set-model {task_id} --effort high --live")
    assert "applies on next dispatch" in out
    assert "switches at its next turn" not in out
    assert len(_events(task_id, kb.ROUTE_CHANGED_EVENT)) == 1  # only the pre-turn one


# ---------------------------------------------------------------------------
# FleetReview #1342 follow-ups (t_c2214f98)
# ---------------------------------------------------------------------------


def test_gated_live_model_does_not_drop_a_later_live_effort(worker_card, monkeypatch):
    """A model refused by a gate is refused alone; the effort write still applies."""
    task_id, run_id = worker_card
    agent = _worker_agent()
    from hermes_cli import model_policy

    monkeypatch.setattr(model_policy, "flagship_model_match",
                        lambda model, config=None: "flag" if "flag" in (model or "") else None)
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET model_override='flag-model' WHERE id=?", (task_id,))
            kb._append_event(conn, task_id, kb.ROUTE_CHANGED_EVENT, {
                "live": True, "touch_model": True, "touch_effort": False,
                "model": "flag-model", "provider": "custom"}, run_id=run_id)
    _set_live(task_id, touch_effort=True, effort="low")

    with patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=1)

    switch.assert_not_called()
    assert agent.model == "model-a"
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    refused = _events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT)
    assert len(refused) == 1 and "flagship" in refused[0][1]["reason"]
    assert refused[0][1]["to"]["effort"] is None
    switched = _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)
    assert len(switched) == 1 and switched[0][1]["kind"] == "effort"


def test_unresolvable_live_model_does_not_drop_a_live_effort(worker_card):
    task_id, _ = worker_card
    agent = _worker_agent()
    _set_live(task_id, touch_model=True, model="model-b", provider="custom-b")
    _set_live(task_id, touch_effort=True, effort="high")
    refused = SimpleNamespace(success=False, error_message="no credentials for custom-b")
    with patch(f"{kc.__name__.rsplit('.', 1)[0]}.model_switch.switch_model", return_value=refused), \
            patch.object(agent, "switch_model") as switch:
        kwr.apply_pending_live_route(agent, iteration=1)

    switch.assert_not_called()
    assert (agent.model, agent.provider) == ("model-a", "custom")
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    ev = _events(task_id, kwr.LIVE_ROUTE_SWITCH_REFUSED_EVENT)
    assert len(ev) == 1 and "no credentials" in ev[0][1]["reason"]
    ev = _events(task_id, kwr.LIVE_ROUTE_SWITCHED_EVENT)
    assert len(ev) == 1 and ev[0][1]["kind"] == "effort"


def test_app_server_marker_write_failure_is_retried_until_it_lands(worker_card, monkeypatch):
    task_id, run_id = worker_card
    agent = _worker_agent()
    monkeypatch.setattr(kwr, "_MARKER_RETRY_DELAYS", (0.0,))
    kwr._live_state["marker_retry"] = None
    real_connect = kb.connect
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("database is locked")
        return real_connect()

    try:
        with patch.object(kb, "connect", side_effect=flaky):
            kwr.mark_live_route_unsupported(agent, reason="codex_app_server: no switch point")
            thread = kwr._live_state["marker_retry"]
            assert thread is not None
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(calls) == 3
        assert [r for r, _ in _events(task_id, kb.ROUTE_LIVE_UNSUPPORTED_EVENT)] == [run_id]
        out = kc.run_slash(f"set-model {task_id} --effort high --live")
        assert "applies on next dispatch" in out
        assert _events(task_id, kb.ROUTE_CHANGED_EVENT) == []
    finally:
        kwr._live_state["marker_retry"] = None
