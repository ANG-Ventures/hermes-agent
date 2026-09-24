"""Rate-limit storm brakes for the kanban dispatcher (t_32f44156).

Measured 2026-09-22: 3,706 ``rate_limited`` runs in 7d; 42% ran >2 min
(boot, load skills + card + repo, make real calls) before dying at 429 with
no output. Three brakes, each tested here and each mutation-checked:

1. POOL-HEALTH ADMISSION -- each claude worker is admitted on the capacity of
   the pool that will actually SERVE it (Argus r1, PR #953):
   claude-apr -> the apr relay's ``/health`` aggregate (:18810),
   claude-bpr -> the bpr relay's aggregate (:18811),
   claude-apx-N / claude-bpx-N -> ONE sub box (``local`` for N=0, else
   ``sub-vps-N``); held only when that sub is listed ``exhausted`` /
   ``capped_quota`` by its family's relay (apx->apr, bpx->bpr). Unlisted = admit.
   ``kanban.pool_health_urls`` maps relay family -> URL; an explicit
   ``provider_health_probes`` entry still wins. Probe failure fails OPEN.
2. ESCALATING PER-TASK BACKOFF -- consecutive rate_limited runs hold the task
   for 5m, 15m, 45m, then 2h (cap); any non-rate_limited run resets it.
3. TICK CIRCUIT -- >= ``kanban.rate_limit_trip`` rate_limited closes OF ONE POOL
   inside a 10-minute window hold that pool's spawns for 10 minutes; notified
   once per pool trip. Non-pool (e.g. openai-codex) closes never count.

Hermetic: a real loopback HTTP server stands in for the pool, run rows carry
explicit timestamps relative to a single ``now``.
"""
from __future__ import annotations

