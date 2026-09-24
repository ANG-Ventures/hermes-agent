"""Two-connection regressions for the round-8 route races (t_f08b7589).

Each test injects a REAL second SQLite connection at the exact boundary the
defect lived in (between a read and the write that trusted it), then checks
the DB readback AND the operator-facing receipt. Only the scheduling is
synthetic. Every test here failed on eaed38366 and passes after the fix.
"""
from contextlib import contextmanager
import inspect
import logging

from pathlib import Path

import pytest

from hermes_cli import kanban as kc, kanban_db as kb
from gateway.kanban_watchers import _log_dispatch_tick
from tests.hermes_cli.test_kanban_batch_set_model import _create


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_SANDBOX', '1')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert kb.kanban_db_path().resolve().is_relative_to(home.resolve())
    kb.init_db()
    (home / 'config.yaml').write_text(
        'providers:\n  batch-provider:\n    base_url: http://127.0.0.1:9999/v1\n    api_key: test\n'
    )
    return home


def _row(task_id):
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    return task.status, task.provider_override, task.model_override, task.reasoning_effort


def _after_selection(monkeypatch, action):
    """Run ``action`` after set-model selects its cards, before it writes."""
    original = kc._select_batch_tasks
    fired = {"n": 0}

    def hooked(conn, **kwargs):
        tasks, error = original(conn, **kwargs)
        if not error:
            fired["n"] += 1
            action()
        return tasks, error

    monkeypatch.setattr(kc, "_select_batch_tasks", hooked)
    return fired


def _before_lock_of(monkeypatch, func_name, action):
    """Run ``action`` on another connection just before ``func_name`` locks."""
    original = kb.write_txn
    fired = {"n": 0}

    @contextmanager
    def interleave(conn):
        if fired["n"] == 0 and any(f.function == func_name for f in inspect.stack()):
            fired["n"] += 1
            action()
        with original(conn):
            yield

    monkeypatch.setattr(kb, "write_txn", interleave)
    return fired


def _other_conn(fn):
    with kb.connect() as other:
        return fn(other)


# ---------------------------------------------------------------------------
# (b) the selection predicate is re-checked inside the batch transaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("effort_only", [False, True])
def test_status_selector_skips_cards_that_left_the_set_before_the_lock(
    kanban_home, monkeypatch, effort_only,
):
    keep = _create("keep", "worker")
    done = _create("done", "worker")
    archived = _create("archived", "worker")
    claimed = _create("claimed", "worker")

    def race():
        _other_conn(lambda c: kb.complete_task(c, done, result="finished"))
        _other_conn(lambda c: kb.archive_task(c, archived))
        _other_conn(lambda c: kb.claim_task(c, claimed, claimer="probe"))

    fired = _after_selection(monkeypatch, race)
    command = (
        "set-model --effort high --where status=ready assignee=worker"
        if effort_only else
        "set-model standard-model --provider batch-provider "
        "--where status=ready assignee=worker"
    )
    out = kc.run_slash(command)

    assert fired["n"] == 1
    if effort_only:
        assert _row(keep)[3] == "high"
        assert f"{keep}: effort=high" in out
    else:
        assert _row(keep)[1:3] == ("batch-provider", "standard-model")
        assert f"{keep}: route=batch-provider/standard-model" in out
    for task_id, status in ((done, "done"), (archived, "archived"), (claimed, "running")):
        assert _row(task_id)[1:] == (None, None, None), task_id
        assert f"{task_id}: skipped (status is now {status}" in out
        assert f"{task_id}: route=" not in out and f"{task_id}: effort=" not in out


def test_assignee_selector_skips_a_card_reassigned_before_the_lock(kanban_home, monkeypatch):
    keep = _create("keep", "worker")
    moved = _create("moved", "worker")
    _after_selection(monkeypatch, lambda: _other_conn(lambda c: kb.assign_task(c, moved, "other")))

    out = kc.run_slash("set-model standard-model --where assignee=worker")

    assert _row(keep)[2] == "standard-model"
    assert _row(moved)[2] is None
    assert f"{moved}: skipped (assignee is now other" in out


