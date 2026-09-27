"""``kanban create --reasoning`` / ``set-model --effort|--clear-effort|--reclaim``.

The DB layer (``set_reasoning_effort``) and the dispatcher's ``--reasoning``
spawn flag already exist; these tests pin the CLI surface that drives them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _new_task(**kw) -> str:
    with kbc.connect() as conn:
        return kb.create_task(conn, title="t", assignee="worker", **kw)


def _task(tid: str):
    with kbc.connect() as conn:
        return kb.get_task(conn, tid)


def test_create_reasoning_is_stored_and_shown(kanban_home):
    kc.run_slash('create "t" --assignee worker --reasoning high')
    with kbc.connect() as conn:
        tid = kb.list_tasks(conn)[0].id
    assert _task(tid).reasoning_effort == "high"
    assert "effort:" in kc.run_slash(f"show {tid}")
    payload = json.loads(kc.run_slash(f"show {tid} --json"))
    assert payload["task"]["reasoning_effort"] == "high"


def test_create_rejects_invalid_reasoning(kanban_home):
    out = kc.run_slash('create "t" --reasoning bogus')
    assert "reasoning_effort must be one of" in out
    with kbc.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_effort_alone_leaves_model_override_untouched(kanban_home):
    tid = _new_task(model_override="m1", provider_override="p1")
    kc.run_slash(f"set-model {tid} --effort xhigh")
    t = _task(tid)
    assert (t.model_override, t.provider_override, t.reasoning_effort) == ("m1", "p1", "xhigh")


def test_clearing_model_never_resets_effort(kanban_home):
    tid = _new_task(model_override="m1", reasoning_effort="low")
    kc.run_slash(f"set-model {tid} none")
    t = _task(tid)
    assert t.model_override is None and t.reasoning_effort == "low"


def test_clear_effort_and_none_is_a_real_level(kanban_home):
    tid = _new_task(reasoning_effort="high")
    kc.run_slash(f"set-model {tid} --effort none")
    assert _task(tid).reasoning_effort == "none"
    kc.run_slash(f"set-model {tid} --clear-effort")
    assert _task(tid).reasoning_effort is None


def test_model_provider_and_effort_in_one_call(kanban_home):
    tid = _new_task()
    kc.run_slash(f"set-model {tid} m2 --provider p2 --effort medium")
    t = _task(tid)
    assert (t.model_override, t.provider_override, t.reasoning_effort) == ("m2", "p2", "medium")


def test_effort_and_clear_effort_are_exclusive(kanban_home):
    tid = _new_task(reasoning_effort="high")
    out = kc.run_slash(f"set-model {tid} --effort low --clear-effort")
    assert "mutually exclusive" in out
    assert _task(tid).reasoning_effort == "high"


def test_reclaim_releases_running_task_onto_new_route(kanban_home):
    tid = _new_task()
    with kbc.connect() as conn:
        assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert _task(tid).status == "running"
    out = kc.run_slash(f"set-model {tid} m2 --provider p2 --reclaim")
    t = _task(tid)
    assert t.status != "running" and t.claim_lock is None
    assert t.model_override == "m2"
    assert "reclaimed" in out


def test_without_reclaim_running_task_keeps_its_claim(kanban_home):
    tid = _new_task()
    with kbc.connect() as conn:
        assert kb.claim_task(conn, tid, claimer="worker") is not None
    kc.run_slash(f"set-model {tid} m2 --provider p2")
    t = _task(tid)
    assert t.status == "running" and t.claim_lock is not None
    assert t.model_override == "m2"


def test_reclaim_on_idle_task_is_a_noop(kanban_home):
    tid = _new_task()
    out = kc.run_slash(f"set-model {tid} m2 --reclaim")
    assert _task(tid).status == "ready"
    assert "applies on next dispatch" in out
