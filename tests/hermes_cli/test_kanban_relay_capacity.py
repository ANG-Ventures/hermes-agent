"""Relay-family admissions are capped at the relay's own free turn slots (t_6b917840).

Incident 2026-11-01: claude-apr had 1 eligible seat, max_inflight 4, active_turns 4,
and the relay rejected 334 turns at max_inflight while the dispatcher kept placing
apr work. Apollo ruling: the dispatcher's apr budget is the relay's effective
capacity (max_inflight - active_turns); an unreadable /health is 0 free.
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_provider_health as ph
from tests.hermes_cli.test_kanban_pool_ratelimit_gates import (  # noqa: F401
    _dead_url, _events, _urls, home,
)


class _Relay:
    """Loopback fake of a relay /health that publishes its turn ceiling."""

    def __init__(self, eligible, max_inflight, active_turns):
        self.body = {"status": "ok", "eligible_count": eligible,
                     "max_inflight": max_inflight, "active_turns": active_turns,
                     "exhausted": [], "capped_quota": []}
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps(relay.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/health"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay():
    made = []

    def make(*args):
        made.append(_Relay(*args))
        return made[-1]
    yield make
    for r in made:
        r.close()


def _setup(home, apr_url, **kanban):
    (home / "config.yaml").write_text(json.dumps({"kanban": {
        "pool_health_urls": _urls(apr_url), "pool_box_health": False,
        "pool_spawns_per_eligible": 100, **kanban}}))
    p = home / "profiles" / "a"
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(json.dumps({"model": {"provider": "claude-apr",
                                                         "default": "m"}}))


def _dispatch(n):
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"apr-{i}", assignee="a") for i in range(n)]
        seen: list = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: seen.append(t.id) or 777777,
                                max_spawn=100, max_in_progress=100)
        deferred = {tid: _events(conn, tid, "deferred") for tid in ids}
        return ids, seen, res, deferred


@pytest.mark.parametrize("eligible,cap,active,free", [(1, 4, 4, 0), (3, 12, 6, 6)])
def test_relay_free_slots_is_max_inflight_minus_active(home, relay, eligible, cap, active, free):
    r = relay(eligible, cap, active)
    got = ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))
    assert got["free"] == free


def test_full_relay_places_nothing_on_apr(home, relay):
    r = relay(1, 4, 4)
    _setup(home, r.url)
    ids, seen, res, deferred = _dispatch(3)
    assert seen == []
    for tid in ids:
        assert (tid, "relay_capacity") in res.respawn_guarded
        assert deferred[tid][-1]["free"] == 0 and deferred[tid][-1]["pool"] == "claude-apr"


def test_admissions_stop_at_relay_free(home, relay):
    r = relay(3, 12, 6)
    _setup(home, r.url)
    ids, seen, res, _ = _dispatch(9)
    assert seen == ids[:6]
    assert all((tid, "relay_capacity") in res.respawn_guarded for tid in ids[6:])


def test_unreadable_relay_fails_closed(home):
    _setup(home, _dead_url())
    ids, seen, res, deferred = _dispatch(2)
    assert seen == []
    assert deferred[ids[0]][-1]["unreadable"] is True


def test_relay_without_figures_adds_no_ceiling(home, relay):
    r = relay(3, 12, 6)
    del r.body["max_inflight"], r.body["active_turns"]
    _setup(home, r.url)
    ids, seen, _, _ = _dispatch(9)
    assert seen == ids


def test_knob_off_restores_eligible_budget_only(home, relay):
    r = relay(1, 4, 4)
    _setup(home, r.url, pool_relay_capacity=False)
    ids, seen, _, _ = _dispatch(3)
    assert seen == ids