import http.server
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_provider_health as ph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Pool:
    """Loopback fake of the relay pool's GET /health."""

    def __init__(self):
        self.eligible = 3
        self.exhausted: list = []
        self.capped_quota: list = []
        self.hits = 0
        pool = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                pool.hits += 1
                body = json.dumps({
                    "status": "ok" if pool.eligible else "all_capped",
                    "eligible_count": pool.eligible, "pool_size": 15,
                    "exhausted": pool.exhausted, "capped_quota": pool.capped_quota,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/health"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def pool():
    p = _Pool()
    yield p
    p.close()


@pytest.fixture
def apr():
    p = _Pool()
    yield p
    p.close()


@pytest.fixture
def bpr():
    p = _Pool()
    yield p
    p.close()


def _urls(apr_url=None, bpr_url=None) -> dict:
    """``kanban.pool_health_urls`` with BOTH families explicit (never the
    production loopback defaults, which a test host may really be serving)."""
    return {"claude-apr": apr_url or "", "claude-bpr": bpr_url or ""}


def _dead_url() -> str:
    """A loopback URL nothing listens on (bind, read the port, close)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/health"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_USAGE_REGISTRY", raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    getattr(ph, "_PROBE_STATE", {}).clear()
    kb.init_db()
    yield h
    getattr(ph, "_PROBE_STATE", {}).clear()


def _config(home: Path, **kanban):
    (home / "config.yaml").write_text(json.dumps({"kanban": kanban}))


def _profile(home: Path, name: str, provider: str):
    p = home / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": provider, "default": "m"},
    }))


def _spawner(observed):
    def spawn(task, workspace, **kwargs):
        observed.append(task.id)
        return 777777
    return spawn


def _runs(conn, tid, outcomes_and_ended):
    """Insert closed runs, oldest first: [(outcome, ended_at), ...]."""
    for outcome, ended in outcomes_and_ended:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'a', ?, ?, ?, ?)",
            (tid, outcome, outcome, ended - 60, ended),
        )
    conn.commit()


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]


# ---------------------------------------------------------------------------
# Gate 1 -- pool-health admission
# ---------------------------------------------------------------------------


def test_pool_health_urls_default_and_config_via_production_loader(home):
    assert ph.configured_pool_health_urls() == {
        "claude-apr": "http://127.0.0.1:18810/health",
        "claude-bpr": "http://127.0.0.1:18811/health",
    }
    _config(home, pool_health_urls={"claude-bpr": "http://127.0.0.1:9/h"})
    # A partial map overrides only the named family.
    assert ph.configured_pool_health_urls() == {
        "claude-apr": "http://127.0.0.1:18810/health",
        "claude-bpr": "http://127.0.0.1:9/h",
    }
    _config(home, pool_health_urls={"claude-apr": ""})
    assert ph.configured_pool_health_urls()["claude-apr"] == ""
    _config(home, pool_health_urls="junk")
    assert ph.configured_pool_health_urls()["claude-apr"] == "http://127.0.0.1:18810/health"


@pytest.mark.parametrize("provider,key", [
    ("claude-apr", ("claude-apr", None)),
    ("claude-bpr", ("claude-bpr", None)),
    ("claude-apx-0", ("claude-apr", "local")),
    ("claude-apx-7", ("claude-apr", "sub-vps-7")),
    ("claude-bpx-22", ("claude-bpr", "sub-vps-22")),
    ("CLAUDE-BPX-10", ("claude-bpr", "sub-vps-10")),
    ("openai-codex", None), ("claude-bpx-x", None), (None, None),
])
def test_pool_route(provider, key):
    assert ph.pool_route(provider) == key


def _task(provider):
    from types import SimpleNamespace
    return SimpleNamespace(model_override="m", provider_override=provider, assignee="a")


def test_relay_provider_capped_on_its_own_relay(home, pool):
    pool.eligible = 0
    got = ph.capped_provider(_task("claude-bpr"), {}, {}, pool_urls=_urls(bpr_url=pool.url))
    assert got is not None and got["provider"] == "claude-bpr"


def test_pool_provider_admitted_when_eligible(home, pool):
    pool.eligible = 2
    assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_urls=_urls(pool.url)) is None


def test_min_eligible_threshold(home, pool):
    pool.eligible = 2
    assert ph.capped_provider(
        _task("claude-apr"), {}, {}, pool_urls=_urls(pool.url), min_eligible=3,
    ) is not None


def test_non_pool_provider_never_probes(home, pool):
    pool.eligible = 0
    assert ph.capped_provider(
        _task("openai-codex"), {}, {}, pool_urls=_urls(pool.url, pool.url)) is None
    assert pool.hits == 0


def test_empty_family_url_disables(home, pool):
    pool.eligible = 0
    assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_urls=_urls(None, pool.url)) is None
    assert pool.hits == 0


def test_explicit_probe_entry_wins_over_pool_urls(home, pool):
    pool.eligible = 0
    probes = {"claude-apr": _dead_url()}
    # The explicit (dead) URL is used, so the gate fails open rather than
    # reading the capped family relay.
    assert ph.capped_provider(_task("claude-apr"), probes, {}, pool_urls=_urls(pool.url)) is None
    assert pool.hits == 0


# --- pinned single-box lanes (claude-apx-N / claude-bpx-N) -----------------


def test_pinned_lane_held_only_when_its_own_sub_exhausted(home, apr, bpr):
    apr.eligible = bpr.eligible = 0  # relay aggregates say NOTHING about box 22
    bpr.exhausted = ["sub-vps-3"]
    urls = _urls(apr.url, bpr.url)
    assert ph.capped_provider(_task("claude-bpx-22"), {}, {}, pool_urls=urls) is None
    bpr.exhausted = ["sub-vps-22"]
    got = ph.capped_provider(_task("claude-bpx-22"), {}, {}, pool_urls=urls)
    assert got is not None and got["sub"] == "sub-vps-22"


def test_pinned_lane_held_when_sub_quota_capped(home, bpr):
    bpr.eligible = 9
    bpr.capped_quota = ["sub-vps-10"]
    assert ph.capped_provider(
        _task("claude-bpx-10"), {}, {}, pool_urls=_urls(None, bpr.url)) is not None


def test_pinned_apx_zero_maps_to_local_on_apr_relay(home, apr, bpr):
    apr.exhausted = ["local"]
    urls = _urls(apr.url, bpr.url)
    assert ph.capped_provider(_task("claude-apx-0"), {}, {}, pool_urls=urls) is not None
    # The bpx family reads the bpr relay, which does not list local.
    assert ph.capped_provider(_task("claude-bpx-0"), {}, {}, pool_urls=urls) is None
    assert bpr.hits == 1


def test_pinned_lane_admitted_when_relay_healthy_but_sub_exhausted_is_other(home, apr):
    apr.eligible = 5
    apr.exhausted = ["sub-vps-4"]
    assert ph.capped_provider(_task("claude-apx-5"), {}, {}, pool_urls=_urls(apr.url)) is None


def test_pinned_lane_fails_open_when_relay_unreachable(home):
    assert ph.capped_provider(
        _task("claude-bpx-22"), {}, {}, pool_urls=_urls(None, _dead_url())) is None


def test_unreachable_fails_open_and_warns_once(home, caplog):
    url = _dead_url()
    caplog.set_level(logging.DEBUG, logger="hermes_cli.kanban_provider_health")
    for _ in range(3):
        assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_urls=_urls(url)) is None
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and url in r.getMessage()]
    assert len(warns) == 1


def test_capped_state_logged_once_per_transition(home, pool, caplog):
    caplog.set_level(logging.INFO, logger="hermes_cli.kanban_provider_health")
    pool.eligible = 0
    for _ in range(3):
        ph.capped_provider(_task("claude-apr"), {}, {}, pool_urls=_urls(pool.url))
    pool.eligible = 4
    for _ in range(3):
        ph.capped_provider(_task("claude-apr"), {}, {}, pool_urls=_urls(pool.url))
    lines = [r.getMessage() for r in caplog.records if pool.url in r.getMessage()]
    assert len(lines) == 2, lines


def test_dispatch_holds_pool_worker_when_pool_empty(home, pool):
    pool.eligible = 0
    _config(home, pool_health_urls=_urls(bpr_url=pool.url))
    _profile(home, "a", "claude-bpr")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pool bound", assignee="a")
        observed = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == []
        assert (tid, "provider_capped") in res.respawn_guarded
        pool.eligible = 3
        res = kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [tid]


def test_dispatch_non_pool_worker_unaffected_by_empty_pool(home, pool):
    pool.eligible = 0
    _config(home, pool_health_urls=_urls(pool.url, pool.url))
    _profile(home, "b", "openai-codex")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex", assignee="b")
        observed = []
        kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [tid]
        assert pool.hits == 0


def test_dispatch_fails_open_when_pool_unreachable(home):
    _config(home, pool_health_urls=_urls(_dead_url()))
    _profile(home, "a", "claude-apr")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pool bound", assignee="a")
        observed = []
        kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [tid]


# --- Argus r1 two-pool cases, through the real dispatch_once ----------------


def _dispatch_one(assignee):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee=assignee)
        seen: list = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        return tid, seen, res


def test_A_bpr_worker_held_when_bpr_empty_even_if_apr_healthy(home, apr, bpr):
    apr.eligible, bpr.eligible = 5, 0
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "daedalus-opus", "claude-bpr")
    tid, seen, res = _dispatch_one("daedalus-opus")
    assert seen == []
    assert (tid, "provider_capped") in res.respawn_guarded
    assert bpr.hits == 1 and apr.hits == 0


def test_B_bpr_worker_spawns_when_bpr_healthy_even_if_apr_empty(home, apr, bpr):
    apr.eligible, bpr.eligible = 0, 8
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "daedalus-opus", "claude-bpr")
    tid, seen, _ = _dispatch_one("daedalus-opus")
    assert seen == [tid]
    assert apr.hits == 0


def test_C_pinned_box_ignores_relay_aggregate(home, apr, bpr):
    apr.eligible, bpr.eligible = 0, 0
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "pin", "claude-bpx-22")
    tid, seen, _ = _dispatch_one("pin")
    assert seen == [tid]  # both relays empty, box 22 not listed -> admitted


def test_C2_pinned_box_held_on_its_own_exhaustion_while_relay_healthy(home, apr, bpr):
    apr.eligible, bpr.eligible = 9, 9
    bpr.exhausted = ["sub-vps-22"]
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "pin", "claude-bpx-22")
    tid, seen, res = _dispatch_one("pin")
    assert seen == []
    assert (tid, "provider_capped") in res.respawn_guarded


def test_fallback_rung_judged_on_its_own_pool(home, apr, bpr):
    """CLASS-SWEEP: available_profile_fallback must consult each rung's own
    pool, so a capped bpr card falls back to a healthy apr rung but never onto
    an exhausted pinned box."""
    apr.eligible, bpr.eligible = 4, 0
    apr.exhausted = ["sub-vps-16"]
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    p = home / "profiles" / "fb"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-bpr", "default": "m"},
        "fallback_providers": [
            {"provider": "claude-apx-16", "model": "m16"},
            {"provider": "claude-apr", "model": "mapr"},
        ],
    }))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="fb")
        seen: list = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [tid]
        ev = _events(conn, tid, "dispatch_provider_fallback")
        assert ev and ev[-1]["to_provider"] == "claude-apr"


@pytest.mark.parametrize("reviews", [0, 15, 30])
def test_single_tick_pool_budget_across_ready_and_review(home, apr, reviews):
    """Real DB + loopback relay: 30 claims may not all spend one eligible seat."""
    apr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url), pool_spawns_per_eligible=2)
    _profile(home, "argus", "claude-apr")
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"burst-{i}", assignee="argus") for i in range(30)]
        if reviews:
            conn.executemany("UPDATE tasks SET status='review' WHERE id=?",
                             [(tid,) for tid in ids[-reviews:]])
            conn.commit()
        seen = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100,
                               max_in_progress=100)
        assert len(seen) == 2
        assert len([tid for tid, reason in res.respawn_guarded if reason == "pool_budget"]) == 28
        for tid in ids:
            if tid not in seen:
                assert _events(conn, tid, "deferred")[-1] == {
                    "reason": "pool_budget", "provider": "claude-apr",
                    "pool": "claude-apr", "eligible": 1, "admitted": 2,
                }
        assert apr.hits == 1


def test_pool_budget_is_per_tick_across_boards_with_shared_tick_cache(home, apr):
    """Argus r1 F1: the gateway tick calls dispatch_once once PER BOARD with one
    shared ``budget_cache``. The relay pool is shared by every board, so the
    admission budget must span the whole tick, not reset per board."""
    apr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url), pool_box_health=False,
            pool_spawns_per_eligible=2)
    _profile(home, "argus", "claude-apr")
    boards = ["default", "b2", "b3"]
    for b in boards[1:]:
        kb.create_board(b)
    ids: dict = {}
    for b in boards:
        with kb.connect_closing(board=b) as conn:
            ids[b] = [kb.create_task(conn, title=f"{b}-{i}", assignee="argus")
                      for i in range(10)]
    tick_cache: dict = {}
    seen: list = []
    for b in boards:
        with kb.connect_closing(board=b) as conn:
            kb.dispatch_once(conn, board=b, spawn_fn=_spawner(seen), max_spawn=100,
                             max_in_progress=100, budget_cache=tick_cache)
    assert len(seen) == 2, seen
    assert set(seen) <= set(ids["default"])
    for b in boards[1:]:
        with kb.connect_closing(board=b) as conn:
            for tid in ids[b]:
                assert _events(conn, tid, "deferred")[-1] == {
                    "reason": "pool_budget", "provider": "claude-apr",
                    "pool": "claude-apr", "eligible": 1, "admitted": 2,
                }
    # One probe per tick, not one per board.
    assert apr.hits == 1

    # A NEW tick (fresh cache) gets a fresh budget.
    seen.clear()
    with kb.connect_closing(board="b2") as conn:
        kb.dispatch_once(conn, board="b2", spawn_fn=_spawner(seen), max_spawn=100,
                         max_in_progress=100, budget_cache={})
    assert len(seen) == 2


def test_pool_budget_single_call_without_tick_cache_is_per_call(home, apr):
    """CLI / standalone daemon pass no cache: each call is its own tick."""
    apr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url), pool_box_health=False,
            pool_spawns_per_eligible=2)
    _profile(home, "argus", "claude-apr")
    with kb.connect_closing() as conn:
        for i in range(6):
            kb.create_task(conn, title=f"c-{i}", assignee="argus")
        seen: list = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert len(seen) == 2
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert len(seen) == 4


def test_pool_budget_config_default_and_legacy_zero(home):
    assert ph.configured_pool_spawns_per_eligible() == 2
    _config(home, pool_spawns_per_eligible=0)
    assert ph.configured_pool_spawns_per_eligible() == 0
    _config(home, pool_spawns_per_eligible=-1)
    assert ph.configured_pool_spawns_per_eligible() == 2


def test_fallback_spawns_charge_serving_pool_budget(home, apr, bpr):
    apr.eligible = bpr.eligible = 1
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), pool_spawns_per_eligible=2)
    p = home / "profiles" / "fb"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-apr", "default": "m"},
        "fallback_providers": [{"provider": "claude-bpr", "model": "mbpr"}],
    }))
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"fallback-{i}", assignee="fb") for i in range(10)]
        seen = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100,
                               max_in_progress=100)
        assert seen == ids[:4]
        assert all(not _events(conn, tid, "dispatch_provider_fallback") for tid in ids[:2])
        assert all(_events(conn, tid, "dispatch_provider_fallback")[-1]["to_provider"] == "claude-bpr"
                   for tid in ids[2:4])
        assert all((tid, "pool_budget") in res.respawn_guarded for tid in ids[4:])
        assert apr.hits == bpr.hits == 1


def test_pinned_lanes_share_sub_budget_and_zero_disables_it(home, apr):
    apr.eligible = 5
    _config(home, pool_health_urls=_urls(apr.url, apr.url), pool_box_health=False,
            pool_spawns_per_eligible=2)
    _profile(home, "apx", "claude-apx-16")
    _profile(home, "bpx", "claude-bpx-16")
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"pinned-{i}", assignee="apx" if i % 2 else "bpx")
               for i in range(6)]
        seen = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert len(seen) == 2
        assert _events(conn, ids[2], "deferred")[-1]["pool"] == "sub-vps-16"
        _config(home, pool_health_urls=_urls(apr.url, apr.url), pool_box_health=False,
                pool_spawns_per_eligible=0)
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert len(seen) == 6


def test_unreachable_probe_fails_open_even_with_pool_budget(home):
    _config(home, pool_health_urls=_urls(_dead_url()), pool_spawns_per_eligible=2)
    _profile(home, "a", "claude-apr")
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"unreachable-{i}", assignee="a") for i in range(5)]
        seen = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert seen == ids


def test_unreachable_pinned_probe_fails_open_without_box_health(home):
    _config(home, pool_health_urls=_urls(_dead_url()), pool_box_health=False,
            pool_spawns_per_eligible=2)
    _profile(home, "a", "claude-apx-16")
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"pin-unreachable-{i}", assignee="a")
               for i in range(5)]
        seen = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen), max_spawn=100, max_in_progress=100)
        assert seen == ids


# ---------------------------------------------------------------------------
# Gate 2 -- escalating per-task backoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streak,hold", [
    (0, 300), (1, 300), (2, 900), (3, 2700), (4, 7200), (9, 7200),
])
def test_backoff_ladder(streak, hold):
    assert kb._rate_limit_hold_seconds(streak, base=300) == hold


def test_backoff_never_below_configured_base():
    assert kb._rate_limit_hold_seconds(2, base=1200) == 1200


def test_backoff_zero_base_disables():
    assert kb._rate_limit_hold_seconds(4, base=0) == 0


def test_guard_escalates_on_consecutive_rate_limits(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        # Three in a row; the latest ended 1000s ago. Flat 300s would admit it.
        _runs(conn, tid, [("rate_limited", now - 5000), ("rate_limited", now - 3000),
                          ("rate_limited", now - 1000)])
        detail: dict = {}
        assert kb.check_respawn_guard(conn, tid, detail=detail) == "rate_limit_cooldown"
        assert detail["eligible_at"] == now - 1000 + 2700
        assert detail["backoff_streak"] == 3


def test_guard_backoff_resets_after_other_outcome(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        _runs(conn, tid, [("rate_limited", now - 9000), ("rate_limited", now - 8000),
                          ("crashed", now - 7000), ("rate_limited", now - 400)])
        assert kb.consecutive_rate_limited_runs(conn, tid) == 1
        assert kb.check_respawn_guard(conn, tid) is None


def test_consecutive_count_ignores_open_runs(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        _runs(conn, tid, [("rate_limited", now - 900), ("rate_limited", now - 600)])
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
            (tid, now),
        )
        conn.commit()
        assert kb.consecutive_rate_limited_runs(conn, tid) == 2


def test_release_stamps_escalated_next_eligible_at(home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    now = int(time.time())
    kb._recent_worker_exits.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        _runs(conn, tid, [("rate_limited", now - 4000), ("rate_limited", now - 3000)])
        task = kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 99999999)
        path = kb.kanban_db_path().parent / "runs" / f"{tid}.{task.current_run_id}.exit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"exit_code": 75, "failure_reason": "rate_limit",
                                    "ts": time.time()}))
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        kb.detect_crashed_workers(conn)
        current = kb.get_task(conn, tid)
        # Third consecutive rate-limit -> 45 min.
        assert current.next_eligible_at >= now + 2700 - 5
        payload = _events(conn, tid, "rate_limited")[-1]
        assert payload["next_eligible_at"] == current.next_eligible_at
    kb._recent_worker_exits.clear()


def test_diag_shows_backoff_hold():
    now = 100_000
    task = {"id": "t_x", "title": "x", "assignee": "demo", "status": "ready",
            "claim_lock": None}
    events = [
        {"kind": "created", "created_at": now - 45 * 60, "payload": None},
        {"kind": "respawn_guarded", "created_at": now - 60, "payload": {
            "reason": "rate_limit_cooldown", "eligible_at": now + 2000,
            "backoff_streak": 3}},
    ]
    stranded = [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
                if d.kind == "stranded_in_ready"][0]
    assert "held: rate_limit_backoff until " in stranded.detail
    assert stranded.data["held_until"] == now + 2000


# ---------------------------------------------------------------------------
# Gate 3 -- tick-level circuit
# ---------------------------------------------------------------------------


def _rl_burst(conn, ends, provider="claude-apr"):
    """A finished card whose runs closed rate_limited at ``ends`` on ``provider``."""
    tid = kb.create_task(conn, title="storm", assignee="z", model_override="m",
                         provider_override=provider)
    conn.execute("UPDATE tasks SET status='done', completed_at=strftime('%s','now') WHERE id=?", (tid,))
    _runs(conn, tid, [("rate_limited", e) for e in ends])
    return tid


def test_circuit_opens_on_trip_within_window(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        ends = [now - 400, now - 300, now - 200, now - 100, now - 60]
        _rl_burst(conn, ends)
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"claude-apr": now - 60 + 600}


def test_circuit_stays_open_ten_minutes_after_trip(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        # Tripped 9 min ago; the window has since emptied but the hold stands.
        t = now - 540
        _rl_burst(conn, [t - 200, t - 150, t - 100, t - 50, t])
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"claude-apr": t + 600}


def test_circuit_closed_below_trip(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 300, now - 200, now - 100, now - 60])
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}


def test_circuit_closed_when_spread_beyond_window(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 2400, now - 1800, now - 1200, now - 601, now - 10])
        # Five closes, but no five of them within any 600s span.
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}


def test_circuit_trip_zero_disables(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 50] * 8)
        assert kb.rate_limit_circuits(conn, now=now, trip=0) == {}


def test_circuit_ignores_non_pool_closes(home):
    """D (unit): five openai-codex 429s are not claude-pool evidence."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50],
                  provider="openai-codex")
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}


