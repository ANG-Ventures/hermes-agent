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


def _exit_code(rest: str) -> int:
    """Run a `/kanban …` string through the REAL parser and return its exit code.

    ``run_slash`` swallows the return value, and the exit code is part of the
    contract here: a batch whose reclaim failed must not look like a success
    to a shell script.
    """
    import argparse
    import contextlib
    import io

    wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
    parser = kc.build_parser(wrap.add_subparsers(dest="_top"))
    args = parser.parse_args(rest.split())
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return kc.kanban_command(args)


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


# --------------------------------------------------------------------------
# The post-commit receipt must survive ANY failure class, not an enumerated
# pair. Catching `(ValueError, RuntimeError)` let a real
# `sqlite3.OperationalError` from reclaim_task's own BEGIN IMMEDIATE escape
# the receipt loop entirely: routes committed, output said only
# `error: database is locked`, no card was named.
# --------------------------------------------------------------------------

def _claimed(title: str) -> str:
    """Create a card and put it in the claimed/running state a reclaim acts on."""
    task_id = _create(title)
    with kb.connect() as conn:
        assert kb.claim_task(conn, task_id, claimer="probe-claimer")
    return task_id


@pytest.fixture
def lock_after_route_commit(monkeypatch):
    """Hold a REAL second-connection write lock from just after the commit.

    The batch's own connection then hits a genuinely locked database inside
    ``reclaim_task``. Only the scheduling is synthetic — the lock, the
    transaction and the raised ``sqlite3.OperationalError`` are all real
    SQLite. ``busy_timeout`` is cut to keep the probe bounded; production
    waits 120s and reaches the same exception class on a prolonged lock or
    any other SQLite I/O failure.
    """
    holders: list = []
    original = kb.apply_batch_route_writes

    def hold(conn, writes):
        result = original(conn, writes)
        conn.execute("PRAGMA busy_timeout = 75")
        other = kb.connect()
        other.execute("BEGIN IMMEDIATE")
        holders.append(other)
        return result

    monkeypatch.setattr(kb, "apply_batch_route_writes", hold)
    yield
    for conn in holders:
        conn.execute("ROLLBACK")
        conn.close()


def test_sqlite_lock_during_reclaim_still_names_every_committed_route(
    kanban_home, lock_after_route_commit,
):
    first = _claimed("first")
    second = _claimed("second")

    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --reclaim"
    )

    # The routes are durable — that is the fact the operator must not lose.
    for task_id in (first, second):
        assert _route(task_id)[1:3] == ("batch-provider", "standard-model")
        assert f"{task_id}: route=batch-provider/standard-model" in out
        assert f"{task_id}: route applied but reclaim failed" in out
    # ...and the real cause is named, not collapsed to a bare lock error.
    assert "OperationalError" in out
    # The cards were NOT reclaimed, so they are still running.
    assert _route(first)[0] == "running"
    assert _route(second)[0] == "running"


def test_sqlite_lock_during_reclaim_is_not_a_success(kanban_home, lock_after_route_commit):
    task_id = _claimed("only")
    assert _exit_code(
        f"set-model {task_id} standard-model --provider batch-provider --reclaim"
    ) == 1


@pytest.mark.parametrize(
    "exc",
    [
        OSError("disk gone"),
        KeyError("missing"),
        TypeError("bad call"),
        MemoryError(),
    ],
    ids=["OSError", "KeyError", "TypeError", "MemoryError"],
)
def test_any_post_commit_reclaim_exception_class_keeps_the_receipt(
    kanban_home, monkeypatch, exc,
):
    """The guard is positional, not a type allowlist.

    Every one of these walked past the old `(ValueError, RuntimeError)`
    catch and took the receipt with it.
    """
    first = _claimed("first")
    second = _claimed("second")

    def explode(conn, task_id, **kwargs):
        raise exc

    monkeypatch.setattr(kb, "reclaim_task", explode)
    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --reclaim"
    )

    for task_id in (first, second):
        assert _route(task_id)[1:3] == ("batch-provider", "standard-model")
        assert f"{task_id}: route=batch-provider/standard-model" in out
        assert f"{task_id}: route applied but reclaim failed" in out
    assert type(exc).__name__ in out


def test_reclaim_that_refuses_a_card_claimed_at_selection_is_named(
    kanban_home, monkeypatch,
):
    """A FALSE return is the same silence as an exception.

    reclaim_task returns False (it does not raise) when the row is no longer
    reclaimable. For a card that WAS claimed when we selected it, that means
    it changed underneath us — the operator asked for a redispatch and did
    not get one, so it must be reported.
    """
    first = _claimed("first")
    second = _claimed("second")

    real = kb.reclaim_task

    def refuse_second(conn, task_id, **kwargs):
        if task_id == second:
            return False
        return real(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "reclaim_task", refuse_second)
    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --reclaim"
    )

    assert f"{first}: route=batch-provider/standard-model applies=redispatch" in out
    assert f"{second}: route applied but reclaim failed" in out
    assert f"{first}: route applied but reclaim failed" not in out


