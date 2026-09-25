from __future__ import annotations

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
    (home / "config.yaml").write_text(
        "providers:\n"
        "  batch-provider:\n"
        "    base_url: http://127.0.0.1:9999/v1\n"
        "    api_key: test\n",
        encoding="utf-8",
    )
    return home


def _create(title: str, assignee: str, *, initial_status: str | None = None) -> str:
    suffix = f" --initial-status {initial_status}" if initial_status else ""
    out = kc.run_slash(f"create '{title}' --assignee {assignee}{suffix}")
    match = re.search(r"t_[a-f0-9]+", out)
    assert match, out
    return match.group(0)


def test_set_model_accepts_multiple_task_ids(kanban_home):
    first = _create("first", "worker")
    second = _create("second", "worker")

    out = kc.run_slash(
        f"set-model {first} {second} model-a --provider batch-provider"
    )

    assert f"{first}: route=batch-provider/model-a applies=next-dispatch" in out
    assert f"{second}: route=batch-provider/model-a applies=next-dispatch" in out
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override == "model-a"
        assert kb.get_task(conn, second).model_override == "model-a"


def test_set_model_where_selects_status_and_assignee(kanban_home):
    ready = _create("ready", "worker")
    running = _create("running", "worker")
    other = _create("other", "other")
    blocked = _create("blocked", "worker", initial_status="blocked")
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))

    out = kc.run_slash(
        "set-model model-a --provider batch-provider "
        "--where status=running,ready assignee=worker"
    )

    assert ready in out
    assert running in out
    assert other not in out
    assert blocked not in out
    with kb.connect() as conn:
        assert kb.get_task(conn, ready).model_override == "model-a"
        assert kb.get_task(conn, running).model_override == "model-a"
        assert kb.get_task(conn, other).model_override is None
        assert kb.get_task(conn, blocked).model_override is None


def test_set_model_all_active_excludes_terminal_tasks(kanban_home):
    ready = _create("ready", "worker")
    blocked = _create("blocked", "worker", initial_status="blocked")
    done = _create("done", "worker")
    archived = _create("archived", "worker")
    with kb.connect() as conn:
        kb.complete_task(conn, done, result="done")
        kb.archive_task(conn, archived)

    out = kc.run_slash(
        "set-model model-a --provider batch-provider --all-active"
    )

    assert ready in out
    assert blocked in out
    assert done not in out
    assert archived not in out


def test_set_model_reclaim_only_reclaims_selected_running_cards(
    kanban_home, monkeypatch,
):
    first = _create("first", "worker")
    second = _create("second", "other")
    signaled = []
    with kb.connect() as conn:
        for task_id, pid in ((first, 111), (second, 222)):
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
                    (f"host:{pid}", pid, task_id),
                )
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda pid, lock, **_kw: signaled.append(pid) or {})

    out = kc.run_slash(
        "set-model model-a --provider batch-provider --reclaim "
        "--where status=running assignee=worker"
    )

    assert f"{first}: route=batch-provider/model-a applies=redispatch" in out
    assert second not in out
    assert signaled == [111]
    with kb.connect() as conn:
        assert kb.get_task(conn, first).status == "ready"
        assert kb.get_task(conn, second).status == "running"


def test_set_model_reclaim_leaves_non_running_selected_cards_alone(
    kanban_home, monkeypatch,
):
    """--reclaim must act on RUNNING cards only, even when the selection is wider.

    Guards the `and task.status == "running"` clause specifically: the sibling
    reclaim test selects only running cards, so a mutation to `or` survives it.
    Reclaiming a ready/blocked card would release claims that don't exist and
    (for blocked) churn a card a human deliberately parked.
    """
    running = _create("running", "worker")
    ready = _create("ready", "worker")
    blocked = _create("blocked", "worker", initial_status="blocked")
    signaled = []
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? "
                "WHERE id=?",
                ("host:311", 311, running),
            )
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda pid, lock, **_kw: signaled.append(pid) or {},
    )

    out = kc.run_slash(
        "set-model model-a --provider batch-provider --reclaim --all-active"
    )

    # Every selected card is re-routed ...
    assert f"{running}: route=batch-provider/model-a applies=redispatch" in out
    assert f"{ready}: route=batch-provider/model-a applies=next-dispatch" in out
    assert f"{blocked}: route=batch-provider/model-a applies=next-dispatch" in out
    # ... but only the running one was actually reclaimed.
    assert signaled == [311]
    with kb.connect() as conn:
        assert kb.get_task(conn, running).status == "ready"
        assert kb.get_task(conn, ready).status == "ready"
        assert kb.get_task(conn, blocked).status == "blocked"


def test_set_model_provider_validation_is_atomic_for_batch(kanban_home):
    first = _create("first", "worker")
    second = _create("second", "worker")

    out = kc.run_slash(
        f"set-model {first} {second} model-a --provider typo-provider"
    )

    assert "unknown provider" in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override is None
        assert kb.get_task(conn, second).model_override is None


@pytest.mark.parametrize("selector", ["ids", "where", "all-active", "reclaim"])
@pytest.mark.parametrize("discovery", ["raises", "empty"])
def test_set_model_refuses_unvalidated_provider_when_registry_unavailable(
    kanban_home, monkeypatch, selector, discovery,
):
    import providers

    first = _create("first", "worker")
    second = _create("second", "worker")
    (kanban_home / "config.yaml").write_text("providers: {}\n", encoding="utf-8")
    if discovery == "raises":
        def broken_registry():
            raise RuntimeError("registry unavailable")
        monkeypatch.setattr(providers, "list_providers", broken_registry)
    else:
        monkeypatch.setattr(providers, "list_providers", lambda: [])
    if selector == "reclaim":
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (first,))
    commands = {
        "ids": f"set-model {first} {second} model-a",
        "where": "set-model model-a --where status=running,ready assignee=worker",
        "all-active": "set-model model-a --all-active",
        "reclaim": "set-model model-a --all-active --reclaim",
    }
    out = kc.run_slash(f"{commands[selector]} --provider typo-provider-zz")
    assert "provider" in out.lower() and ("unknown" in out.lower() or "discover" in out.lower()), out
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override is None
        assert kb.get_task(conn, second).model_override is None
        assert kb.get_task(conn, first).status == ("running" if selector == "reclaim" else "ready")


def test_set_model_batch_firepower_gate_is_atomic(kanban_home):
    first = _create("first", "worker")
    second = _create("second", "worker")

    out = kc.run_slash(
        f"set-model {first} {second} gpt-6-astra-900k --provider batch-provider"
    )

    assert "orchestrator-only" in out
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override is None
        assert kb.get_task(conn, second).model_override is None


def test_set_model_selector_requires_a_match(kanban_home):
    out = kc.run_slash(
        "set-model model-a --provider batch-provider --where assignee=nobody"
    )
    assert "matched no tasks" in out.lower()
