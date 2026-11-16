"""Stale fleet-pause holds (t_0c5a95ab, Ace 2026-11-06 20:31).

Measured live 11-06 20:12: t_1bf66aac / t_6aa3a051 still read ``pause_hold (ci-box:...)`` 37 h after ci-box resumed
(manifest archived), t_f3fcb3a0 held on ``ace-media:resume_pin_conflict:host_studio`` after ace-media resumed.
Each arm below is red on the pre-fix module and green after it.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pause_hold as kph

from tests.hermes_cli.test_kanban_pause_hold import (  # noqa: F401  (fixtures + helpers of the base suite)
    MID, _no_real_spawn, _plan, _routes, _set, _tick, home,
)

DEAD_PID = 2 ** 22 + 12345   # above every default pid_max on macOS/Linux: never a live pid


def _write(home, doc):
    d = home / "state" / "fleet-pause" / doc["host"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(doc))


def _last_wait(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='allocation_wait' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return None if row is None else json.loads(row[0])


def _label(conn, tid, detail):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "allocation_wait", {"code": "pause_hold", "detail": detail,
                                                        "reason": f"pause_hold ({detail})", "route": "wait",
                                                        "host": None, "since": int(time.time()) - 3600})


# -- (1) manifest resumed / archived, card still labelled held ------------------------------------------------

def test_resumed_host_card_never_routed_loses_its_stale_label_in_one_tick(home):
    """t_1bf66aac shape: a no_worker card labelled pause_hold by an old ci-box pause; ci-box has no manifest.json
    any more. One live tick ends the label (the dispatcher never routes a no_worker card, so nothing else did)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="human lane", assignee="alpha", body="x")
        _set(conn, tid, status="ready", no_worker=1)
        _label(conn, tid, f"ci-box:{MID}")
        assert [c["task_id"] for c in kph.holds(conn)["cards"]] == [tid]
        _tick(conn, _plan())
        last = _last_wait(conn, tid)
        assert last.get("cleared") is True, last
        assert "no longer holds" in last["reason"]
        assert kph.holds(conn)["cards"] == []