def test_unclaimed_card_in_a_reclaim_batch_is_not_reported_as_a_failure(kanban_home):
    """Control: a ready card has nothing to reclaim; that is a no-op, not a race."""
    ready = _create("ready-card")

    out = kc.run_slash(
        f"set-model {ready} standard-model --provider batch-provider --reclaim"
    )

    assert "reclaim failed" not in out
    assert _route(ready)[1:3] == ("batch-provider", "standard-model")


def test_successful_reclaim_control_actually_redispatches(kanban_home):
    """Control: without contention the reclaim really happens and says so."""
    first = _claimed("first")
    second = _claimed("second")

    out = kc.run_slash(
        f"set-model {first} {second} standard-model --provider batch-provider --reclaim"
    )

    assert "reclaim failed" not in out
    for task_id in (first, second):
        assert f"{task_id}: route=batch-provider/standard-model applies=redispatch" in out
        assert _route(task_id)[0] == "ready"
    assert _exit_code(
        f"set-model {_claimed('third')} standard-model "
        "--provider batch-provider --reclaim"
    ) == 0


def test_pre_commit_failure_still_reports_plainly_and_writes_nothing(
    kanban_home, monkeypatch,
):
    """The receipt path must not swallow errors from BEFORE the commit.

    Nothing is durable yet there, so the honest output is the bare error —
    and an unexpected class must still propagate rather than be laundered
    into a fake receipt.
    """
    first = _create("first")

    def explode(conn, writes):
        raise RuntimeError("write refused")

    monkeypatch.setattr(kb, "apply_batch_route_writes", explode)
    out = kc.run_slash(
        f"set-model {first} standard-model --provider batch-provider --reclaim"
    )

    assert "write refused" in out
    assert "route applied" not in out
    assert _route(first) == ("ready", None, None, None)


# --------------------------------------------------------------------------
# Same class, sibling command: `reassign --reclaim`.
#
# The reclaim (SIGTERM + claim release) runs BEFORE the assign and cannot be
# undone. When the assign then refused, the CLI printed "still running — pass
# --reclaim to release first" — actively false, since the reclaim had already
# fired and the card was no longer running.
# --------------------------------------------------------------------------

def _assignment(task_id: str):
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return tuple(row)


def test_reassign_reclaim_reports_the_reclaim_when_the_assign_refuses(
    kanban_home, monkeypatch,
):
    task_id = _claimed("victim")

    monkeypatch.setattr(kb, "assign_task", lambda conn, tid, profile: False)
    out = kc.run_slash(f"reassign {task_id} worker-b --reclaim")

    status, claim_lock, _assignee = _assignment(task_id)
    # The reclaim really happened...
    assert claim_lock is None and status != "running"
    # ...so the output must not claim the card is still running.
    assert "still running" not in out
    assert "claim WAS reclaimed" in out
    assert task_id in out


def test_reassign_reclaim_exception_is_named_not_swallowed(kanban_home, monkeypatch):
    task_id = _claimed("victim")

    def explode(conn, tid, **kwargs):
        raise OSError("signal refused")

    monkeypatch.setattr(kb, "reclaim_task", explode)
    out = kc.run_slash(f"reassign {task_id} worker-b --reclaim")

    assert "reclaim failed" in out and "OSError" in out
    assert "NOT reassigned" in out
    # Nothing moved: the reclaim never landed and the assign was not attempted.
    assert _assignment(task_id)[2] == "worker"


def test_reassign_reclaim_success_control(kanban_home):
    """Control: the happy path still reassigns and reports the reclaim."""
    task_id = _claimed("victim")

    out = kc.run_slash(f"reassign {task_id} worker-b --reclaim")

    assert "Reassigned" in out and "claim reclaimed" in out
    status, claim_lock, assignee = _assignment(task_id)
    assert claim_lock is None and assignee == "worker-b" and status != "running"


def test_reassign_without_reclaim_keeps_its_original_refusal(kanban_home):
    """Control: with no --reclaim nothing irreversible ran, so the old
    'still running — pass --reclaim' guidance is still the right message."""
    task_id = _claimed("victim")

    out = kc.run_slash(f"reassign {task_id} worker-b")

    assert "still running" in out
    assert "claim WAS reclaimed" not in out
    assert _assignment(task_id)[2] == "worker"


def test_reassign_reclaim_on_an_unclaimed_card_does_not_claim_a_reclaim(kanban_home):
    """A ready card has no claim; --reclaim is a no-op and must not say otherwise."""
    task_id = _create("ready-card")

    out = kc.run_slash(f"reassign {task_id} worker-b --reclaim")

    assert "Reassigned" in out
    assert "claim reclaimed" not in out
