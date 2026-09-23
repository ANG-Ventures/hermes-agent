"""A `set-model` batch must be all-or-nothing, even under concurrent mutation.

The batch selects N cards and then writes them. When each write committed
individually, a card that changed status AFTER selection (another connection
archives it, a worker claims it) failed midway and left the cards ahead of it
pinned — a silently split route across what the operator issued as ONE action,
with no receipt naming the cards that moved.

Static prevalidation cannot close this: the whole defect lives in the interval
between "we checked" and "we wrote". These tests inject the interleaving at
exactly that point, using a SECOND REAL SQLite connection (only the scheduling
is synthetic; both DB operations are real), and assert nothing committed.
"""

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


def _create(title: str, assignee: str = "worker") -> str:
    out = kc.run_slash(f"create '{title}' --assignee {assignee}")
    match = re.search(r"t_[a-f0-9]+", out)
    assert match, out
    return match.group(0)


def _route(task_id: str):
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    return task.status, task.provider_override, task.model_override, task.reasoning_effort


def _archive_from_another_connection(task_id: str) -> None:
    """Archive via a genuinely separate connection, as a concurrent actor would."""
    with kb.connect() as other:
        assert kb.archive_task(other, task_id)


def _interleave_after_selection(monkeypatch, action) -> dict:
    """Run ``action`` after the batch selects its cards, before it writes any.

    That boundary IS the defect window: the CLI has finished choosing and
    validating its cards, and has not yet written any of them. Injecting here
    models a concurrent actor that commits between those two moments, which is
    precisely what the old per-card-commit loop mishandled.

    The seam is ``_select_batch_tasks`` deliberately — it exists both before
    and after the atomicity fix, so this same test can be run against the
    pre-fix tree to prove it is RED there.

    The action must NOT run inside the batch transaction: a second connection
    writing there would simply block on ``BEGIN IMMEDIATE`` until the busy
    timeout, which is correct serialization, not the race under test.
    """
    state = {"fired": 0}
    original = kc._select_batch_tasks

    def hooked(conn, **kwargs):
        tasks, error = original(conn, **kwargs)
        if not error:
            state["fired"] += 1
            action()
        return tasks, error

    monkeypatch.setattr(kc, "_select_batch_tasks", hooked)
    return state


# --------------------------------------------------------------------------
# The reported defect: a card archived after selection, mid-batch.
# --------------------------------------------------------------------------

def test_batch_rolls_back_when_a_card_is_archived_mid_batch(kanban_home, monkeypatch):
    first = _create("first")
    second = _create("second")

    state = _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(second),
    )
    out = kc.run_slash(f"set-model {first} {second} standard-model --provider batch-provider")

    assert state["fired"] == 1, "the interleaving must land after selection"
    # The refusal is reported...
    assert "archived" in out and second in out
    # ...and NOTHING committed. Before the fix `first` was left pinned here.
    assert _route(first) == ("ready", None, None, None)
    assert _route(second)[1:] == (None, None, None)


def test_batch_rolls_back_effort_too_when_a_card_is_archived_mid_batch(
    kanban_home, monkeypatch,
):
    """Same class via the effort knob: a model+effort batch is one unit."""
    first = _create("first")
    second = _create("second")

    _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(second),
    )
    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --effort high"
    )

    assert "archived" in out
    assert _route(first) == ("ready", None, None, None)


def test_selector_batch_rolls_back_when_a_card_is_archived_mid_batch(
    kanban_home, monkeypatch,
):
    """`--where` selects the same way; it must not commit partially either."""
    first = _create("first")
    second = _create("second")

    _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(second),
    )
    out = kc.run_slash(
        "set-model standard-model --provider batch-provider "
        "--where status=ready assignee=worker"
    )

    assert "archived" in out
    assert _route(first) == ("ready", None, None, None)
    assert _route(second)[1:] == (None, None, None)


def test_batch_rolls_back_when_a_card_is_deleted_mid_batch(kanban_home, monkeypatch):
    """A vanished row is the same class as an archived one: refuse the batch."""
    first = _create("first")
    second = _create("second")

    def delete_second():
        with kb.connect() as other:
            with kb.write_txn(other):
                other.execute("DELETE FROM tasks WHERE id = ?", (second,))

    _interleave_after_selection(monkeypatch, delete_second)
    out = kc.run_slash(f"set-model {first} {second} standard-model --provider batch-provider")

    assert "no such task" in out and second in out
    assert _route(first) == ("ready", None, None, None)


# --------------------------------------------------------------------------
# Controls — the fix must not make normal batches refuse.
# --------------------------------------------------------------------------

def test_uninterrupted_multi_card_batch_still_commits_every_card(kanban_home):
    first = _create("first")
    second = _create("second")
    third = _create("third")

    out = kc.run_slash(
        f"set-model {first} {second} {third} standard-model --provider batch-provider"
    )

    for task_id in (first, second, third):
        assert f"{task_id}: route=batch-provider/standard-model" in out
        assert _route(task_id)[1:3] == ("batch-provider", "standard-model")


def test_interleaved_archive_of_an_unselected_card_does_not_block_the_batch(
    kanban_home, monkeypatch,
):
    """Only contention on a SELECTED card may refuse; a bystander must not."""
    first = _create("first")
    second = _create("second")
    bystander = _create("bystander")

    _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(bystander),
    )
    out = kc.run_slash(f"set-model {first} {second} standard-model --provider batch-provider")

    assert "archived" not in out
    for task_id in (first, second):
        assert _route(task_id)[1:3] == ("batch-provider", "standard-model")


def test_batch_rollback_leaves_no_event_or_audit_residue(kanban_home, monkeypatch):
    """A rolled-back batch must not leave events claiming a write happened."""
    first = _create("first")
    second = _create("second")

    _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(second),
    )
    kc.run_slash(f"set-model {first} {second} standard-model --provider batch-provider")

    with kb.connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'model_override_set'",
            (first,),
        ).fetchone()[0]
    assert events == 0


def test_batch_rollback_fires_no_task_updated_observer(kanban_home, monkeypatch):
    """Observers must never announce a mutation the transaction rolled back."""
    first = _create("first")
    second = _create("second")

    announced: list[str] = []
    original_notify = kb.notify_task_updated

    def recording(conn, task_id, fields, **kwargs):
        announced.append(task_id)
        return original_notify(conn, task_id, fields, **kwargs)

    monkeypatch.setattr(kb, "notify_task_updated", recording)
    _interleave_after_selection(
        monkeypatch, lambda: _archive_from_another_connection(second),
    )
    kc.run_slash(f"set-model {first} {second} standard-model --provider batch-provider")

    assert announced == []


# --------------------------------------------------------------------------
# --reclaim: an irreversible side effect, so it cannot be rolled back —
# but it must never be silent. The receipt is the honest-partial contract
# the atomic path cannot provide here.
# --------------------------------------------------------------------------

def test_reclaim_failure_still_reports_the_routes_that_committed(
    kanban_home, monkeypatch,
):
    first = _create("first")
    second = _create("second")

    def explode(conn, task_id, **kwargs):
        raise RuntimeError("reclaim exploded")

    monkeypatch.setattr(kb, "reclaim_task", explode)
    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --reclaim"
    )

    # Routes did commit (reclaim runs after the batch transaction)...
    for task_id in (first, second):
        assert _route(task_id)[1:3] == ("batch-provider", "standard-model")
        # ...so the operator is told BOTH that the route moved...
        assert f"{task_id}: route=batch-provider/standard-model" in out
        # ...and that the reclaim they asked for did not happen.
        assert f"{task_id}: route applied but reclaim failed" in out