def test_resumed_manifest_ends_label_but_a_live_pause_keeps_it(home):
    """Control: with ci-box really paused the same card keeps its label (the hold is derived, not erased)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="human lane", assignee="alpha", body="x")
        _set(conn, tid, status="ready", no_worker=1, origin_host="ci-box")
        _label(conn, tid, f"ci-box:{MID}")
        _write(home, {"schema": 2, "id": MID, "host": "ci-box", "phase": "paused", "units": [],
                      "lease_until": int(time.time()) + 3600, "driver": {"pid": DEAD_PID}})
        _tick(conn, _plan())
        assert _last_wait(conn, tid).get("code") == "pause_hold"
        rows = kph.holds(conn)["cards"]
        assert rows and rows[0]["live"] is True
        _write(home, {"schema": 2, "id": MID, "host": "ci-box", "phase": "resumed", "units": [],
                      "lease_until": int(time.time()) + 3600, "resumed_at": int(time.time())})
        _tick(conn, _plan())
        assert _last_wait(conn, tid).get("cleared") is True


# -- (2) lease expired, driver dead ---------------------------------------------------------------------------

def test_lease_expired_driver_dead_manifest_no_longer_holds(home, monkeypatch):
    sent = []
    monkeypatch.setattr(kph, "_pager", lambda: (lambda body: sent.append(body) or True))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _write(home, {"schema": 2, "id": MID, "host": "ci-box", "phase": "paused", "units": [],
                      "lease_until": int(time.time()) - kph.LEASE_GRACE_S - 60, "driver": {"pid": DEAD_PID}})
        res, spawned = _tick(conn, _plan())
        assert tid in {t for t, _ in spawned}, _routes(res).get(tid)
        _set(conn, tid, status="ready", claim_lock=None)
        _tick(conn, _plan())
    assert len(sent) == 1 and kph.STALE_EVENT_REASON in sent[0] and MID in sent[0]
    assert (home / "state" / "fleet-pause" / "ci-box" / f"stale-cleared.{MID}.json").exists()


@pytest.mark.parametrize("shape", ["lease_live", "driver_alive", "no_lease"])
def test_stale_failsafe_never_lifts_a_live_pause(home, shape):
    """Controls: inside the lease, with a live driver past it, or with no lease field at all, the hold stands."""
    import os
    lease = int(time.time()) + 3600 if shape == "lease_live" else int(time.time()) - kph.LEASE_GRACE_S - 60
    doc = {"schema": 2, "id": MID, "host": "ci-box", "phase": "paused", "units": [],
           "driver": {"pid": os.getpid() if shape == "driver_alive" else DEAD_PID}}
    if shape != "no_lease":
        doc["lease_until"] = lease
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _write(home, doc)
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert _routes(res)[tid]["code"] == "pause_hold"
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, tid)


def test_driver_probe_is_cross_platform_dead_pid_stale_live_pid_holds():
    """The driver probe is psutil (never os.kill(pid, 0): on Windows that sends CTRL_C_EVENT). Dead pid = stale,
    live pid = hold, on every OS."""
    import os
    lease = int(time.time()) - kph.LEASE_GRACE_S - 60
    dead = {"phase": "paused", "lease_until": lease, "driver": {"pid": DEAD_PID}}
    live = {"phase": "paused", "lease_until": lease, "driver": {"pid": os.getpid()}}
    assert kph._driver_alive(dead) is False and kph._driver_alive(live) is True
    assert "lease expired" in (kph.stale_reason(dead) or "")
    assert kph.stale_reason(live) is None


def test_driver_probe_that_cannot_run_keeps_the_hold(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: (_ for _ in ()).throw(OSError("probe failed")))
    doc = {"phase": "paused", "lease_until": int(time.time()) - kph.LEASE_GRACE_S - 60, "driver": {"pid": DEAD_PID}}
    assert kph._driver_alive(doc) is True
    assert kph.stale_reason(doc) is None


def test_kill_switch_off_restores_the_old_hold(home, monkeypatch):
    """KANBAN_PAUSE_HOLD_STALE=0: a lease-expired, driver-dead manifest holds again and a conflicting resume pin
    waits past the grace, exactly as before the fail-safe."""
    monkeypatch.setenv(kph.STALE_ENV, "0")
    doc = {"phase": "paused", "lease_until": int(time.time()) - kph.LEASE_GRACE_S - 60, "driver": {"pid": DEAD_PID}}
    assert kph.stale_reason(doc) is None
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _write(home, {"schema": 2, "id": MID, "host": "ci-box", **doc, "units": []})
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert _routes(res)[tid]["code"] == "pause_hold"
        pin = _pin_card(conn, home, kph.PIN_CONFLICT_GRACE_S + 60)
        res, _spawned = _tick(conn, _plan())
        assert "resume_pin_conflict:host_studio" in _routes(res)[pin]["detail"]


# -- (3) resume_pin_conflict on a resumed host ----------------------------------------------------------------

def _pin_card(conn, home, resumed_ago):
    tid = kb.create_task(conn, title="studio", assignee="alpha",
                         body="host: studio\nrequires: control-plane-read\nRead the board.")
    _set(conn, tid, status="ready", origin_host="ace-media")
    _write(home, {"schema": 2, "id": "m-ace-media", "host": "ace-media", "phase": "resumed",
                  "resumed_at": int(time.time()) - resumed_ago, "lease_until": int(time.time()),
                  "units": [{"lane": "kanban", "id": tid, "resume": {"action": "unblock", "new_run_id": None}}]})
    return tid


def test_resume_pin_conflict_releases_after_grace(home):
    """t_f3fcb3a0 shape: a `host: studio` card whose origin ace-media resumed; the pin can never be honoured and the
    card can never earn the run that retires it. Past PIN_CONFLICT_GRACE_S it routes ordinarily."""
    with kb.connect_closing() as conn:
        tid = _pin_card(conn, home, kph.PIN_CONFLICT_GRACE_S + 60)
        res, _spawned = _tick(conn, _plan())
        r = _routes(res)[tid]
        assert "resume_pin_conflict" not in str(r.get("detail") or ""), r
        assert r.get("code") != "pause_hold", r


def test_resume_pin_conflict_still_waits_inside_grace(home):
    with kb.connect_closing() as conn:
        tid = _pin_card(conn, home, 60)
        res, _spawned = _tick(conn, _plan())
        assert "resume_pin_conflict:host_studio" in _routes(res)[tid]["detail"]


def test_settled_resume_action_owes_no_pin(home):
    assert kph.owes_run({"resume": {"action": "unblock", "new_run_id": None}}) is True
    assert kph.owes_run({"resume": {"action": "unblock", "new_run_id": 9}}) is False
    for action in kph.SETTLED_ACTIONS:
        assert kph.owes_run({"resume": {"action": action, "new_run_id": None}}) is False


# -- (4) operator verb ----------------------------------------------------------------------------------------

def test_placement_holds_read_model_and_clear(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="alpha", body="x")
        _set(conn, tid, status="ready", no_worker=1, origin_host="ci-box")
        _label(conn, tid, f"ci-box:{MID}")
        _write(home, {"schema": 2, "id": MID, "host": "ci-box", "phase": "paused", "units": [],
                      "lease_until": int(time.time()) - kph.LEASE_GRACE_S - 60, "driver": {"pid": DEAD_PID}})
        rep = kph.holds(conn)
        assert rep["hosts"]["ci-box"]["holds"] is False
        assert "lease expired" in rep["hosts"]["ci-box"]["why"]
        assert rep["cards"][0]["live"] is False
        assert kph.clear_card(conn, tid, "test") is True
        assert kph.clear_card(conn, tid, "test") is False
        assert kph.holds(conn)["cards"] == []