def test_all_active_skips_a_card_completed_before_the_lock(kanban_home, monkeypatch):
    keep = _create("keep", "worker")
    done = _create("done", "worker")
    _after_selection(
        monkeypatch, lambda: _other_conn(lambda c: kb.complete_task(c, done, result="x")),
    )

    out = kc.run_slash("set-model standard-model --all-active")

    assert _row(keep)[2] == "standard-model"
    assert _row(done)[:3] == ("done", None, None)
    assert f"{done}: skipped (status is now done" in out


def test_explicit_ids_abort_whole_batch_when_one_completes_before_the_lock(
    kanban_home, monkeypatch,
):
    first = _create("first", "worker")
    second = _create("second", "worker")
    _after_selection(
        monkeypatch, lambda: _other_conn(lambda c: kb.complete_task(c, second, result="x")),
    )

    out = kc.run_slash(f"set-model {first} {second} standard-model")

    assert f"{second}: status is now done; no cards were changed" in out
    assert _row(first)[2] is None and _row(second)[2] is None


def test_selector_where_every_card_left_is_a_failure_not_a_receipt(kanban_home, monkeypatch):
    only = _create("only", "worker")
    _after_selection(
        monkeypatch, lambda: _other_conn(lambda c: kb.complete_task(c, only, result="x")),
    )
    code = kc.kanban_command(
        kc.build_parser(
            __import__("argparse").ArgumentParser().add_subparsers(dest="_t")
        ).parse_args("set-model standard-model --where status=ready".split())
    )
    assert code == 1
    assert _row(only)[2] is None


# ---------------------------------------------------------------------------
# (a) main's flagship policy runs on the EFFECTIVE route at dispatch
# ---------------------------------------------------------------------------

def _ban(home, *parts):
    text = (home / "config.yaml").read_text()
    text += "kanban:\n  banned_worker_model_substrings: [" + ", ".join(parts) + "]\n"
    (home / "config.yaml").write_text(text)


def _spawn_log():
    seen = []

    def spawn(task, workspace, *, board=None):
        seen.append((task.id, task.provider_override, task.model_override))
        return 4242

    return seen, spawn


def test_lane_route_banned_after_it_was_set_is_refused_on_the_ready_path(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="gpt-5.6-sol",
            expires_at=10**10, reason="capacity",
        )
        lane_card = kb.create_task(conn, title="lane", assignee="worker")
    _ban(kanban_home, "sol")  # policy tightened AFTER the lane was installed
    seen, spawn = _spawn_log()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        comments = [c.body for c in kb.list_comments(conn, lane_card)]

    assert seen == []
    assert result.flagship_refused == [lane_card]
    assert any("lane-model override" in c and "gpt-5.6-sol" in c for c in comments)


def test_lane_route_banned_after_it_was_set_is_refused_on_the_review_path(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="gpt-5.6-sol",
            expires_at=10**10, reason="capacity",
        )
        card = kb.create_task(conn, title="review", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (card,))
    _ban(kanban_home, "sol")
    seen, spawn = _spawn_log()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert seen == []
    assert result.flagship_refused == [card]


def test_authorized_lane_and_authorized_card_still_spawn(kanban_home, all_assignees_spawnable):
    _ban(kanban_home, "sol")
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="gpt-5.6-sol",
            expires_at=10**10, reason="capacity", firepower="drill: capped pool",
        )
        lane_card = kb.create_task(conn, title="lane", assignee="worker")
        card = kb.create_task(conn, title="card", assignee="worker")
        assert kb.set_model_override(
            conn, card, "gpt-5.6-sol", provider="openai-codex",
            flagship_override_reason="drill: capped pool",
        )
    seen, spawn = _spawn_log()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert result.flagship_refused == []
    assert sorted(seen) == sorted([
        (lane_card, "openai-codex", "gpt-5.6-sol"),
        (card, "openai-codex", "gpt-5.6-sol"),
    ])


