"""Signing grants on BACKGROUND delegations (t_4548202f): the grant belongs to the batch, not the parent turn.

Minted at spawn, closed when its child completes (tests/tools/test_delegate_sign_grant_close.py), revoked on every
other batch exit (error, cancel, refused admission), never inherited by a
relaunched generation, and swept at gateway start when the process that owned the batch is gone. Drives the real
delegate_task background dispatch (in-gateway thread path) over the real board; only the child build/run seams are
patched. The foreground (turn-bound) contract stays in tests/tools/test_delegate_sign_grant.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import run_agent  # noqa: F401  Bootstrap recovery before the per-test home-I/O guard.

from gateway.session_context import set_session_vars
from tools.process_registry import process_registry
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sign_grants as ksg
from tools import delegate_tool_sign_grant as dsg

from tests.tools.test_delegate_sign_grant import MODEL, board  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_queue_and_context(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    _drain_all()
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    os.environ.pop("HERMES_SESSION_ID", None)
    _drain_all()


def _drain_all():
    while not process_registry.completion_queue.empty():
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            break


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "parent-sess"
    parent.model = parent._dispatched_model = MODEL
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.api_mode = "chat_completions"
    parent.quiet_mode = True
    parent._oneshot_children_spawned = 0
    parent.__dict__.pop("_sign_grants", None)
    return parent


def _patch(monkeypatch, body, *, detached=False):
    import tools.delegate_tool as dt

    child = MagicMock()
    child.model = MODEL
    child.session_id = "child-sid"
    child._subagent_id = "sa-0-bg"
    child._context_inherited = "summary"
    child._delegate_role = "leaf"
    child.provider = "openrouter"
    child.custom_provider = None
    child.base_url = "https://parent.invalid/v1"
    child.api_mode = "chat_completions"
    child.acp_command = child.acp_args = None
    child.enabled_toolsets = ["file"]
    child.reasoning_config = child.fallback_model = child.service_tier = None
    child.providers_allowed = child.providers_ignored = child.providers_order = child.provider_sort = None
    child.provider_require_parameters = False
    child.provider_data_collection = child.openrouter_min_coding_score = None
    child.prefill_messages = None
    child._inherit_mode = "summary"
    child._boomerang_route_source = "delegation-default"
    child._boomerang_parent_route = {"source": "parent", "provider": "openrouter", "model": MODEL,
                                     "base_url": child.base_url, "api_mode": "chat_completions",
                                     "fallback_chain": [], "request_overrides": {}}
    child.requested_provider = "openrouter"
    child.request_overrides = {}
    child._fallback_chain = []
    child.terminal_cwd = "/tmp"
    child.__dict__.pop("_held_sign_grants", None)
    creds = {"model": MODEL, "provider": None, "base_url": None, "api_key": None, "api_mode": None,
             "command": None, "args": None}
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: child)
    monkeypatch.setattr(dt, "_run_single_child", body)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(dt, "_get_detached_background", lambda: detached)
    set_session_vars(platform="tui", chat_id="chat-1", session_key="parent-sess", session_id="parent-sess",
                     async_delivery=True)
    return dt, child


def _task(card):
    return {"goal": "review it", "inherit_context": True,
            "sign_grant": {"cards": [card], "verbs": ["verdict", "approve"], "expires_in_min": 30}}


def _grant_rows():
    with kb.connect_closing() as conn:
        return [dict(r) for r in conn.execute("SELECT grant_id, revoked_at FROM sign_grants")]


def _promoted(card):
    with kb.connect_closing() as conn:
        return ksg.operator_rows_with_live_grants(conn, card)


def _owner_file():
    return dsg._own_path()[0]


def _ok(task_index, goal, *_a, **_kw):
    return {"task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1,
            "duration_seconds": 0.1, "model": MODEL, "exit_reason": "completed"}


def _granted_write(card, gid):
    from agent.delegation_context import delegated_child_context

    with delegated_child_context("child-sid"), kb.connect_closing() as conn:
        return kb.add_comment(conn, card, "apollo", "review note", session_ref=kb.derive_session_ref("child-sid"),
                              grant_id=gid)


def test_background_grant_is_minted_live_through_the_batch_and_closed_when_it_ends(board, monkeypatch):
    seen = {}
    release = threading.Event()

    def body(task_index, goal, child=None, parent_agent=None, **kw):
        seen["live"] = [r for r in _grant_rows() if r["revoked_at"] is None]
        seen["goal"] = goal
        seen["owner"] = json.loads(_owner_file().read_text(encoding="utf-8-sig"))
        release.wait(10)
        return _ok(task_index, goal)

    dt, _child = _patch(monkeypatch, body)
    parent = _parent()
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=parent))
    assert out["status"] == "dispatched", out
    # The parent turn ends here: the grant is NOT the turn's, so its end revokes nothing.
    assert "_sign_grants" not in parent.__dict__
    dsg.revoke_turn_grants(parent)
    assert [r["revoked_at"] for r in _grant_rows()] == [None]
    release.set()
    evt = process_registry.completion_queue.get(timeout=20)
    assert evt["status"] == "completed", evt
    assert len(seen["live"]) == 1
    gid = seen["live"][0]["grant_id"]
    assert f"signing grant {gid}" in seen["goal"] and "ends with this delegation batch" in seen["goal"]
    assert [g["grant_id"] for g in seen["owner"]["grants"]] == [gid]
    assert seen["owner"]["pid"] == os.getpid() and seen["owner"]["grants"][0]["delegation_id"] == out["delegation_id"]
    # Batch over, child completed: closed (not revoked), owner record gone, and the child's late stamp is refused.
    assert _grant_rows()[0]["revoked_at"] is None
    assert not _owner_file().exists()
    with pytest.raises(ksg.GrantRefused, match="is closed"):
        _granted_write(board, gid)


def test_a_batch_that_raises_still_revokes_its_grant(board, monkeypatch):
    def body(task_index, goal, *a, **kw):
        raise RuntimeError("child blew up")

    dt, _child = _patch(monkeypatch, body)
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert out["status"] == "dispatched", out
    process_registry.completion_queue.get(timeout=20)
    rows = _grant_rows()
    assert len(rows) == 1 and rows[0]["revoked_at"] is not None
    assert not _owner_file().exists()
    assert _promoted(board) == []


def test_cancelling_the_batch_revokes_its_grant_before_the_child_returns(board, monkeypatch):
    from tools import async_delegation

    started, release = threading.Event(), threading.Event()

    def body(task_index, goal, *a, **kw):
        started.set()
        release.wait(10)
        return _ok(task_index, goal)

    dt, _child = _patch(monkeypatch, body)
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert out["status"] == "dispatched", out
    assert started.wait(10)
    assert async_delegation.interrupt_for_session(session_key="parent-sess", reason="test_stop") >= 1
    # The wedged child has not returned, yet the grant is already void and out of the owner record.
    assert _grant_rows()[0]["revoked_at"] is not None
    assert not _owner_file().exists()
    release.set()


def test_a_refused_admission_revokes_the_grant(board, monkeypatch):
    import tools.async_delegation as ad

    dt, _child = _patch(monkeypatch, _ok)
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch",
                        lambda **kw: {"status": "rejected", "reason": "test_full"})
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert out.get("status") != "dispatched", out
    rows = _grant_rows()
    assert len(rows) == 1 and rows[0]["revoked_at"] is not None
    assert not _owner_file().exists()


def test_an_unwritable_owner_record_refuses_the_background_grant(board, monkeypatch):
    dt, _child = _patch(monkeypatch, _ok)
    monkeypatch.setattr(dsg, "_write_owner", lambda path, data: (_ for _ in ()).throw(OSError("read-only")))
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert "owner record could not be written" in out.get("error", ""), out
    assert [r["revoked_at"] is not None for r in _grant_rows()] == [True]


def test_a_granted_batch_stays_in_process_and_its_restart_intent_carries_no_grant(board, monkeypatch):
    """Detached on: a granted unit still runs here (its grant is bound to the child built here), and the persisted
    restart intent has neither the grant nor the brief, so a relaunched generation holds nothing."""
    ran = threading.Event()
    specs = []

    def body(task_index, goal, *a, **kw):
        ran.set()
        return _ok(task_index, goal)

    dt, _child = _patch(monkeypatch, body, detached=True)
    real_spec = dt._build_durable_background_spec

    def spy(**kw):
        out = real_spec(**kw)
        specs.append(out[0])
        return out

    monkeypatch.setattr(dt, "_build_durable_background_spec", spy)
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert out["status"] == "dispatched" and "detached" not in out, out
    assert ran.wait(10)
    process_registry.completion_queue.get(timeout=20)
    assert specs and specs[0] is not None, "test needs a durable restart intent to inspect"
    tasks = specs[0]["source"]["tasks"]
    assert tasks[0]["goal"] == "review it"
    assert "sign_grant" not in tasks[0] and dsg.GOAL_BEFORE_GRANT not in tasks[0]


def test_a_background_call_that_runs_synchronously_keeps_the_turn_bound_grant(board, monkeypatch):
    """Finite session: background=true joins inline, so the grant stays the parent TURN's (the parent may gate on the
    child's stamp in this turn) and is never batch-tracked (Prism f9c6de2f594f)."""
    seen = {}

    def body(task_index, goal, *a, **kw):
        seen["goal"] = goal
        return _ok(task_index, goal)

    dt, _child = _patch(monkeypatch, body)
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    parent = _parent()
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=parent))
    assert out["results"][0]["status"] == "completed", out
    assert "your parent's turn" in seen["goal"]
    assert [r["revoked_at"] for r in _grant_rows()] == [None]  # live after the batch: the turn owns it
    assert not _owner_file().exists()
    dsg.revoke_turn_grants(parent)
    assert _grant_rows()[0]["revoked_at"] is not None


