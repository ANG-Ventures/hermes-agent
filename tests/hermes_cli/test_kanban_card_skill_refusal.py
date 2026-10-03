"""Dispatcher refuses cards whose skills the assignee profile cannot load.

t_0b786d9b (2026-10-03): card skills ``power-outage-recovery`` +
``ups-nut-fleet`` lived in an external skills dir the assignee profile did not
list; the worker exited ``Unknown skill(s)`` at startup three times and the
dispatcher gave up. These tests drive the real ``dispatch_once`` against a temp
home with real profile dirs and config.yaml files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_skill_resolve as ksr


def _skill(root: Path, rel: str, name: str | None = None) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name or Path(rel).name}\ndescription: x\n---\nbody\n"
    (d / "SKILL.md").write_text(fm)
    return d


def _profile(home: Path, name: str, external: list[Path]) -> Path:
    p = home / "profiles" / name
    (p / "skills").mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"    - {e}" for e in external)
    (p / "config.yaml").write_text(
        "skills:\n  external_dirs:\n" + lines + "\n" if external else "skills: {}\n"
    )
    return p


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    shared = home / "skills-shared"
    _skill(shared / "general", "kanban-worker")
    _skill(shared / "devops", "ups-nut-fleet")
    _skill(shared / "devops", "ops/power-outage-recovery")
    return home


# ---------------------------------------------------------------- resolver


def test_resolver_sees_external_dirs_frontmatter_and_skips_archive(tmp_path):
    home = tmp_path / "h"
    ext = tmp_path / "ext"
    _skill(ext, "cat/dir-name", name="fm-name")
    _skill(home / "skills" / ".archive", "archived-only")
    _profile_home = home
    (home / "config.yaml").write_text(f"skills:\n  external_dirs:\n    - {ext}\n")
    dirs = ksr.profile_skill_dirs(_profile_home)
    assert ksr.skill_resolves("dir-name", dirs)
    assert ksr.skill_resolves("fm-name", dirs)
    assert ksr.skill_resolves("cat/dir-name", dirs)
    assert not ksr.skill_resolves("archived-only", dirs)
    # plugin-qualified / absolute identifiers are never judged (fail open)
    assert ksr.unresolved_skills(["plug:x", "/abs/x", "nope"], home) == ["nope"]


def test_kanban_worker_gate_honours_external_dirs(kanban_home):
    shared = kanban_home / "skills-shared"
    with_general = _profile(kanban_home, "w1", [shared / "general"])
    archived_only = _profile(kanban_home, "w2", [])
    _skill(archived_only / "skills" / ".archive" / "old", "kanban-worker")
    assert kb._kanban_worker_skill_available(str(with_general)) is True
    assert kb._kanban_worker_skill_available(str(archived_only)) is False


# ---------------------------------------------------------------- dispatcher


def test_dispatch_blocks_card_naming_the_missing_category(kanban_home):
    shared = kanban_home / "skills-shared"
    _profile(kanban_home, "worker", [shared / "general"])
    spawned = []
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="power blip", assignee="worker",
            skills=["power-outage-recovery", "ups-nut-fleet"],
        )
        res = kb.dispatch_once(conn, spawn_fn=lambda t, ws: spawned.append(t.id))
        task = kb.get_task(conn, tid)
        reason = kb._latest_block_reason(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]
    assert spawned == []
    assert res.skill_refused == [(tid, ["power-outage-recovery", "ups-nut-fleet"])]
    assert task.status == "blocked"
    assert str((shared / "devops").resolve()) in reason, reason
    assert "power-outage-recovery" in reason and "worker" in reason
    assert "skill_refused" in events


def test_dispatch_spawns_when_category_is_listed(kanban_home):
    shared = kanban_home / "skills-shared"
    _profile(kanban_home, "worker", [shared / "general", shared / "devops"])
    spawned = []
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="power blip", assignee="worker",
            skills=["power-outage-recovery", "ups-nut-fleet"],
        )
        res = kb.dispatch_once(conn, spawn_fn=lambda t, ws: spawned.append(t.id))
    assert res.skill_refused == []
    assert spawned == [tid]


def test_dispatch_dry_run_reports_without_blocking(kanban_home):
    _profile(kanban_home, "worker", [])
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker", skills=["ups-nut-fleet"])
        res = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
    assert res.skill_refused == [(tid, ["ups-nut-fleet"])]
    assert task.status == "ready"
