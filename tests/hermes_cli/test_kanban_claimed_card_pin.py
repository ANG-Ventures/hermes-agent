"""The worker's card pin is the pin as CLAIMED, not a later row read (t_a30417c3).

FleetReview P1 on #1446: the worker snapshotted its card pin by reading the
mutable card row. A ``set-model`` without ``--live`` landing between the
dispatcher's spawn and the worker's first card read (seconds) became the
in-flight run's pin. The dispatcher now hands the claim-time row pin to the
worker in the spawn env; a lane override or a capped-pool rung is never
passed as a card pin (t_16642ede).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_route as kwr


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.delenv(kwr.CLAIMED_CARD_PIN_ENV, raising=False)
    kwr._card_pin_cache.clear()
    yield tmp_path
    kwr._card_pin_cache.clear()


def _claim(**overrides):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="a", **overrides)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        claimed = kb.claim_task(conn, tid, ttl_seconds=600)
    assert claimed is not None
    return claimed


def _spawn_env(task, workspace, monkeypatch):
    captured = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace.mkdir(parents=True, exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    return captured["env"]


def _set_model(tid, model, provider):
    # What ``hermes kanban set-model`` (no --live) does: rewrite the row.
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                     (model, provider, tid))
        conn.commit()


def _become_worker(env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", env["HERMES_KANBAN_TASK"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", env["HERMES_KANBAN_RUN_ID"])
    if kwr.CLAIMED_CARD_PIN_ENV in env:
        monkeypatch.setenv(kwr.CLAIMED_CARD_PIN_ENV, env[kwr.CLAIMED_CARD_PIN_ENV])


def test_row_change_after_spawn_does_not_move_the_claimed_pin(home, monkeypatch):
    claimed = _claim(model_override="gpt-6-sol-900k", provider_override="openai-codex")
    env = _spawn_env(claimed, home / "ws", monkeypatch)
    assert json.loads(env[kwr.CLAIMED_CARD_PIN_ENV]) == {
        "model": "gpt-6-sol-900k", "provider": "openai-codex"}

    # set-model for the NEXT dispatch lands after spawn, before the worker's
    # first card read.
    _set_model(claimed.id, "claude-opus-5-5", "claude-bpr")
    _become_worker(env, monkeypatch)

    assert kwr.card_pinned_route() == ("gpt-6-sol-900k", "openai-codex")
    agent = SimpleNamespace(provider="openai-codex", model="gpt-6-sol-900k",
                            _primary_runtime={"provider": "openai-codex",
                                              "model": "gpt-6-sol-900k"})
    # The claimed pin is enforced: the new row's route is still a refused
    # failover target for this run.
    assert kwr.refuse_runtime_failover(agent, "claude-bpr", "claude-opus-5-5") is True


def test_unpinned_claim_stays_unpinned_when_row_is_pinned_after_spawn(home, monkeypatch):
    claimed = _claim()
    env = _spawn_env(claimed, home / "ws", monkeypatch)
    assert json.loads(env[kwr.CLAIMED_CARD_PIN_ENV]) == {"model": None, "provider": None}

    _set_model(claimed.id, "gpt-6-sol-900k", "openai-codex")
    _become_worker(env, monkeypatch)

    assert kwr.card_pinned_route() == (None, None)


def test_lane_override_and_rung_are_not_passed_as_card_pin(home, monkeypatch):
    claimed = _claim()
    lane = kb.LaneModelOverride(assignee="a", provider="claude-bpr", model="claude-opus-5-5")
    assert kb.apply_lane_model_override(claimed, lane) is not None
    assert claimed.model_override == "claude-opus-5-5"  # in-memory route moved
    env = _spawn_env(claimed, home / "ws", monkeypatch)
    assert json.loads(env[kwr.CLAIMED_CARD_PIN_ENV]) == {"model": None, "provider": None}


def test_older_dispatcher_without_env_falls_back_to_row(home, monkeypatch):
    claimed = _claim(model_override="gpt-6-sol-900k", provider_override="openai-codex")
    env = _spawn_env(claimed, home / "ws", monkeypatch)
    env.pop(kwr.CLAIMED_CARD_PIN_ENV)
    _become_worker(env, monkeypatch)
    assert kwr.card_pinned_route() == ("gpt-6-sol-900k", "openai-codex")


def test_unclaimed_task_spawn_does_not_leak_an_inherited_pin(home, monkeypatch):
    claimed = _claim(model_override="gpt-6-sol-900k", provider_override="openai-codex")
    claimed.claimed_card_pin = None  # a Task that was never claimed here
    monkeypatch.setenv(kwr.CLAIMED_CARD_PIN_ENV, json.dumps({"model": "x", "provider": "y"}))
    env = _spawn_env(claimed, home / "ws", monkeypatch)
    assert kwr.CLAIMED_CARD_PIN_ENV not in env
