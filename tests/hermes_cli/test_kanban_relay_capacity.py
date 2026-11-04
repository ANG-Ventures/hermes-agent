"""Relay-family admissions are capped at the relay's own free turn slots (t_6b917840).

Incident 2026-11-01: claude-apr had 1 eligible seat, max_inflight 4, active_turns 4,
and the relay rejected 334 turns at max_inflight while the dispatcher kept placing
apr work. Apollo ruling: the dispatcher's apr budget is the relay's effective
capacity (max_inflight - active_turns).

t_bde7653d (11-04): max_inflight (box_capacity) sums EVERY seat's cap, spent
ones included, so 25/26 seats exhausted still read as ~150 free. The relay
ceiling is now min(max_inflight, summed cap of eligible_keys seats) -
active_turns, read through a short-TTL cache. An unreadable /health adds no
ceiling (falls back to the slot count) and logs a WARNING.
"""
from __future__ import annotations

import http.server
import json
import logging
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
        self.hits = 0
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                relay.hits += 1
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


def test_unreadable_relay_falls_back_to_slot_count_and_logs(home, caplog):
    """t_bde7653d: a dead relay probe never blocks dispatch; it logs loudly."""
    _setup(home, _dead_url())
    with caplog.at_level(logging.WARNING, logger=ph.__name__):
        ids, seen, res, deferred = _dispatch(2)
    assert seen == ids
    assert not any(tid for tid, why in res.respawn_guarded if why == "relay_capacity")
    assert any("PHASE=kanban_relay_capacity_unreadable" in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)


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


# --- t_bde7653d: spent seats are not free -------------------------------------

SEATS = [f"sub-vps-{i}" for i in range(1, 27)]


def _seat_body(r, eligible_keys, active):
    """The live /health shape (11-01 09:37Z / 11-04): every seat cap 6, max_inflight
    the summed box capacity of ALL seats, eligible_keys only the unspent ones."""
    r.body.update({
        "eligible_count": len(eligible_keys), "eligible_keys": list(eligible_keys),
        "seats": {k: {"cap": 6, "used": 0, "fresh": True} for k in SEATS},
        "max_inflight": 6 * len(SEATS), "active_turns": active,
        "exhausted": [k for k in SEATS if k not in eligible_keys],
    })


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(ph, "time", c)
    return c


def test_exhausted_seats_yield_zero_free_and_no_spawn(home, relay, clock):
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=6)  # the one live seat is full
    assert ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))["free"] == 0
    _setup(home, r.url)
    ids, seen, res, deferred = _dispatch(3)
    assert seen == []
    for tid in ids:
        assert (tid, "relay_capacity") in res.respawn_guarded
        assert deferred[tid][-1]["free"] == 0


def test_zero_eligible_is_zero_free_whatever_max_inflight_says(home, relay):
    r = relay(0, 156, 0)
    assert ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))["free"] == 0


def test_one_eligible_seat_admits_only_its_spare_cap(home, relay, clock):
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=4)
    _setup(home, r.url)
    ids, seen, _, _ = _dispatch(5)
    assert seen == ids[:2]  # 6 cap - 4 active; max_inflight would have said 152


def test_recovered_seats_restore_free(home, relay, clock):
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=6)
    _setup(home, r.url)
    held, seen, _, _ = _dispatch(2)
    assert seen == []
    _seat_body(r, ["sub-vps-4", "sub-vps-16", "sub-vps-21", "sub-vps-22"], active=6)
    clock.t += ph._RELAY_HEALTH_TTL_S + 0.1  # past the TTL: the next tick re-reads
    ids, seen, _, _ = _dispatch(3)
    assert sorted(seen) == sorted(held + ids)  # the held cards spawn too
    assert ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))["free"] == 24 - 6


def test_relay_health_read_is_cached_for_the_ttl(home, relay, clock):
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=0)
    urls = _urls(r.url)
    assert ph.relay_free_slots("claude-apr", {}, {}, urls)["free"] == 6
    clock.t += ph._RELAY_HEALTH_TTL_S - 1
    assert ph.relay_free_slots("claude-apr", {}, {}, urls)["free"] == 6
    assert r.hits == 1
    clock.t += 2
    ph.relay_free_slots("claude-apr", {}, {}, urls)
    assert r.hits == 2


def test_published_free_is_capped_by_eligible_seats(home, relay):
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=5)
    r.body["free"] = 40
    assert ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))["free"] == 1


def test_admission_drops_cached_health_so_next_tick_rereads(home, relay, clock):
    """Prism e4502a532a19: a body cached before an admission must not let the next
    tick (inside the TTL) re-grant the same spare slots."""
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=4)
    _setup(home, r.url)
    first, seen, _, _ = _dispatch(2)
    assert seen == first  # 2 spare slots, both used
    r.body["active_turns"] = 6  # the relay now carries the two new workers
    clock.t += 1  # well inside the TTL
    ids, seen, res, _ = _dispatch(2)
    assert seen == []
    assert all((tid, "relay_capacity") in res.respawn_guarded for tid in ids)
    assert r.hits == 2


@pytest.mark.parametrize("keys,free", [
    (["sub-vps-23", "sub-vps-23"], 0),  # duplicate key is one seat (6 cap - 6 active)
    (["sub-vps-23", 7], 150),  # 156 max_inflight - 6  # non-string key: no seat figure, max_inflight rules
    (["sub-vps-23", ["x"]], 150),  # unhashable key: no TypeError
])
def test_malformed_eligible_keys_never_overcount_or_raise(home, relay, keys, free):
    """Prism b3a5f15c6abc."""
    r = relay(1, 0, 0)
    _seat_body(r, ["sub-vps-23"], active=6)
    r.body["eligible_keys"] = keys
    r.body["active_turns"] = 6
    assert ph.relay_free_slots("claude-apr", {}, {}, _urls(r.url))["free"] == free
