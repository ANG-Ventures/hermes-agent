"""Rate-limit storm brakes for the kanban dispatcher (t_32f44156).

Measured 2026-09-22: 3,706 ``rate_limited`` runs in 7d; 42% ran >2 min
(boot, load skills + card + repo, make real calls) before dying at 429 with
no output. Three brakes, each tested here and each mutation-checked:

1. POOL-HEALTH ADMISSION -- a worker whose provider is the shared claude relay
   pool (claude-apr / claude-bpr / claude-apx-* / claude-bpx-*) is not spawned
   while the pool's ``/health`` reports ``eligible_count`` below the minimum.
   Uses ``kanban.pool_health_url`` when ``provider_health_probes`` has no
   explicit entry. Non-pool providers never probe. Probe failure fails OPEN.
2. ESCALATING PER-TASK BACKOFF -- consecutive rate_limited runs hold the task
   for 5m, 15m, 45m, then 2h (cap); any non-rate_limited run resets it.
3. TICK CIRCUIT -- >= ``kanban.rate_limit_trip`` rate_limited closes inside a
   10-minute window hold ALL pool-bound spawns for 10 minutes; notified once.

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
        self.hits = 0
        pool = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                pool.hits += 1
                body = json.dumps({
                    "status": "ok" if pool.eligible else "all_capped",
                    "eligible_count": pool.eligible, "pool_size": 15,
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


@pytest.mark.parametrize("provider,expected", [
    ("claude-apr", True), ("claude-bpr", True), ("claude-apx-3", True),
    ("claude-bpx-22", True), ("CLAUDE-BPX-7", True),
    ("openai-codex", False), ("anthropic", False), ("claude-api-proxy", False),
    ("", False), (None, False),
])
def test_is_pool_provider(provider, expected):
    assert ph.is_pool_provider(provider) is expected


def test_pool_health_url_default_and_config_via_production_loader(home):
    assert ph.configured_pool_health_url() == "http://127.0.0.1:18810/health"
    _config(home, pool_health_url="http://127.0.0.1:9/h")
    assert ph.configured_pool_health_url() == "http://127.0.0.1:9/h"
    _config(home, pool_health_url="")
    assert ph.configured_pool_health_url() == ""


def _task(provider):
    from types import SimpleNamespace
    return SimpleNamespace(model_override="m", provider_override=provider, assignee="a")


def test_pool_provider_capped_via_pool_url(home, pool):
    pool.eligible = 0
    got = ph.capped_provider(_task("claude-bpx-22"), {}, {}, pool_url=pool.url)
    assert got is not None and got["provider"] == "claude-bpx-22"


def test_pool_provider_admitted_when_eligible(home, pool):
    pool.eligible = 2
    assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_url=pool.url) is None


def test_min_eligible_threshold(home, pool):
    pool.eligible = 2
    assert ph.capped_provider(
        _task("claude-apr"), {}, {}, pool_url=pool.url, min_eligible=3,
    ) is not None


def test_non_pool_provider_never_probes(home, pool):
    pool.eligible = 0
    assert ph.capped_provider(_task("openai-codex"), {}, {}, pool_url=pool.url) is None
    assert pool.hits == 0


def test_empty_pool_url_disables(home, pool):
    pool.eligible = 0
    assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_url="") is None
    assert pool.hits == 0


def test_explicit_probe_entry_wins_over_pool_url(home, pool):
    pool.eligible = 0
    probes = {"claude-apr": _dead_url()}
    # The explicit (dead) URL is used, so the gate fails open rather than
    # reading the capped pool_url.
    assert ph.capped_provider(_task("claude-apr"), probes, {}, pool_url=pool.url) is None
    assert pool.hits == 0


def test_unreachable_fails_open_and_warns_once(home, caplog):
    url = _dead_url()
    caplog.set_level(logging.DEBUG, logger="hermes_cli.kanban_provider_health")
    for _ in range(3):
        assert ph.capped_provider(_task("claude-apr"), {}, {}, pool_url=url) is None
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and url in r.getMessage()]
    assert len(warns) == 1


def test_capped_state_logged_once_per_transition(home, pool, caplog):
    caplog.set_level(logging.INFO, logger="hermes_cli.kanban_provider_health")
    pool.eligible = 0
    for _ in range(3):
        ph.capped_provider(_task("claude-apr"), {}, {}, pool_url=pool.url)
    pool.eligible = 4
    for _ in range(3):
        ph.capped_provider(_task("claude-apr"), {}, {}, pool_url=pool.url)
    lines = [r.getMessage() for r in caplog.records if pool.url in r.getMessage()]
    assert len(lines) == 2, lines


def test_dispatch_holds_pool_worker_when_pool_empty(home, pool):
    pool.eligible = 0
    _config(home, pool_health_url=pool.url)
    _profile(home, "a", "claude-bpx-22")
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
    _config(home, pool_health_url=pool.url)
    _profile(home, "b", "openai-codex")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex", assignee="b")
        observed = []
        kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [tid]
        assert pool.hits == 0


def test_dispatch_fails_open_when_pool_unreachable(home):
    _config(home, pool_health_url=_dead_url())
    _profile(home, "a", "claude-apr")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pool bound", assignee="a")
        observed = []
        kb.dispatch_once(conn, spawn_fn=_spawner(observed))
        assert observed == [tid]


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


def _rl_burst(conn, ends):
    tid = kb.create_task(conn, title="storm", assignee="z")
    conn.execute("UPDATE tasks SET status='done', completed_at=strftime('%s','now') WHERE id=?", (tid,))
    _runs(conn, tid, [("rate_limited", e) for e in ends])
    return tid


def test_circuit_opens_on_trip_within_window(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        ends = [now - 400, now - 300, now - 200, now - 100, now - 60]
        _rl_burst(conn, ends)
        assert kb.rate_limit_circuit_open_until(conn, now=now, trip=5) == now - 60 + 600


def test_circuit_stays_open_ten_minutes_after_trip(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        # Tripped 9 min ago; the window has since emptied but the hold stands.
        t = now - 540
        _rl_burst(conn, [t - 200, t - 150, t - 100, t - 50, t])
        assert kb.rate_limit_circuit_open_until(conn, now=now, trip=5) == t + 600


def test_circuit_closed_below_trip(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 300, now - 200, now - 100, now - 60])
        assert kb.rate_limit_circuit_open_until(conn, now=now, trip=5) is None


def test_circuit_closed_when_spread_beyond_window(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 2400, now - 1800, now - 1200, now - 601, now - 10])
        # Five closes, but no five of them within any 600s span.
        assert kb.rate_limit_circuit_open_until(conn, now=now, trip=5) is None


def test_circuit_trip_zero_disables(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        _rl_burst(conn, [now - 50] * 8)
        assert kb.rate_limit_circuit_open_until(conn, now=now, trip=0) is None


def test_rate_limit_trip_knob_via_production_loader(home):
    assert kb._resolve_rate_limit_trip() == 5
    _config(home, rate_limit_trip=7)
    assert kb._resolve_rate_limit_trip() == 7
    _config(home, rate_limit_trip="junk")
    assert kb._resolve_rate_limit_trip() == 5


def test_dispatch_circuit_holds_pool_only_and_notifies_once(home, pool, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    pool.eligible = 5  # the pool LOOKS healthy; the circuit still holds
    _config(home, pool_health_url=pool.url)
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


def test_dispatch_dry_run_does_not_notify(home, pool, monkeypatch):
    from hermes_cli import kanban_budget as kbud

    _config(home, pool_health_url=pool.url)
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
