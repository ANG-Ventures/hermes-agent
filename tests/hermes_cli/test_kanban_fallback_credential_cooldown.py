"""Capped-pool fallback never spawns into a provider whose credential is cooling
(t_6445986b).

Incident 2026-09-25: the lane pool (claude-bpr) was budget-capped, the dispatch
fallback picked the profile's openai-codex rung, and every worker died at auth
with ``worker_route_pin_refused {rate_limited: true, reason: "Codex credential
is in cooldown."}`` -> exit 75 -> rate_limited close -> escalating backoff. A P0
card lost 30 minutes this way. A rate-limited refusal now marks the provider as
cooling for ``kanban.credential_cooldown_seconds``: it is skipped as a rung (and
as a route), and when no rung is left the card is HELD (deferred, no run).
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_pool_ratelimit_gates import (  # noqa: F401
    _Pool, _events, _running_run, _urls, home,
)


@pytest.fixture
def bpr():
    p = _Pool()
    yield p
    p.close()


def _config(home, **kanban):
    (home / "config.yaml").write_text(json.dumps({"kanban": {
        "pool_box_health": False, "pool_spawns_per_eligible": 2, **kanban,
    }}))


def _profile(home, fallbacks):
    p = home / "profiles" / "d"
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-bpr", "default": "opus"},
        "fallback_providers": [{"provider": prov, "model": m} for prov, m in fallbacks],
    }))


def _spawner(observed):
    def spawn(task, workspace, **kwargs):
        observed.append((task.id, task.provider_override, task.model_override))
        return 777777
    return spawn


def _refusal(conn, provider="openai-codex", *, age=60, rate_limited=True):
    """A closed run whose worker refused ``provider`` at auth ``age`` s ago."""
    now = int(time.time())
    tid = kb.create_task(conn, title="refused", assignee="d")
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'd', 'rate_limited', 'rate_limited', ?, ?)",
        (tid, now - age - 5, now - age))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, 'worker_route_pin_refused', ?, ?)",
        (tid, cur.lastrowid, json.dumps({
            "stage": "auth", "provider": provider, "model": "gpt-6-sol-900k",
            "rate_limited": rate_limited, "reason": "Codex credential is in cooldown.",
        }), now - age))
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.commit()


def _cap_bpr(conn, bpr):
    """bpr eligible=1 x 2 per eligible, two workers in flight -> pool_budget."""
    bpr.eligible = 1
    for i in range(2):
        _running_run(conn, kb.create_task(conn, title=f"busy-{i}", assignee="d"),
                     {"pool": "claude-bpr"})


def _dispatch(conn, seen):
    return kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100,
                            max_in_progress=100)


def test_capped_lane_and_cooling_fallback_holds_without_spawn_or_backoff(home, bpr):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [("openai-codex", "gpt-6-sol-900k")])
    with kb.connect_closing() as conn:
        _cap_bpr(conn, bpr)
        _refusal(conn)
        tid = kb.create_task(conn, title="P0", assignee="d", priority=5)
        seen: list = []
        res = _dispatch(conn, seen)
        assert seen == []
        assert (tid, "pool_budget") in res.respawn_guarded
        deferred = _events(conn, tid, "deferred")[-1]
        assert deferred["reason"] == "pool_budget"
        assert deferred["fallback_skipped"] == [{
            "provider": "openai-codex", "model": "gpt-6-sol-900k",
            "reason": "credential_cooldown",
        }]
        # The hold is not a run: no rate_limited close, no backoff stamp.
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?",
                            (tid,)).fetchone()[0] == 0
        assert kb.consecutive_rate_limited_runs(conn, tid) == 0
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.next_eligible_at is None
        # Held again next tick: still no backoff accumulates.
        _dispatch(conn, seen)
        assert seen == [] and kb.get_task(conn, tid).next_eligible_at is None
        assert kb.consecutive_rate_limited_runs(conn, tid) == 0


def test_healthy_fallback_still_spawns_on_fallback(home, bpr):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [("openai-codex", "gpt-6-sol-900k")])
    with kb.connect_closing() as conn:
        _cap_bpr(conn, bpr)
        tid = kb.create_task(conn, title="P0", assignee="d")
        seen: list = []
        res = _dispatch(conn, seen)
        assert seen == [(tid, "openai-codex", "gpt-6-sol-900k")]
        assert res.spawn_route_sources[tid] == "dispatch-fallback(capped profile-default)"


@pytest.mark.parametrize("age,rate_limited", [(1801, True), (60, False)])
def test_stale_or_non_rate_limited_refusal_does_not_cool(home, bpr, age, rate_limited):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [("openai-codex", "gpt-6-sol-900k")])
    with kb.connect_closing() as conn:
        _cap_bpr(conn, bpr)
        _refusal(conn, age=age, rate_limited=rate_limited)
        tid = kb.create_task(conn, title="P0", assignee="d")
        seen: list = []
        _dispatch(conn, seen)
        assert seen == [(tid, "openai-codex", "gpt-6-sol-900k")]


def test_zero_cooldown_disables(home, bpr):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url), credential_cooldown_seconds=0)
    _profile(home, [("openai-codex", "gpt-6-sol-900k")])
    with kb.connect_closing() as conn:
        _cap_bpr(conn, bpr)
        _refusal(conn)
        tid = kb.create_task(conn, title="P0", assignee="d")
        seen: list = []
        _dispatch(conn, seen)
        assert seen == [(tid, "openai-codex", "gpt-6-sol-900k")]


def test_cooling_rung_skipped_for_next_healthy_rung_and_named_in_route_source(home, bpr):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [("openai-codex", "gpt-6-sol-900k"), ("other", "m2")])
    with kb.connect_closing() as conn:
        _cap_bpr(conn, bpr)
        _refusal(conn)
        tid = kb.create_task(conn, title="P0", assignee="d")
        seen: list = []
        res = _dispatch(conn, seen)
        assert seen == [(tid, "other", "m2")]
        assert res.spawn_route_sources[tid] == (
            "dispatch-fallback(capped profile-default; "
            "skipped openai-codex:credential_cooldown)")
        assert _events(conn, tid, "dispatch_provider_fallback")[-1]["skipped"] == [{
            "provider": "openai-codex", "model": "gpt-6-sol-900k",
            "reason": "credential_cooldown",
        }]


def test_cooling_primary_route_is_held_not_spawned(home, bpr):
    """A card whose OWN route is the cooling provider holds instead of dying at auth."""
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [])
    with kb.connect_closing() as conn:
        _refusal(conn)
        tid = kb.create_task(conn, title="pinned", assignee="d")
        conn.execute(
            "UPDATE tasks SET model_override='gpt-6-sol-900k', "
            "provider_override='openai-codex' WHERE id=?", (tid,))
        conn.commit()
        seen: list = []
        res = _dispatch(conn, seen)
        assert seen == []
        assert (tid, "credential_cooldown") in res.respawn_guarded
        assert kb.get_task(conn, tid).next_eligible_at is None


def test_freed_pool_slot_goes_to_highest_priority_then_oldest(home, bpr):
    _config(home, pool_health_urls=_urls(bpr_url=bpr.url))
    _profile(home, [("openai-codex", "gpt-6-sol-900k")])
    with kb.connect_closing() as conn:
        bpr.eligible = 1
        _running_run(conn, kb.create_task(conn, title="busy", assignee="d"),
                     {"pool": "claude-bpr"})  # one of two slots free
        _refusal(conn)  # fallback cooling: only the freed slot is spawnable
        low_old = kb.create_task(conn, title="low-old", assignee="d", priority=0)
        conn.execute("UPDATE tasks SET created_at=created_at-100 WHERE id=?", (low_old,))
        high_a = kb.create_task(conn, title="high-a", assignee="d", priority=5)
        conn.execute("UPDATE tasks SET created_at=created_at-50 WHERE id=?", (high_a,))
        high_b = kb.create_task(conn, title="high-b", assignee="d", priority=5)
        conn.commit()
        seen: list = []
        res = _dispatch(conn, seen)
        assert [s[0] for s in seen] == [high_a]
        assert (high_b, "pool_budget") in res.respawn_guarded
        assert (low_old, "pool_budget") in res.respawn_guarded
