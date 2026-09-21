"""Policy gates for explicit flagship model overrides."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import model_switch as ms
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


@pytest.fixture
def firepower_aliases(monkeypatch):
    saved = dict(ms.DIRECT_ALIASES)
    saved_degraded = ms._DIRECT_ALIASES_DEGRADED
    ms.DIRECT_ALIASES.clear()
    ms._DIRECT_ALIASES_DEGRADED = False

    def _loader():
        merged = dict(ms._BUILTIN_DIRECT_ALIASES)
        merged.update({
            "astra": ms.DirectAlias(
                model="gpt-6-astra-900k", provider="openai-codex", base_url=""
            ),
            "gpt": ms.DirectAlias(
                model="gpt-6-astra-900k", provider="openai-codex", base_url=""
            ),
        })
        return merged, True

    monkeypatch.setattr(ms, "_load_direct_aliases", _loader)
    yield
    ms.DIRECT_ALIASES.clear()
    ms.DIRECT_ALIASES.update(saved)
    ms._DIRECT_ALIASES_DEGRADED = saved_degraded


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


def test_alias_is_canonicalized_before_guard_and_classification(firepower_aliases):
    assert "--firepower" in firepower_guard_error("astra", None)
    assert "--firepower" in firepower_guard_error("gpt", None)
    assert route_kind("astra") == "firepower"


def test_create_refuses_firepower_alias_without_reason(kanban_home, firepower_aliases):
    output = kc.run_slash("create 'aliased' --assignee worker --model astra")
    assert "--firepower" in output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_create_refuses_flagship_without_firepower_reason(kanban_home):
    output = kc.run_slash(
        "create 'expensive task' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex"
    )
    assert "--firepower" in output
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
    assert "openai-codex/gpt-6-astra-900k" in comments[0].body


def test_idempotent_create_same_route_does_not_duplicate_audit(kanban_home):
    command = (
        "create 'expensive task' --assignee worker --idempotency-key same "
        "--model gpt-6-astra-900k --provider openai-codex "
        "--firepower 'hard recovery'"
    )
    first_id = _created_id(kc.run_slash(command))
    second_id = _created_id(kc.run_slash(command))
    assert second_id == first_id
    with kb.connect() as conn:
        comments = kb.list_comments(conn, first_id)
    assert len(comments) == 1


def test_idempotent_create_rejects_conflicting_route_without_false_audit(kanban_home):
    first_id = _created_id(kc.run_slash(
        "create 'plain task' --assignee worker --idempotency-key same "
        "--model gpt-5.6-sol-900k --provider openai-codex"
    ))
    rejected = kc.run_slash(
        "create 'different route' --assignee worker --idempotency-key same "
        "--model gpt-6-astra-900k --provider openai-codex "
        "--firepower 'hard recovery'"
    )
    assert "conflicts with existing task" in rejected
    with kb.connect() as conn:
        task = kb.get_task(conn, first_id)
        comments = kb.list_comments(conn, first_id)
    assert task.model_override == "gpt-5.6-sol-900k"
    assert comments == []


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


@pytest.mark.parametrize(
    "blank",
    ["", "   "],
    ids=["empty", "whitespace"],
)
def test_set_model_blank_justification_is_not_a_bypass(kanban_home, blank):
    """A blank audit body must not satisfy the firepower requirement.

    ``audit_comment_body`` counts as the written justification, so a blank one
    would otherwise be a silent way past the guard.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="blank justification",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
        )
        with pytest.raises(ValueError, match="firepower"):
            kb.set_model_override(
                conn,
                task_id,
                "gpt-6-astra-900k",
                provider="openai-codex",
                audit_comment_author="operator",
                audit_comment_body=blank,
            )
        with pytest.raises(ValueError, match="firepower"):
            kb.set_model_override(
                conn,
                task_id,
                "gpt-6-astra-900k",
                provider="openai-codex",
                firepower_reason=blank,
            )
        task = kb.get_task(conn, task_id)
    assert task.model_override == "gpt-5.6-sol-900k"


def test_set_model_accepts_explicit_audit_comment_as_justification(kanban_home):
    """An explicit audit comment is a written reason; it needs no duplicate flag."""
    body = "firepower override: reason=hard recovery"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="explicit audit",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
        )
        assert kb.set_model_override(
            conn,
            task_id,
            "gpt-6-astra-900k",
            provider="openai-codex",
            audit_comment_author="operator",
            audit_comment_body=body,
        )
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-6-astra-900k"
    assert [c.body for c in comments] == [body]


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
                audit_comment_body="firepower override: reason=hard recovery",
            )
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.model_override == "gpt-5.6-sol-900k"


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


def test_set_task_model_keeps_literal_empty_string_while_guarded(kanban_home):
    """Guarding ``set_task_model`` must not change its empty-string contract.

    ``set_task_model`` is literal: ``""`` is stored verbatim, unlike
    ``set_model_override`` which treats ``""`` as "clear the override".
    Routing the guard through the latter silently coerced ``""`` to NULL.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="literal", assignee="worker")

        assert kb.set_task_model(conn, task_id, "") == 1
        raw = conn.execute(
            "SELECT model_override FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert raw["model_override"] == ""

        # None still clears to SQL NULL.
        assert kb.set_task_model(conn, task_id, None) == 1
        raw = conn.execute(
            "SELECT model_override FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert raw["model_override"] is None

        # A nonexistent id is a no-op, never a silent success.
        assert kb.set_task_model(conn, "t_missing", "claude-opus-5") == 0

        # ...and the guard is still armed on this path.
        with pytest.raises(ValueError, match="firepower"):
            kb.set_task_model(conn, task_id, "gpt-6-astra-900k")