def test_circuit_is_per_pool(home):
    """Closes on two pools never sum into one trip; each pool trips alone."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150], provider="claude-apr")
        _rl_burst(conn, [now - 120, now - 90], provider="claude-bpr")
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}
        _rl_burst(conn, [now - 80, now - 70, now - 60], provider="claude-bpr")
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"claude-bpr": now - 60 + 600}


def test_circuit_pinned_lanes_key_by_sub(home):
    """claude-apx-7 and claude-bpx-7 share sub-vps-7: one box, one circuit."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150], provider="claude-apx-7")
        _rl_burst(conn, [now - 100, now - 50], provider="claude-bpx-7")
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"sub-vps-7": now - 50 + 600}


def test_rate_limit_trip_knob_via_production_loader(home):
    assert kb._resolve_rate_limit_trip() == 5
    _config(home, rate_limit_trip=7)
    assert kb._resolve_rate_limit_trip() == 7
    _config(home, rate_limit_trip="junk")
    assert kb._resolve_rate_limit_trip() == 5


def test_dispatch_circuit_holds_pool_only_and_notifies_once(home, pool, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    pool.eligible = 5  # the pool LOOKS healthy; the circuit still holds
    _config(home, pool_health_urls=_urls(pool.url, pool.url))
    _profile(home, "a", "claude-apr")
    _profile(home, "b", "openai-codex")
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv))
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50])
        pooled = kb.create_task(conn, title="pool", assignee="a")
        codex = kb.create_task(conn, title="codex", assignee="b")
        observed = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [codex]
        assert (pooled, "rate_limit_circuit") in res.respawn_guarded
        kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [codex]
        assert len(sent) == 1
        assert "--target" in sent[0] and kbud.RECOVERY_TARGET in sent[0]


