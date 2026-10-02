"""Pool-shed rung (t_9038e9f3): while a claude relay pool runs short of eligible
subs, low-priority cards spawn on their profile's non-pool rung (the other
vendor's seat) instead of the pool. Ace's rules: an exhausted seat means use the
other seat, no top-up, and no worker-concurrency cap. Also: ``claude-btpr`` (the
bpr relay in tui mode, the first rung of every claude worker chain) is judged on
the bpr relay's capacity.

Hermetic: loopback relays from the rate-limit-gates suite, real DB, real
production config loader. Mutation-checked (see the card handoff).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_provider_health as ph
from tests.hermes_cli.test_kanban_pool_ratelimit_gates import (  # noqa: F401  (fixtures)
    _Pool, _events, _spawner, _urls, apr, bpr, home,
)

SHED = [{"below_eligible": 10, "max_priority": 2}, {"below_eligible": 7, "max_priority": 60}]


def _config(home: Path, **kanban):
    (home / "config.yaml").write_text(json.dumps({"kanban": kanban}))


def _worker(home: Path, name="w", chain=("claude-btpr", "claude-bpr", "openai-codex")):
    p = home / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-alr", "default": "claude-opus-5-5"},
        "fallback_providers": [{"provider": c, "model": "gpt-6.1-sol" if c == "openai-codex"
                                else "claude-opus-5-5"} for c in chain],
    }))


def _dispatch(conn):
    seen: list = []
    kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=50, max_in_progress=50)
    return seen


def _shed_to(conn, tid):
    ev = _events(conn, tid, "dispatch_provider_fallback")
    return ev[-1] if ev else None


def test_btpr_is_the_bpr_relay():
    assert ph.pool_route("claude-btpr") == ("claude-bpr", None)
    assert ph.pool_key("claude-btpr") == "claude-bpr"


def test_configured_pool_shed_parsing(home):
    _config(home, pool_shed=SHED + [{"below_eligible": "x", "max_priority": 1}, "junk",
                                    {"below_eligible": 0, "max_priority": 5}])
    assert ph.configured_pool_shed() == [(7, 60), (10, 2)]
    _config(home)
    assert ph.configured_pool_shed() == []
    tiers = [(7, 60), (10, 2)]
    assert ph.shed_priority(tiers, 12) is None and ph.shed_priority(tiers, 10) is None
    assert ph.shed_priority(tiers, 9) == 2 and ph.shed_priority(tiers, 6) == 60
    assert ph.shed_priority(tiers, None) is None


@pytest.mark.parametrize("eligible,priority,shed", [
    (12, 0, False),    # pool healthy: nothing sheds
    (9, 0, True),      # tier 1: the low-priority tail sheds
    (9, 2, True),
    (9, 3, False),     # above the tier-1 priority: stays on the pool
    (6, 60, True),     # tier 2 widens the band
    (6, 61, False),
])
def test_shed_by_eligible_and_priority(home, apr, bpr, eligible, priority, shed):
    apr.eligible = bpr.eligible = eligible
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), pool_shed=SHED)
    _worker(home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w", priority=priority)
        assert _dispatch(conn) == [tid]          # always spawns: shedding never holds work
        ev = _shed_to(conn, tid)
        if shed:
            assert ev and ev["to_provider"] == "openai-codex" and ev["reason"] == "pool_shed"
            assert ev["pool"] == "claude-apr" and ev["eligible"] == eligible
        else:
            assert ev is None


def test_shed_off_without_config(home, apr, bpr):
    apr.eligible = bpr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _worker(home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        assert _dispatch(conn) == [tid] and _shed_to(conn, tid) is None


def test_no_non_pool_rung_means_no_shed(home, apr, bpr):
    """cadmus-style chains (claude rungs only) keep running on the pool."""
    apr.eligible = bpr.eligible = 3
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), pool_shed=SHED)
    _worker(home, chain=("claude-btpr", "claude-bpr"))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        assert _dispatch(conn) == [tid] and _shed_to(conn, tid) is None


def test_card_pin_is_never_shed(home, apr, bpr):
    apr.eligible = bpr.eligible = 3
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), pool_shed=SHED)
    _worker(home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        conn.execute("UPDATE tasks SET model_override='claude-opus-5-5', provider_override='claude-alr' "
                     "WHERE id=?", (tid,))
        conn.commit()
        assert _dispatch(conn) == [tid] and _shed_to(conn, tid) is None


def test_non_pool_primary_is_untouched(home, apr, bpr):
    apr.eligible = bpr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), pool_shed=SHED)
    p = home / "profiles" / "sol"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "openai-codex", "default": "gpt-6.1-sol"},
        "fallback_providers": [{"provider": "claude-alr", "model": "claude-opus-5-5"}]}))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="sol")
        assert _dispatch(conn) == [tid] and _shed_to(conn, tid) is None


def test_unknown_pool_health_fails_open(home):
    """No reachable relay = unknown eligible = no shed (never shed on a blind probe)."""
    _config(home, pool_health_urls=_urls("http://127.0.0.1:9/health", "http://127.0.0.1:9/health"),
            pool_shed=SHED)
    _worker(home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        assert _dispatch(conn) == [tid] and _shed_to(conn, tid) is None


def test_btpr_rung_judged_on_bpr_capacity(home, apr, bpr):
    """apr capped, bpr capped: the btpr rung used to be admitted blind (no pool
    route) and 429; now it is skipped and the card lands on the codex rung."""
    apr.eligible, bpr.eligible = 0, 0
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _worker(home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        assert _dispatch(conn) == [tid]
        ev = _shed_to(conn, tid)
        assert ev["to_provider"] == "openai-codex"
        assert {s["provider"] for s in ev["skipped"]} == {"claude-btpr", "claude-bpr"}
