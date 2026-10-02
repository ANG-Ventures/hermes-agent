"""Per-card harness brain (t_a8f335c5): ``tasks.brain`` + ``set-model/create --brain``.

The lane runner (hermes-home skills-shared/coding/kanban-foreign-lane) reads
``tasks.brain`` and resolves card > lane > profile ``foreign_lane.brain``; this
file covers the fork half: the column, the allowlist, the audit event and the
CLI surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _created_id(out: str) -> str:
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, f"no task id in output: {out!r}"
    return m.group(1)


def _brain_events(tid: str) -> list:
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT payload, actor_profile FROM task_events WHERE task_id = ? AND kind = 'brain_set' ORDER BY id",
            (tid,),
        ).fetchall()
    return [json.loads(r["payload"] or "{}") for r in rows]


@pytest.mark.parametrize("given,stored", [
    ("clrf", "clrf"), ("cpr-cli", "clrf"), ("CLRF", "clrf"),
    ("clxf-25", "clxf-25"), ("clxf:25", "clxf-25"), ("cpx-cli:25", "clxf-25"),
    ("dtlrf", "dtlrf"), ("dtlr", "dtlrf"), ("dtlxf-3", "dtlxf-3"), ("dtlx:3", "dtlxf-3"),
    ("alrf", "alrf"),
    ("cliproxy:gpt-6-astra", "cliproxy:gpt-6-astra"),
    ("openrouter:anthropic/claude-sonnet-5-5", "openrouter:anthropic/claude-sonnet-5-5"),
    ("", None), (None, None), ("none", None),
])
def test_allowlist_normalizes(given, stored):
    assert kb.normalize_card_brain(given) == stored


@pytest.mark.parametrize("bad,needle", [
    ("clr", "use clrf"), ("clx-16", "use clxf-16"),
    ("bogus", "brain must be one of"), ("clxf-x", "brain must be one of"),
    ("cliproxy:", "brain must be one of"), ("apr-cli", "brain must be one of"),
])
def test_allowlist_refuses(bad, needle):
    with pytest.raises(ValueError, match=re.escape(needle)):
        kb.normalize_card_brain(bad)


def test_migration_adds_nullable_column(kanban_home):
    with kb.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        tid = kb.create_task(conn, title="plain", assignee="cc-worker")
        assert kb.get_task(conn, tid).brain is None
    assert "brain" in cols
    kb.init_db()  # idempotent


def test_create_brain_persists_and_is_on_created_event(kanban_home):
    out = kc.run_slash("create 'x' --assignee cc-worker --brain clxf:25")
    tid = _created_id(out)
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).brain == "clxf-25"
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'", (tid,),
        ).fetchone()["payload"])
    assert payload["brain"] == "clxf-25"


def test_create_refuses_bad_brain_without_creating(kanban_home):
    out = kc.run_slash("create 'x' --assignee cc-worker --brain clr")
    assert "use clrf" in out
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_set_model_brain_with_model_and_effort(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker"))
    res = kc.run_slash(f"set-model {tid} claude-sonnet-5-5 --effort high --brain clrf")
    assert "Set brain on" in res and "clrf" in res
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert (t.model_override, t.reasoning_effort, t.brain) == ("claude-sonnet-5-5", "high", "clrf")
    assert _brain_events(tid) == [{"brain": "clrf"}]


def test_brain_alone_leaves_model_untouched(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker --model claude-sonnet-5-5"))
    res = kc.run_slash(f"set-model {tid} --brain dtlrf")
    assert "model override" not in res
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert t.model_override == "claude-sonnet-5-5"
    assert t.brain == "dtlrf"


def test_clear_brain(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker --brain clrf"))
    res = kc.run_slash(f"set-model {tid} --clear-brain")
    assert "Cleared brain" in res
    with kb.connect() as conn:
        raw = conn.execute("SELECT brain FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert raw["brain"] is None
    assert _brain_events(tid) == [{"brain": None}]


def test_clearing_model_keeps_brain(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker --model claude-sonnet-5-5 --brain clrf"))
    kc.run_slash(f"set-model {tid} none")
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert t.model_override is None and t.brain == "clrf"


def test_set_model_refuses_bad_brain_writes_nothing(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker"))
    res = kc.run_slash(f"set-model {tid} claude-sonnet-5-5 --brain bogus")
    assert "brain must be one of" in res
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert t.model_override is None and t.brain is None
    assert _brain_events(tid) == []


def test_brain_and_clear_brain_exclusive(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker --brain clrf"))
    res = kc.run_slash(f"set-model {tid} --brain dtlrf --clear-brain")
    assert "usage error" in res or "not allowed with" in res
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).brain == "clrf"


def test_live_refuses_brain(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker"))
    res = kc.run_slash(f"set-model {tid} claude-sonnet-5-5 --brain clrf --live")
    assert "next dispatch" in res
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).brain is None


def test_set_card_brain_refuses_archived(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="cc-worker")
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
        conn.commit()
        with pytest.raises(RuntimeError, match="archived"):
            kb.set_card_brain(conn, tid, "clrf")


def test_show_json_carries_brain(kanban_home):
    tid = _created_id(kc.run_slash("create 'x' --assignee cc-worker --brain alrf"))
    out = kc.run_slash(f"show {tid} --json")
    assert json.loads(out)["task"]["brain"] == "alrf"