def test_D_codex_429s_do_not_trip_claude_circuit(home, apr, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible = 12
    _config(home, pool_health_urls=_urls(apr.url))
    _profile(home, "argus", "claude-apr")
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50],
                  provider="openai-codex")
        claude_t = kb.create_task(conn, title="claude", assignee="argus")
        seen: list = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [claude_t]
        assert not [r for r in res.respawn_guarded if r[1] == "rate_limit_circuit"]


def test_dispatch_circuit_holds_only_the_tripped_pool(home, apr, bpr, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible = bpr.eligible = 5
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "a", "claude-apr")
    _profile(home, "b", "claude-bpr")
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50],
                  provider="claude-bpr")
        on_apr = kb.create_task(conn, title="apr", assignee="a")
        on_bpr = kb.create_task(conn, title="bpr", assignee="b")
        seen: list = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [on_apr]
        assert (on_bpr, "rate_limit_circuit") in res.respawn_guarded


def test_circuited_card_falls_back_to_rung_on_open_pool(home, apr, bpr, monkeypatch):
    """CLASS-SWEEP: a circuit on the card's pool is a capacity verdict like
    provider_capped -- a healthy rung on another pool is taken, a rung on the
    SAME circuited pool is not."""
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible = bpr.eligible = 5
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    p = home / "profiles" / "fb"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-bpr", "default": "m"},
        "fallback_providers": [{"provider": "claude-apr", "model": "mapr"}],
    }))
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50],
                  provider="claude-bpr")
        tid = kb.create_task(conn, title="x", assignee="fb")
        seen: list = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [tid]
        assert _events(conn, tid, "dispatch_provider_fallback")[-1]["to_provider"] == "claude-apr"