def test_a_relaunched_generation_mints_no_grant(board, monkeypatch):
    """Recovery re-enters delegate_task with the persisted tasks; even a task that still names a grant mints none."""
    import tools.delegate_tool as dt

    child = SimpleNamespace(model=MODEL, session_id="child-sid-2", _subagent_id="sa-1", _context_inherited="summary")
    seen = {}

    def body(task_index, goal, *a, **kw):
        seen["goal"] = goal
        return _ok(task_index, goal)

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: child)
    monkeypatch.setattr(dt, "_run_single_child", body)
    out = json.loads(dt.delegate_task(tasks=[dict(_task(board), route_source="delegation-default",
                                                  **{dsg.GOAL_BEFORE_GRANT: "review it"})],
                                      sign_grant={"cards": [board], "verbs": ["approve"]},
                                      parent_agent=_parent(), _recovery_spec={"execution": {}}))
    assert out.get("results", [{}])[0].get("status") == "completed", out
    assert _grant_rows() == []
    assert "signing grant" not in seen["goal"]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_gateway_start_sweep_revokes_grants_of_a_dead_owner_only(board, monkeypatch):
    parent = _parent()
    dead, live = [], []
    for target, sid in ((dead, "dead-child"), (live, "live-child")):
        child = SimpleNamespace(model=MODEL, session_id=sid, _subagent_id="sa-x", _context_inherited="summary")
        g = dsg.mint_for_child(_task(board), child, parent, batch_bound=True)
        g["board"] = g.get("board") or "default"
        target.append(g)
    root = dsg._batch_dir()
    root.mkdir(parents=True, exist_ok=True)
    pid = _dead_pid()
    dead_file = root / f"{pid}-1.json"
    dead_file.write_text(json.dumps({"pid": pid, "created": 1.0, "grants": [
        {"grant_id": dead[0]["grant_id"], "board": dead[0]["board"], "delegation_id": "d-dead"}]}), encoding="utf-8")
    # A previous process that had THIS pid (pid reuse, or the same pid after a host reboot): another creation
    # time, so another file, never shared with ours (Prism 88dc83b1800a, f22d85a50ff7, c711413b466e).
    prior = SimpleNamespace(model=MODEL, session_id="prior-child", _subagent_id="sa-p", _context_inherited="summary")
    gp = dsg.mint_for_child(_task(board), prior, parent, batch_bound=True)
    gp["board"] = gp.get("board") or "default"
    prior_file = root / f"{os.getpid()}-1.json"
    prior_file.write_text(json.dumps({"pid": os.getpid(), "created": 1.0, "grants": [
        {"grant_id": gp["grant_id"], "board": gp["board"], "delegation_id": "d-prior"}]}), encoding="utf-8")
    dsg.track_batch_grants(live, delegation_id="d-live")  # this live process owns it

    from gateway.run import GatewayRunner

    runner = SimpleNamespace(_current_boot_id=lambda: "boot-x", config=SimpleNamespace(multiplex_profiles=False))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda multiplex=False: [])
    GatewayRunner._recover_async_delegations_once(runner)

    state = {r["grant_id"]: r["revoked_at"] for r in _grant_rows()}
    assert state[dead[0]["grant_id"]] is not None
    assert state[gp["grant_id"]] is not None
    assert state[live[0]["grant_id"]] is None
    assert not dead_file.exists() and not prior_file.exists()
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT closed_at FROM sign_grants WHERE grant_id = ?",
                            (dead[0]["grant_id"],)).fetchone()[0] is None  # swept = revoked, never closed
    assert _owner_file().exists()
    dsg.revoke_batch_grants(live)
    assert not _owner_file().exists()


def test_a_pid_reused_within_one_second_names_another_owner_file(board, monkeypatch):
    """Prism a6eae8ca8e9c: the owner file carries the creation time to the millisecond."""
    paths = []
    for created in (1700000000.1, 1700000000.9):
        monkeypatch.setattr(dsg, "_process_created", lambda pid, _c=created: _c)
        paths.append(dsg._own_path()[0])
    assert paths[0] != paths[1]
