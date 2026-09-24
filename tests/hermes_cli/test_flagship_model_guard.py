"""Policy gates for explicit flagship model overrides."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.model_policy import firepower_guard_error, route_kind


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _created_id(output: str) -> str:
    match = re.search(r"(t_[a-f0-9]+)", output)
    assert match, output
    return match.group(1)


@pytest.mark.parametrize("model", [
    "gpt-6-astra-900k",
    "openai-codex/gpt-6-astra-900k",
    "claude-fable-5-1",
])
def test_guard_requires_nonempty_reason_for_flagship(model):
    assert "--firepower" in firepower_guard_error(model, None)
    assert "--firepower" in firepower_guard_error(model, "  ")
    assert firepower_guard_error(model, "hard architecture adjudication") is None


def test_guard_does_not_restrict_sub_flagship():
    assert firepower_guard_error("gpt-5.6-sol-900k", None) is None
    assert firepower_guard_error("claude-opus-5", None) is None
    assert route_kind("openai-codex/gpt-5.6-sol-900k") == "standard"
    assert route_kind("openai-codex/gpt-6-astra-900k") == "firepower"


def test_create_refuses_flagship_without_firepower_reason(kanban_home):
    output = kc.run_slash(
        "create 'expensive task' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex"
    )
    # create is main's #823 path: its message names --allow-flagship
    # (--firepower is an argparse alias for the same dest).
    assert "--allow-flagship" in output
    with kb.connect() as conn:
        count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert count == 0


def test_create_accepts_flagship_and_appends_audit_comment(kanban_home):
    reason = "cross-module concurrency diagnosis"
    output = kc.run_slash(
        "create 'expensive task' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex "
        f"--firepower '{reason}'"
    )
    task_id = _created_id(output)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-6-astra-900k"
    assert len(comments) == 1
    assert reason in comments[0].body
    # The dispatcher's flagship gate authorizes on exactly this prefix.
    assert comments[0].body.startswith("flagship override:")


def test_create_rolls_back_if_firepower_audit_comment_fails(
    kanban_home, monkeypatch
):
    def fail_comment(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(kb, "add_comment", fail_comment)
    output = kc.run_slash(
        "create 'must stay audited' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex "
        "--firepower 'hard recovery'"
    )
    assert "audit unavailable" in output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_set_model_refuses_before_mutating_existing_pin(kanban_home):
    output = kc.run_slash("create 'plain task' --assignee worker --model gpt-5.6-sol-900k")
    task_id = _created_id(output)

    rejected = kc.run_slash(
        f"set-model {task_id} gpt-6-astra-900k --provider openai-codex"
    )

    assert "--firepower" in rejected
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-5.6-sol-900k"
    assert comments == []


def test_set_model_accepts_flagship_and_appends_audit_comment(kanban_home):
    output = kc.run_slash("create 'plain task' --assignee worker")
    task_id = _created_id(output)
    reason = "ambiguous multi-file recovery"

    changed = kc.run_slash(
        f"set-model {task_id} gpt-6-astra-900k --provider openai-codex "
        f"--firepower '{reason}'"
    )

    assert "Set model override" in changed
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-6-astra-900k"
    assert len(comments) == 1
    assert reason in comments[0].body


def test_set_model_rolls_back_if_firepower_audit_comment_fails(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="atomic override",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
        )

    def fail_comment(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(kb, "add_comment", fail_comment)
    with kb.connect() as conn:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            kb.set_model_override(
                conn,
                task_id,
                "gpt-6-astra-900k",
                provider="openai-codex",
                audit_comment_author="operator",
                audit_comment_body="flagship override: reason=hard recovery",
            )
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.model_override == "gpt-5.6-sol-900k"


def test_db_layer_refuses_flagship_route_without_dispatch_authorizing_comment(
    kanban_home,
):
    """A route the DB writes must never be one the dispatcher then refuses.

    main's dispatcher gate (``flagship_refused``) only accepts a
    ``flagship override:`` comment, so the batch/lane writer requires exactly
    that prefix rather than any free-text audit body.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="t", assignee="worker")
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
                task_id=task_id, touch_model=True, model="gpt-6-astra-900k",
                provider="openai-codex", audit_comment_author="op",
                audit_comment_body="some other note",
            )])
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.set_model_override(conn, task_id, "gpt-6-astra-900k")
        assert kb.get_task(conn, task_id).model_override is None
        assert kb.list_comments(conn, task_id) == []


def test_batch_flagship_route_passes_main_dispatch_gate(kanban_home):
    """set-model --where with --firepower yields a card main's gate admits."""
    with kb.connect() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="worker") for i in range(2)]
    out = kc.run_slash(
        "set-model gpt-6-astra-900k --where assignee=worker "
        "--firepower 'capacity incident'"
    )
    for task_id in ids:
        assert f"{task_id}: route=gpt-6-astra-900k" in out, out
    with kb.connect() as conn:
        res = kb.dispatch_once(conn, dry_run=True)
        for task_id in ids:
            bodies = [c.body for c in kb.list_comments(conn, task_id)]
            assert any(b.startswith("flagship override:") for b in bodies), bodies
    assert not (set(res.flagship_refused) & set(ids)), res.flagship_refused


def test_effective_worker_route_reads_profile_default(kanban_home):
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="plain", assignee="worker")
        task = kb.get_task(conn, task_id)
    assert kb.effective_worker_route(task) == "openai-codex/gpt-5.6-sol-900k"


def test_effective_worker_route_prefers_card_override(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="pinned",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
            provider_override="openai-codex",
        )
        task = kb.get_task(conn, task_id)
    assert kb.effective_worker_route(task) == "openai-codex/gpt-5.6-sol-900k"


def test_dispatch_tick_records_route_for_each_spawn(kanban_home, monkeypatch):
    from hermes_cli import profiles

    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route announce", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: 12345,
            reconcile_orphans=False,
        )
    assert result.spawn_routes == {
        task_id: "openai-codex/gpt-5.6-sol-900k",
    }