def test_circuited_card_never_falls_back_onto_another_circuited_pool(home, apr, bpr, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible = bpr.eligible = 5  # sub-vps-3 not listed exhausted: health admits it
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    p = home / "profiles" / "fb"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-bpr", "default": "m"},
        "fallback_providers": [{"provider": "claude-bpx-3", "model": "m3"}],
    }))
    now = int(time.time())
    with kb.connect_closing() as conn:
        ends = [now - 250, now - 200, now - 150, now - 100, now - 50]
        _rl_burst(conn, ends, provider="claude-bpr")
        _rl_burst(conn, ends, provider="claude-apx-3")  # sub-vps-3 circuit open too
        tid = kb.create_task(conn, title="x", assignee="fb")
        seen: list = []
        res = kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == []
        assert (tid, "rate_limit_circuit") in res.respawn_guarded


def test_notify_once_per_episode_even_as_closes_extend_the_hold(home, pool, monkeypatch):
    """Argus F2: workers already running when the circuit trips keep dying at
    429 during the hold; each close pushes ``until`` forward. That must extend
    the SAME episode, not re-arm the #logs latch."""
    from hermes_cli import kanban_budget as kbud

    _config(home, pool_health_urls=_urls(pool.url, pool.url))
    _profile(home, "a", "claude-apr")
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv))
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50])
        kb.create_task(conn, title="pool", assignee="a")
        kb.dispatch_once(conn)
        for ended in (now - 30, now - 20, now - 10):
            _runs(conn, tid, [("rate_limited", ended)])
            kb.dispatch_once(conn)
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"claude-apr": now - 10 + 600}
    assert len(sent) == 1