def test_card_pin_still_beats_a_banned_lane(kanban_home, all_assignees_spawnable):
    """Precedence is unchanged: a standard card pin never inherits the lane ban."""
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="gpt-5.6-sol",
            expires_at=10**10, reason="capacity",
        )
        card = kb.create_task(conn, title="card", assignee="worker")
        assert kb.set_model_override(conn, card, "standard-model")
    _ban(kanban_home, "sol")
    seen, spawn = _spawn_log()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert result.flagship_refused == []
    assert seen == [(card, None, "standard-model")]


# ---------------------------------------------------------------------------
# (c) lane read-then-write happens in one writer transaction
# ---------------------------------------------------------------------------

def test_clear_racing_a_replacement_reports_the_row_it_actually_deleted(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-old",
            expires_at=10**10, reason="old",
        )

    def replace():
        _other_conn(lambda c: kb.set_lane_model_override(
            c, provider="openai-codex", model="model-new",
            expires_at=10**10, reason="new",
        ))

    fired = _before_lock_of(monkeypatch, "clear_lane_model_override", replace)
    out = kc.run_slash("lane-model clear")

    assert fired["n"] == 1
    assert "was openai-codex/model-new" in out and "model-old" not in out
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn, include_expired=True) == []


def test_expiry_racing_a_renewal_neither_deletes_nor_announces_it(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-old",
            expires_at=100, reason="old", now=50,
        )

    def renew():
        _other_conn(lambda c: kb.set_lane_model_override(
            c, provider="openai-codex", model="model-renewed",
            expires_at=500, reason="renewed", now=100,
        ))

    fired = _before_lock_of(monkeypatch, "expire_lane_model_overrides", renew)
    with kb.connect() as conn:
        expired = kb.expire_lane_model_overrides(conn, now=100)
        active = kb.get_lane_model_override(conn, now=100)

    assert fired["n"] == 1
    assert expired == []
    assert active is not None and active.route == "openai-codex/model-renewed"


def test_scoped_expiry_under_a_live_board_wide_lane_names_the_survivor(
    kanban_home, monkeypatch, all_assignees_spawnable, caplog,
):
    """Deterministic overlapping-lane control: route precedence is unchanged."""
    seen, spawn = _spawn_log()
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-global",
            expires_at=1000, reason="global", now=50,
        )
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-scoped",
            expires_at=100, reason="scoped", assignee="worker", now=50,
        )
        card = kb.create_task(conn, title="lane", assignee="worker")
        monkeypatch.setattr(kb.time, "time", lambda: 100)
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert result.expired_lane_models == [("worker", "openai-codex/model-scoped")]
    assert result.expired_lane_successors == {
        "worker": "board-wide lane openai-codex/model-global",
    }
    assert seen == [(card, "openai-codex", "model-global")]
    logger = logging.getLogger("test.f08.expiry")
    with caplog.at_level(logging.INFO, logger="test.f08.expiry"):
        _log_dispatch_tick(logger, "scratch", result)
    assert ("lane-model expired -> board-wide lane openai-codex/model-global "
            "(worker: openai-codex/model-scoped)") in caplog.text
    assert "-> profile default" not in caplog.text


def test_sole_lane_expiry_still_says_profile_default(
    kanban_home, monkeypatch, all_assignees_spawnable, caplog,
):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-old",
            expires_at=100, reason="only", now=50,
        )
        monkeypatch.setattr(kb.time, "time", lambda: 100)
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    logger = logging.getLogger("test.f08.expiry2")
    with caplog.at_level(logging.INFO, logger="test.f08.expiry2"):
        _log_dispatch_tick(logger, "scratch", result)
    assert "lane-model expired -> profile default (*: openai-codex/model-old)" in caplog.text


def test_clearing_a_scoped_lane_under_a_board_wide_lane_names_the_survivor(kanban_home):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-global",
            expires_at=10**10, reason="global",
        )
        kb.set_lane_model_override(
            conn, provider="openai-codex", model="model-scoped",
            expires_at=10**10, reason="scoped", assignee="worker",
        )
    out = kc.run_slash("lane-model clear --assignee worker")
    assert "was openai-codex/model-scoped" in out
    assert "now routes via board-wide lane openai-codex/model-global" in out
