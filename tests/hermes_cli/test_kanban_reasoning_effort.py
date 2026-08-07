"""Per-task reasoning effort — DB layer + dispatcher passthrough.

Fork port of upstream 0b69a6ac0 ("feat(kanban): let a task pin its own
thinking depth"). Upstream's coverage for this layer lives in
tests/plugins/test_kanban_model_override.py; the fork keeps kanban DB tests
under tests/hermes_cli/, so the behaviours are asserted here:

  * ``tasks.reasoning_effort`` column exists; migration is idempotent on an
    existing kanban.db (run twice, pre-existing rows stay NULL = inherit).
  * ``create_task(reasoning_effort=...)`` normalizes case-insensitively
    ("  XHigh " -> "xhigh") and persists; omitted -> genuine SQL NULL.
  * ``"none"`` is a real stored level (thinking off), not a clear.
  * unknown levels are rejected loudly (ValueError naming reasoning_effort),
    never silently falling back to the profile default.
  * ``set_reasoning_effort`` sets / clears (None or "" -> NULL), refuses
    archived tasks, and is independent of the model override in BOTH
    directions.
  * dispatcher spawn argv carries ``--reasoning <level>`` without requiring
    a model override, and omits the flag when unset.
  * the worker CLI actually accepts ``--reasoning`` (flag round-trip).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# create_task / normalization
# ---------------------------------------------------------------------------

def test_create_task_normalizes_and_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", reasoning_effort="  XHigh ",
        )
        task = kb.get_task(conn, tid)
    assert task.reasoning_effort == "xhigh"


def test_create_task_without_effort_is_null(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        raw = conn.execute(
            "SELECT reasoning_effort FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert raw["reasoning_effort"] is None  # genuine NULL = inherit profile


def test_none_is_a_real_level(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", reasoning_effort="none",
        )
        raw = conn.execute(
            "SELECT reasoning_effort FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert raw["reasoning_effort"] == "none"  # stored value, NOT NULL


def test_create_task_rejects_unknown_level(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="reasoning_effort"):
            kb.create_task(
                conn, title="t", assignee="worker",
                reasoning_effort="extremely-hard",
            )
        # Nothing half-written.
        assert kb.list_tasks(conn) == []


def test_all_valid_levels_accepted(kanban_home):
    from hermes_constants import VALID_REASONING_EFFORTS

    with kb.connect() as conn:
        for level in ("none", *VALID_REASONING_EFFORTS):
            tid = kb.create_task(
                conn, title=f"t-{level}", assignee="worker",
                reasoning_effort=level,
            )
            assert kb.get_task(conn, tid).reasoning_effort == level


# ---------------------------------------------------------------------------
# set_reasoning_effort
# ---------------------------------------------------------------------------

def test_set_reasoning_effort_sets_normalizes_and_clears(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.set_reasoning_effort(conn, tid, "XHIGH")
        assert kb.get_task(conn, tid).reasoning_effort == "xhigh"
        # Empty string clears to NULL (inherit).
        assert kb.set_reasoning_effort(conn, tid, "")
        raw = conn.execute(
            "SELECT reasoning_effort FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert raw["reasoning_effort"] is None
        # None clears too.
        assert kb.set_reasoning_effort(conn, tid, "high")
        assert kb.set_reasoning_effort(conn, tid, None)
        assert kb.get_task(conn, tid).reasoning_effort is None


def test_set_reasoning_effort_rejects_unknown_level(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", reasoning_effort="high",
        )
        with pytest.raises(ValueError, match="reasoning_effort"):
            kb.set_reasoning_effort(conn, tid, "turbo")
        # Loud rejection, no silent fallback: stored value untouched.
        assert kb.get_task(conn, tid).reasoning_effort == "high"


def test_set_reasoning_effort_unknown_task_returns_false(kanban_home):
    with kb.connect() as conn:
        assert kb.set_reasoning_effort(conn, "t_nope", "high") is False


def test_set_reasoning_effort_refuses_archived(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,)
        )
        with pytest.raises(RuntimeError):
            kb.set_reasoning_effort(conn, tid, "high")


# ---------------------------------------------------------------------------
# Independence from the model override (both directions)
# ---------------------------------------------------------------------------

def test_effort_without_model_override(kanban_home):
    """A card can pin depth while running the profile's own model."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", reasoning_effort="low",
        )
        t = kb.get_task(conn, tid)
    assert t.model_override is None
    assert t.reasoning_effort == "low"