def test_notify_again_for_a_new_episode_after_the_hold_lapsed(home, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv))
    kb._notify_rate_limit_circuit(None, "claude-apr", 1_000, 5, now=500)
    kb._notify_rate_limit_circuit(None, "claude-apr", 1_060, 5, now=560)   # same episode
    kb._notify_rate_limit_circuit(None, "claude-bpr", 1_060, 5, now=560)   # other pool
    kb._notify_rate_limit_circuit(None, "claude-apr", 2_000, 5, now=1_400)  # lapsed -> new
    assert len(sent) == 3


def test_dispatch_dry_run_does_not_notify(home, pool, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    _config(home, pool_health_urls=_urls(pool.url, pool.url))
    _profile(home, "a", "claude-apr")
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv))
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 250, now - 200, now - 150, now - 100, now - 50])
        kb.create_task(conn, title="pool", assignee="a")
        kb.dispatch_once(conn, dry_run=True)
        assert sent == []


# ---------------------------------------------------------------------------
# r3 -- G2: a pinned box is judged on ITS OWN bridge /health (Argus r2)
# ---------------------------------------------------------------------------
# Live 2026-09-24: both relays list only local + sub-vps-1..9/11/12/15, so the
# relay lists never mention sub-vps-10/16/19/20/22 (82.8% of pinned-lane
# closes over 7d). Each box's bridge /health carries ``usage_limits`` straight
# from the upstream response headers; seven_day=rejected there is a certain 429.


