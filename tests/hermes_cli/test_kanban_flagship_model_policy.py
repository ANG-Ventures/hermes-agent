from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import model_switch as ms
from hermes_cli.model_policy import FLAGSHIP_MODEL_SUBSTRINGS


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kc.build_parser(subparsers)
    return parser.parse_args(["kanban", *argv])


def _created_id(stdout: str) -> str:
    match = re.search(r"t_[a-f0-9]+", stdout)
    assert match is not None
    return match.group(0)


@pytest.fixture
def flagship_alias(monkeypatch):
    saved = dict(ms.DIRECT_ALIASES)
    saved_degraded = ms._DIRECT_ALIASES_DEGRADED
    ms.DIRECT_ALIASES.clear()
    ms._DIRECT_ALIASES_DEGRADED = False

    def _loader():
        merged = dict(ms._BUILTIN_DIRECT_ALIASES)
        merged["fast"] = ms.DirectAlias(
            model="gpt-6-astra", provider="openai-codex", base_url=""
        )
        return merged, True

    monkeypatch.setattr(ms, "_load_direct_aliases", _loader)
    yield
    ms.DIRECT_ALIASES.clear()
    ms.DIRECT_ALIASES.update(saved)
    ms._DIRECT_ALIASES_DEGRADED = saved_degraded


@pytest.mark.parametrize("model", ["gpt-6-astra", "CLAUDE-FABLE-5-1"])
def test_create_refuses_flagship_model_without_override(
    kanban_home, capsys, model,
):
    rc = kc.kanban_command(
        _parse("create", "guarded", "--assignee", "worker", "--model", model)
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert (
        f"flagship model '{model}' is orchestrator-only (Ace 2026-09-17). "
        "Workers: claude-opus-5/claude-apr, or openai-codex/gpt-5.6-sol "
        "when Claude is capped. Override with --allow-flagship \"<reason>\" "
        "(logged)."
    ) in captured.err
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_create_override_records_comment(kanban_home, capsys):
    rc = kc.kanban_command(
        _parse(
            "create",
            "incident",
            "--assignee",
            "worker",
            "--model",
            "gpt-6-astra",
            "--allow-flagship",
            "incident commander approved",
        )
    )

    captured = capsys.readouterr()
    assert rc == 0
    task_id = _created_id(captured.out)
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override == "gpt-6-astra"
        comments = kb.list_comments(conn, task_id)
    assert [comment.body for comment in comments] == [
        "flagship override: incident commander approved"
    ]


def test_create_validates_resolved_alias_and_records_override(
    kanban_home, flagship_alias, capsys,
):
    rc = kc.kanban_command(
        _parse("create", "blocked alias", "--assignee", "worker", "--model", "fast")
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "flagship model 'gpt-6-astra' is orchestrator-only" in captured.err
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []

    rc = kc.kanban_command(
        _parse(
            "create",
            "allowed alias",
            "--assignee",
            "worker",
            "--model",
            "fast",
            "--allow-flagship",
            "incident alias approved",
        )
    )
    captured = capsys.readouterr()
    assert rc == 0
    task_id = _created_id(captured.out)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert (task.model_override, task.provider_override) == (
        "gpt-6-astra",
        "openai-codex",
    )
    assert [comment.body for comment in comments] == [
        "flagship override: incident alias approved"
    ]


def test_set_model_refuses_then_override_records_comment(kanban_home, capsys):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="existing", assignee="worker")

    rc = kc.kanban_command(_parse("set-model", task_id, "gpt-6-astra"))
    captured = capsys.readouterr()
    assert rc == 2
    assert "flagship model 'gpt-6-astra' is orchestrator-only" in captured.err
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override is None

    rc = kc.kanban_command(
        _parse(
            "set-model",
            task_id,
            "gpt-6-astra",
            "--allow-flagship",
            "production incident",
        )
    )
    capsys.readouterr()
    assert rc == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override == "gpt-6-astra"
        comments = kb.list_comments(conn, task_id)
    assert [comment.body for comment in comments] == [
        "flagship override: production incident"
    ]


def test_set_model_validates_resolved_alias_and_records_override(
    kanban_home, flagship_alias, capsys,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="existing alias", assignee="worker")

    rc = kc.kanban_command(_parse("set-model", task_id, "fast"))
    captured = capsys.readouterr()
    assert rc == 2
    assert "flagship model 'gpt-6-astra' is orchestrator-only" in captured.err
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override is None

    rc = kc.kanban_command(
        _parse(
            "set-model",
            task_id,
            "fast",
            "--allow-flagship",
            "set alias approved",
        )
    )
    capsys.readouterr()
    assert rc == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert (task.model_override, task.provider_override) == (
        "gpt-6-astra",
        "openai-codex",
    )
    assert [comment.body for comment in comments] == [
        "flagship override: set alias approved"
    ]


def test_programmatic_create_refuses_flagship_without_reason(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.create_task(
                conn,
                title="bypass",
                assignee="worker",
                model_override="claude-fable-5",
            )


def test_edit_refuses_resolved_flagship_alias(
    kanban_home, flagship_alias, capsys,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="edit bypass", assignee="worker")

    rc = kc.kanban_command(_parse("edit", task_id, "--model", "fast"))
    captured = capsys.readouterr()
    assert rc == 1
    assert "flagship model 'gpt-6-astra' is orchestrator-only" in captured.err
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override is None


def test_configured_ban_is_used_by_create(kanban_home, capsys):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  banned_worker_model_substrings: [ultra-worker]\n",
        encoding="utf-8",
    )

    rc = kc.kanban_command(
        _parse(
            "create",
            "custom policy",
            "--assignee",
            "worker",
            "--model",
            "vendor-ultra-worker-v2",
        )
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "flagship model 'vendor-ultra-worker-v2'" in captured.err


def test_dispatch_refuses_direct_db_bypass_and_logs_once(
    kanban_home, monkeypatch, all_assignees_spawnable, caplog,
):
    spawned = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="bypass", assignee="worker")
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("gpt-6-astra", task_id),
        )
        conn.commit()

        first = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: spawned.append(task.id))
        second = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: spawned.append(task.id))
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)

    assert spawned == []
    assert first.flagship_refused == [task_id]
    assert second.flagship_refused == [task_id]
    assert task.status == "ready"
    refusal_comments = [
        comment.body for comment in comments
        if comment.body.startswith("flagship dispatch refused:")
    ]
    assert len(refusal_comments) == 1
    assert "gpt-6-astra" in refusal_comments[0]
    assert (
        f"PHASE=kanban_flagship_refused task={task_id} model=gpt-6-astra"
        in caplog.text
    )


def test_dispatch_allows_direct_db_model_with_override_comment(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    spawned = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="authorized", assignee="worker")
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("claude-fable-5", task_id),
        )
        conn.commit()
        kb.add_comment(
            conn,
            task_id,
            "operator",
            "flagship override: incident commander approved",
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )

    assert spawned == [task_id]
    assert result.flagship_refused == []


def test_policy_constant_matches_default_contract():
    assert FLAGSHIP_MODEL_SUBSTRINGS == ("fable", "astra")