def test_clearing_model_override_keeps_effort(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker",
            model_override="claude-opus-4-8", reasoning_effort="ultra",
        )
        assert kb.set_model_override(conn, tid, None)
        t = kb.get_task(conn, tid)
    assert t.model_override is None
    assert t.reasoning_effort == "ultra"


def test_setting_effort_keeps_model_override(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker",
            model_override="claude-opus-4-8", provider_override=None,
        )
        assert kb.set_reasoning_effort(conn, tid, "xhigh")
        t = kb.get_task(conn, tid)
    assert t.model_override == "claude-opus-4-8"
    assert t.reasoning_effort == "xhigh"


# ---------------------------------------------------------------------------
# Migration idempotency on an EXISTING kanban.db
# ---------------------------------------------------------------------------

def _reasoning_cols(conn) -> list:
    return [
        r["name"] for r in conn.execute("PRAGMA table_info(tasks)")
        if r["name"] == "reasoning_effort"
    ]


def test_migration_adds_column_and_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    db_path = kb.kanban_db_path()
    # Rebuild a PRE-FEATURE db: drop the column, seed a legacy row.
    raw = sqlite3.connect(str(db_path))
    raw.execute("ALTER TABLE tasks DROP COLUMN reasoning_effort")
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_legacy1', 'legacy', 'done', 1000)"
    )
    raw.commit()
    raw.close()

    # First open post-drop runs the migration.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect() as conn:
        assert _reasoning_cols(conn) == ["reasoning_effort"]
        # Pre-existing row reads as NULL = inherit, and loads fine.
        assert kb.get_task(conn, "t_legacy1").reasoning_effort is None

    # Second run on the SAME existing db must be a clean no-op.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect() as conn:
        kb._migrate_add_optional_columns(conn)  # explicit third pass
        assert _reasoning_cols(conn) == ["reasoning_effort"]
        assert kb.get_task(conn, "t_legacy1").reasoning_effort is None


# ---------------------------------------------------------------------------
# Dispatcher spawn argv
# ---------------------------------------------------------------------------

def _spawn_argv_for(monkeypatch, task) -> list:
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    workspace = kb.resolve_workspace(task)
    assert kb._default_spawn(task, str(workspace)) == 4242
    return captured["cmd"]


def test_spawn_passes_reasoning_without_a_model(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", reasoning_effort="xhigh",
        )
        task = kb.get_task(conn, tid)
    argv = _spawn_argv_for(monkeypatch, task)
    i = argv.index("--reasoning")
    assert argv[i + 1] == "xhigh"
    assert "-m" not in argv  # depth is independent of the model knob


def test_spawn_omits_reasoning_when_unset(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        task = kb.get_task(conn, tid)
    argv = _spawn_argv_for(monkeypatch, task)
    assert "--reasoning" not in argv


def test_spawn_passes_reasoning_alongside_model(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker",
            model_override="claude-opus-4-8", reasoning_effort="none",
        )
        task = kb.get_task(conn, tid)
    argv = _spawn_argv_for(monkeypatch, task)
    assert argv[argv.index("-m") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--reasoning") + 1] == "none"


def test_dispatch_once_spawn_argv_carries_reasoning(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Full dispatcher tick (claim -> spawn) drives the real _default_spawn."""
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 777

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with kb.connect() as conn:
        kb.create_task(
            conn, title="t", assignee="worker",
            reasoning_effort="high", initial_status="running",
        )
        result = kb.dispatch_once(conn)
    assert result.spawned, f"expected a spawn, got {result!r}"
    cmd = captured["cmd"]
    assert cmd[cmd.index("--reasoning") + 1] == "high"


# ---------------------------------------------------------------------------
# The worker CLI accepts the flag the dispatcher emits
# ---------------------------------------------------------------------------

def test_worker_cli_accepts_the_reasoning_flag():
    """--reasoning must be a real flag on the worker's own CLI — the
    dispatcher passthrough is inert otherwise."""
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    args = parser.parse_args(
        ["--cli", "chat", "-q", "hi", "--reasoning", "high"]
    )
    assert args.reasoning == "high"