class _Box:
    """Loopback fake of one sub box's bridge GET /health (``usage_limits``)."""

    def __init__(self):
        self.five_hour = "allowed"
        self.seven_day = "allowed"
        self.resets_at = "2099-01-01T00:00:00.000Z"
        self.hits = 0
        box = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                box.hits += 1
                window = lambda st: {"utilization": 100 if st == "rejected" else 10,
                                     "resets_at": box.resets_at, "status": st}
                rejected = "rejected" in (box.five_hour, box.seven_day)
                body = json.dumps({
                    "status": "ok", "service": "claude-bridge",
                    "usage_limits": {
                        "five_hour": window(box.five_hour),
                        "seven_day": window(box.seven_day),
                        "overall_status": "rejected" if rejected else "allowed",
                    },
                    "padding": "x" * 90000,  # live bodies are 8-15 KB and growing
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def box():
    b = _Box()
    yield b
    b.close()


def _registry(home: Path, subs: dict):
    """usage-registry.json at the path the claude-apx/bpx plugins read."""
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "usage-registry.json").write_text(json.dumps({"subs": [
        {"key": k, "bridge_route_base_url": v, "route_base_url": v + "/unused"}
        for k, v in subs.items()
    ]}))


def test_G2_unlisted_pinned_sub_held_when_its_box_reports_rejected(home, bpr, box):
    bpr.eligible = 9  # relay healthy and does NOT list sub-vps-19
    box.seven_day = "rejected"
    _registry(home, {"sub-vps-19": box.base})
    got = ph.capped_provider(_task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url))
    assert got is not None and got["reason"] == "provider_capped"
    assert got["sub"] == "sub-vps-19"
    assert box.hits == 1


def test_G2_apx_lane_shares_the_sub_box_verdict(home, apr, box):
    box.five_hour = "rejected"
    _registry(home, {"sub-vps-19": box.base})
    assert ph.capped_provider(
        _task("claude-apx-19"), {}, {}, pool_urls=_urls(apr.url)) is not None


def test_G2_box_allowed_admits(home, bpr, box):
    _registry(home, {"sub-vps-19": box.base})
    assert ph.capped_provider(
        _task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url)) is None
    assert box.hits == 1


def test_G2_stale_rejection_past_its_reset_admits(home, bpr, box):
    """usage_limits is captured from the LAST response; a held box sends no
    traffic, so a rejection whose window already reset must not hold forever."""
    box.seven_day = "rejected"
    box.resets_at = "2000-01-01T00:00:00.000Z"
    _registry(home, {"sub-vps-19": box.base})
    assert ph.capped_provider(
        _task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url)) is None


def test_G2_box_unreachable_or_unregistered_fails_open(home, bpr):
    _registry(home, {"sub-vps-19": _dead_url().rsplit("/", 1)[0]})
    assert ph.capped_provider(
        _task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url)) is None
    # sub missing from the registry entirely
    assert ph.capped_provider(
        _task("claude-bpx-20"), {}, {}, pool_urls=_urls(None, bpr.url)) is None
    (home / "config" / "usage-registry.json").unlink()
    assert ph.capped_provider(
        _task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url)) is None


def test_G2_box_probe_runs_even_with_family_relay_disabled(home, box):
    box.seven_day = "rejected"
    _registry(home, {"sub-vps-19": box.base})
    assert ph.capped_provider(_task("claude-bpx-19"), {}, {}, pool_urls=_urls()) is not None


def test_G2_box_probe_knob_off_admits(home, bpr, box):
    box.seven_day = "rejected"
    _registry(home, {"sub-vps-19": box.base})
    _config(home, pool_box_health=False)
    assert ph.configured_box_health() is False
    assert ph.capped_provider(
        _task("claude-bpx-19"), {}, {}, pool_urls=_urls(None, bpr.url),
        box_health=ph.configured_box_health()) is None
    assert box.hits == 0


def test_G2_box_health_knob_via_production_loader(home):
    assert ph.configured_box_health() is True
    _config(home, pool_box_health=False)
    assert ph.configured_box_health() is False
    _config(home, pool_box_health="junk")
    assert ph.configured_box_health() is True


def test_G2_dispatch_holds_pinned_card_on_rejected_unlisted_box(home, apr, bpr, box):
    apr.eligible = bpr.eligible = 9
    box.seven_day = "rejected"
    _registry(home, {"sub-vps-19": box.base})
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    _profile(home, "pin", "claude-bpx-19")
    tid, seen, res = _dispatch_one("pin")
    assert seen == []
    assert (tid, "provider_capped") in res.respawn_guarded


def test_G2_fallback_skips_rung_whose_box_is_rejected(home, apr, bpr, box):
    """CLASS-SWEEP: the live chain's rungs (claude-bpx-16, -10) are unlisted
    subs; a rung on a rejected box must be skipped for the next rung."""
    apr.eligible, bpr.eligible = 4, 0
    box.seven_day = "rejected"
    _registry(home, {"sub-vps-16": box.base})
    _config(home, pool_health_urls=_urls(apr.url, bpr.url))
    p = home / "profiles" / "fb"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": "claude-bpr", "default": "m"},
        "fallback_providers": [
            {"provider": "claude-bpx-16", "model": "m16"},
            {"provider": "claude-apr", "model": "mapr"},
        ],
    }))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="fb")
        seen: list = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [tid]
        assert _events(conn, tid, "dispatch_provider_fallback")[-1]["to_provider"] == "claude-apr"


# ---------------------------------------------------------------------------
# r3 -- G1: a close counts against the pool that SERVED the run (Argus r2)
# ---------------------------------------------------------------------------


def _fb_chain_profile(home, chain, provider="claude-bpr"):
    p = home / "profiles" / "fb"
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(json.dumps({
        "model": {"provider": provider, "default": "m"}, "fallback_providers": chain,
    }))


def _close_running_rate_limited(conn, now):
    rows = conn.execute(
        "SELECT id, task_id FROM task_runs WHERE ended_at IS NULL ORDER BY id").fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE task_runs SET status='rate_limited', outcome='rate_limited', "
                     "ended_at=? WHERE id=?", (now - 300 + 30 * i, r["id"]))
        conn.execute("UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
                     "worker_pid=NULL WHERE id=?", (r["task_id"],))
    conn.commit()
    return len(rows)


