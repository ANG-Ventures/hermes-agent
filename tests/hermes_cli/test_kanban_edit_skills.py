"""`kanban edit --skill/--clear-skills` + sdlc-review auto-attach on
`[milestone] QA` create (t_c9af70b6).

kanban-review-lane-lint's remediation hint names `kanban edit --skill
sdlc-review`; before this change `edit` had no `--skill` flag, so the only fix
for a live QA card was re-minting it.
"""

from __future__ import annotations

import argparse
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
    m = re.search(r"Created (t_[0-9a-f]+)", out)
    assert m, out
    return m.group(1)


def _skills(tid: str) -> list[str]:
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    return list(task.skills or [])


def _events(tid: str, kind: str) -> list[dict]:
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, kind),
        ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


# -- 1. edit --skill / --clear-skills ----------------------------------------

def test_edit_skill_appends_dedupes_and_audits(kanban_home):
    tid = _created_id(kc.run_slash("create 'plain' --assignee alice --skill a"))
    out = kc.run_slash(f"edit {tid} --skill b --skill a --skill c")
    assert f"{tid}: skills: a, b, c" in out
    assert _skills(tid) == ["a", "b", "c"]
    ev = _events(tid, "skills_set")
    assert len(ev) == 1
    assert ev[0]["before"] == ["a"] and ev[0]["after"] == ["a", "b", "c"]


def test_edit_skill_noop_writes_no_event(kanban_home):
    tid = _created_id(kc.run_slash("create 'plain' --assignee alice --skill a"))
    kc.run_slash(f"edit {tid} --skill a")
    assert _events(tid, "skills_set") == []


def test_edit_clear_skills_then_add(kanban_home):
    tid = _created_id(kc.run_slash("create 'plain' --assignee alice --skill a --skill b"))
    kc.run_slash(f"edit {tid} --clear-skills --skill c")
    assert _skills(tid) == ["c"]
    kc.run_slash(f"edit {tid} --clear-skills")
    assert _skills(tid) == []
    with kb.connect() as conn:
        raw = conn.execute("SELECT skills FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert raw is None


def test_edit_skill_refuses_toolset_name_and_comma(kanban_home):
    tid = _created_id(kc.run_slash("create 'plain' --assignee alice"))
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="toolset"):
            kb.set_task_skills(conn, tid, add=["terminal"])
        with pytest.raises(ValueError, match="comma"):
            kb.set_task_skills(conn, tid, add=["a,b"])
    assert _skills(tid) == []


def test_edit_skill_unknown_id(kanban_home):
    with kb.connect() as conn:
        assert kb.set_task_skills(conn, "t_00000000", add=["a"]) is None


# -- 2. create auto-attaches sdlc-review to [milestone] QA --------------------

def test_create_milestone_qa_auto_adds_sdlc_review(kanban_home):
    out = kc.run_slash("create '[milestone] QA: thing' --assignee argus")
    tid = _created_id(out)
    assert "Added skill sdlc-review" in out
    assert _skills(tid) == ["sdlc-review"]
    assert _events(tid, "created")[0]["skills_auto_added"] == ["sdlc-review"]


def test_create_milestone_qa_keeps_given_skills(kanban_home):
    out = kc.run_slash("create '[Milestone] qa x' --assignee argus --skill qa")
    assert _skills(_created_id(out)) == ["qa", "sdlc-review"]


def test_create_milestone_qa_explicit_skill_not_duplicated(kanban_home):
    out = kc.run_slash("create '[milestone] QA: y' --assignee argus --skill sdlc-review")
    tid = _created_id(out)
    assert "Added skill" not in out
    assert _skills(tid) == ["sdlc-review"]
    assert "skills_auto_added" not in _events(tid, "created")[0]


def test_create_milestone_qa_whitespace_skill_no_false_notice(kanban_home):
    out = kc.run_slash("create '[milestone] QA: z' --assignee argus --skill ' sdlc-review '")
    assert "Added skill" not in out
    assert _skills(_created_id(out)) == ["sdlc-review"]


def test_idempotent_hit_on_legacy_card_no_false_notice(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="[milestone] QA: old", assignee="argus",
                             idempotency_key="k1")
        conn.execute("UPDATE tasks SET skills = NULL WHERE id = ?", (tid,))
        conn.commit()
    out = kc.run_slash("create '[milestone] QA: old' --assignee argus --idempotency-key k1")
    assert _created_id(out) == tid
    assert "Added skill" not in out
    assert _skills(tid) == []


def test_create_other_titles_untouched(kanban_home):
    for title in ("plain", "[milestone] build x", "fix [milestone] QA wording"):
        with kb.connect() as conn:
            tid = kb.create_task(conn, title=title, assignee="alice")
        assert _skills(tid) == [], title


# -- 3. the lint hint names a flag `kanban edit` actually has -----------------

def _edit_flags() -> set[str]:
    root = argparse.ArgumentParser()
    kc.build_parser(root.add_subparsers(dest="command"))
    kanban = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction)).choices["kanban"]
    sub = next(a for a in kanban._actions if isinstance(a, argparse._SubParsersAction))
    return {opt for act in sub.choices["edit"]._actions for opt in act.option_strings}


def test_edit_parser_has_skill_flags():
    flags = _edit_flags()
    assert {"--skill", "--clear-skills"} <= flags


def test_hint_command_parses():
    """The exact remediation the lint prints must parse as a `kanban edit`."""
    root = argparse.ArgumentParser()
    kc.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(["kanban", "edit", "t_1", "--skill", kb.MILESTONE_QA_SKILL])
    assert args.skills == ["sdlc-review"]


def test_lint_hint_flags_exist_in_edit_parser():
    """Contract with ~/.hermes/scripts/kanban-review-lane-lint.py: every
    `kanban edit --flag` it suggests is a real `edit` option. Skips when the
    home checkout isn't present (fork CI)."""
    lint = Path.home() / ".hermes" / "scripts" / "kanban-review-lane-lint.py"
    if not lint.exists():
        pytest.skip("hermes-home lint not on this host")
    hinted = set(re.findall(r"kanban edit (?:\S+ )?(--[a-z-]+)", lint.read_text()))
    assert hinted, "lint no longer names a kanban edit flag"
    assert hinted <= _edit_flags()