def test_G1_fallback_rung_429s_open_the_rungs_circuit_not_the_primary(home, apr, bpr, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible, bpr.eligible = 4, 0
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), max_spawn=50,
            max_in_progress_per_profile=50, pool_spawns_per_eligible=0)
    _fb_chain_profile(home, [
        {"provider": "claude-bpx-16", "model": "m16"},
        {"provider": "claude-apr", "model": "mapr"},
    ])
    with kb.connect_closing() as conn:
        first = [kb.create_task(conn, title=f"s{i}", assignee="fb") for i in range(5)]
        kb.dispatch_once(conn, spawn_fn=_spawner([]))
        rungs = [_events(conn, t, "dispatch_provider_fallback")[-1]["to_provider"] for t in first]
        assert rungs == ["claude-bpx-16"] * 5
        now = int(time.time())
        assert _close_running_rate_limited(conn, now) == 5
        circuits = kb.rate_limit_circuits(conn, now=now, trip=5)
        assert set(circuits) == {"sub-vps-16"}
        nxt = kb.create_task(conn, title="next", assignee="fb")
        seen: list = []
        kb.dispatch_once(conn, spawn_fn=_spawner(seen))
        assert seen == [nxt]
        assert _events(conn, nxt, "dispatch_provider_fallback")[-1]["to_provider"] == "claude-apr"


def test_G1_fallback_onto_non_pool_rung_does_not_charge_the_primary(home):
    """A capped claude-bpr card served by an openai-codex rung: its 429s are
    codex evidence, never claude-bpr's."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        for i in range(5):
            tid = kb.create_task(conn, title=f"c{i}", assignee="z", model_override="m",
                                 provider_override="claude-bpr")
            conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, "
                         "started_at, ended_at) VALUES (?, 'z', 'rate_limited', "
                         "'rate_limited', ?, ?)", (tid, now - 400 + 30 * i, now - 300 + 30 * i))
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "dispatch_provider_fallback", {
                    "from_provider": "claude-bpr", "to_provider": "openai-codex",
                    "to_model": "gpt"}, run_id=run_id)
        conn.commit()
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}


def test_G1_lane_routed_429s_charge_the_lane_pool(home, apr, bpr, monkeypatch):
    """CLASS-SWEEP: a lane override is the other in-memory route change; its
    runs are served by the lane provider, not the profile default."""
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible = bpr.eligible = 9
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), max_spawn=50,
            max_in_progress_per_profile=50)
    _profile(home, "ln", "claude-bpr")
    with kb.connect_closing() as conn:
        kb.set_lane_model_override(conn, provider="claude-apr", model="lm", assignee="ln",
                                   expires_at=int(time.time()) + 3600, reason="window")
        for i in range(5):
            kb.create_task(conn, title=f"l{i}", assignee="ln")
        kb.dispatch_once(conn, spawn_fn=_spawner([]))
        now = int(time.time())
        assert _close_running_rate_limited(conn, now) == 5
        assert set(kb.rate_limit_circuits(conn, now=now, trip=5)) == {"claude-apr"}


# Argus r3 F2: a lane override on a capped pool that then falls back to a rung
# writes dispatch_lane_route FIRST and dispatch_provider_fallback SECOND. The
# later event names the pool that actually served the run; a first-event-wins
# resolver would charge the lane pool. (Adopted from Argus r3 probe test_H1.)
def test_circuit_lane_then_fallback_charges_the_serving_rung(home, apr, bpr, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: None)
    apr.eligible, bpr.eligible = 0, 9          # lane pool (apr) capped
    _config(home, pool_health_urls=_urls(apr.url, bpr.url), max_spawn=50,
            max_in_progress_per_profile=50, pool_spawns_per_eligible=0)
    _fb_chain_profile(home, [{"provider": "claude-bpx-16", "model": "m16"}])
    with kb.connect_closing() as conn:
        kb.set_lane_model_override(conn, provider="claude-apr", model="lm", assignee="fb",
                                   expires_at=int(time.time()) + 3600, reason="window")
        tids = [kb.create_task(conn, title=f"h{i}", assignee="fb") for i in range(5)]
        kb.dispatch_once(conn, spawn_fn=_spawner([]))
        lane = [_events(conn, t, "dispatch_lane_route") for t in tids]
        fb = [_events(conn, t, "dispatch_provider_fallback") for t in tids]
        print("\nH1 lane events:", [e[-1]["provider"] if e else None for e in lane])
        print("H1 fallback rungs:", [e[-1]["to_provider"] if e else None for e in fb])
        assert all(lane) and all(fb), "precondition: both route events on every run"
        assert {e[-1]["to_provider"] for e in fb} == {"claude-bpx-16"}
        now = int(time.time())
        assert _close_running_rate_limited(conn, now) == 5
        circuits = kb.rate_limit_circuits(conn, now=now, trip=5)
        print("H1 circuits:", circuits)
        assert set(circuits) == {"sub-vps-16"}, (
            f"5 closes SERVED by claude-bpx-16 charged to {sorted(circuits)}")
