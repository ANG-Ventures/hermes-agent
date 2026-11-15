"""Rename wave 3 step 2 (t_7be48220): every reader of the placement event kinds gives the SAME answer when
the board holds the allocation names (placement -> allocation, placement_wait -> allocation_wait,
worker_placed -> worker_allocated, placement_rerouted -> allocation_rerouted). Writers still emit the old
names; old rows are never rewritten, so for one release a board holds both.

Each reader is run once on a board written with the old names and once on a board written with the new
names; the two results must be equal AND carry the signal (a reader that ignores the rows entirely would be
equal on both and is refused by the ``signal`` check).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement_reasons as pr

NOW = 1_791_800_000
OLD = {"placement": "placement", "wait": "placement_wait", "placed": "worker_placed",
       "rerouted": "placement_rerouted"}
NEW = {"placement": "allocation", "wait": "allocation_wait", "placed": "worker_allocated",
       "rerouted": "allocation_rerouted"}


def test_kind_map_is_the_settled_one():
    assert pr.KIND_ALIASES == {"placement": "allocation", "placement_wait": "allocation_wait",
                               "worker_placed": "worker_allocated", "placement_hint": "allocation_hint",
                               "placement_rerouted": "allocation_rerouted"}
    assert pr.kind_names("placement_wait") == ("placement_wait", "allocation_wait")
    assert pr.kind_names("allocation_wait") == ("placement_wait", "allocation_wait")
    assert pr.kind_names("claimed") == ("claimed",)
    assert pr.kind_names("worker_placed", "placement") == ("worker_placed", "worker_allocated",
                                                           "placement", "allocation")
    assert pr.kind_marks("placement", "claimed") == "?,?,?"
    assert pr.normalize_kind("worker_placed") == pr.normalize_kind("worker_allocated") == "worker_allocated"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing():
        kb.create_board(slug="default", name="Default")
    for slug in ("old", "new"):
        kb.create_board(slug=slug, name=slug)
    return h


def _ev(conn, tid, kind, payload, run_id=None, created=None):
    """A raw event row: the reader must accept the name as stored, so no writer helper renames it."""
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                 (tid, run_id, kind, json.dumps(payload), created if created is not None else NOW - 100))


def _run(conn, tid, status="running"):
    return conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'alpha', ?, ?)",
                        (tid, status, NOW - 200)).lastrowid


def _seed(conn, k) -> dict:
    """One board's worth of placement history, every kind written under the names in ``k``."""
    ids = {}
    # a running card placed on a pool host, with a reservation
    t = kb.create_task(conn, title="running", assignee="alpha")
    r = _run(conn, t)
    conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (r, t))
    _ev(conn, t, "spawned", {}, run_id=r)
    _ev(conn, t, k["placed"], {"host": "zeta-box", "target": "kanbanw@zeta-box", "reason": "pin",
                               "placement_reservation": "res-1"}, run_id=r)
    _ev(conn, t, k["placement"], {"run_id": r, "rule": "policy", "host": "zeta-box", "route_class": "remote",
                                  "reason": "pool"}, run_id=r)
    ids["running"], ids["run"] = t, r
    # a ready card held by an open pause_hold wait on the drained-pin reason
    h = kb.create_task(conn, title="held", assignee="alpha")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (h,))
    _ev(conn, h, k["wait"], {"code": "pause_hold", "reason": "pin_host_draining", "host": "none",
                             "since": NOW - 7200, "route": "pin"})
    ids["held"] = h
    # a ready card whose latest wait is an open host=none requires hold
    n = kb.create_task(conn, title="none", assignee="alpha")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (n,))
    _ev(conn, n, k["wait"], {"host": "none", "reason": "requires_unsatisfied:gpu", "since": NOW - 60})
    ids["none"] = n
    conn.commit()
    return ids


def _boards(home):
    out = {}
    for slug, k in (("old", OLD), ("new", NEW)):
        with kb.connect_closing(board=slug) as c:
            out[slug] = _seed(c, k)
    return out


def _both(home, read):
    """``read(conn, ids)`` on the old-name board and on the new-name board."""
    ids = _boards(home)
    res = {}
    for slug in ("old", "new"):
        with kb.connect_closing(board=slug) as c:
            res[slug] = read(c, ids[slug])
    return res["old"], res["new"]


def _strip_ids(rows, *keys):
    return [{k: v for k, v in r.items() if k not in keys} for r in rows]


READERS = {
    "workspace.bind_host": (
        lambda c, i: __import__("hermes_cli.kanban_db_workspace", fromlist=["x"]).bind_host(c, i["running"]),
        lambda v: v == "zeta-box"),
    "headroom.previous_host": (
        lambda c, i: __import__("hermes_cli.kanban_headroom", fromlist=["x"]).previous_host(c, i["running"]),
        lambda v: v == "zeta-box"),
    "headroom.run_reservation": (
        lambda c, i: __import__("hermes_cli.kanban_headroom", fromlist=["x"]).run_reservation(
            c, i["running"], i["run"]),
        lambda v: v == ("res-1", None)),
    "headroom.run_footprint_target": (
        lambda c, i: (lambda d: d and {k: d[k] for k in ("host", "target")})(
            __import__("hermes_cli.kanban_headroom", fromlist=["x"]).run_footprint_target(
                c, i["running"], i["run"])),
        lambda v: v == {"host": "zeta-box", "target": "kanbanw@zeta-box"}),
    "headroom.running_reservations": (
        lambda c, i: [(rid, via) for _t, rid, via in
                      __import__("hermes_cli.kanban_headroom", fromlist=["x"]).running_reservations(c)],
        lambda v: v == [("res-1", None)]),
    "worker_pool.running_by_host": (
        lambda c, i: __import__("hermes_cli.kanban_worker_pool", fromlist=["x"]).running_by_host(c),
        lambda v: v == {"zeta-box": 1}),
    "placement_view._running": (
        lambda c, i: _strip_ids(__import__("hermes_cli.kanban_placement_view", fromlist=["x"])._running(c),
                                "task_id", "run_id"),
        lambda v: v == [{"assignee": "alpha", "host": "zeta-box", "rule": "policy"}]),
    "placement_view._held": (
        lambda c, i: sorted(r["code"] for r in
                            __import__("hermes_cli.kanban_placement_view", fromlist=["x"])._held(c)),
        lambda v: v == ["pause_hold", "requires_unsatisfied:gpu"]),
    "pause_hold._open_pause_labels": (
        lambda c, i: [(t == i["held"]) for t, *_ in
                      __import__("hermes_cli.kanban_pause_hold", fromlist=["x"])._open_pause_labels(c)],
        lambda v: v == [True]),
    "host_affinity._last_payloads": (
        lambda c, i: __import__("hermes_cli.kanban_host_affinity", fromlist=["x"])._last_payloads(
            c, [i["held"]], "placement_wait").get(i["held"], {}).get("code"),
        lambda v: v == "pause_hold"),
    "host_capabilities._open_none_wait": (
        lambda c, i: __import__("hermes_cli.kanban_host_capabilities", fromlist=["x"])._open_none_wait(
            c, i["none"]),
        lambda v: v is True),
    "drained_pin.drained_pin_waits": (
        lambda c, i: [h["reason"] for h in __import__("hermes_cli.kanban_drained_pin", fromlist=["x"])
                      .drained_pin_waits(c, {i["held"]: "pin_host_draining"}, NOW)],
        lambda v: v == ["pin_host_draining"]),
    "drained_pin._episode_since": (
        lambda c, i: __import__("hermes_cli.kanban_drained_pin", fromlist=["x"])._episode_since(c, i["held"]),
        lambda v: v == NOW - 7200),
    "dispatch._wait_history": (
        lambda c, i: [p.get("code") for _e, _c, p in
                      __import__("hermes_cli.kanban_db_dispatch", fromlist=["x"])._wait_history(
                          c, [i["held"]], NOW - 10_000)[0].get(i["held"], [])],
        lambda v: v == ["pause_hold"]),
    "studio_allowlist._spent_on_studio": (
        lambda c, i: __import__("hermes_cli.kanban_studio_allowlist", fromlist=["x"])._spent_on_studio(
            c, i["running"], 0),
        lambda v: v is False),  # the run is on a pool host, so a --once override is NOT spent
    "studio_allowlist._episode_events": (
        lambda c, i: [k for k, _c, _p in __import__("hermes_cli.kanban_studio_allowlist", fromlist=["x"])
                      ._episode_events(c, i["held"], ())],
        lambda v: v == []),  # an open (uncleared) wait is not an episode end on either name
    "continuation.backfill_origin_hosts": (
        lambda c, i: (__import__("hermes_cli.kanban_continuation", fromlist=["x"]).backfill_origin_hosts(c),
                      c.execute("SELECT host FROM task_runs WHERE id=?", (i["run"],)).fetchone()[0]),
        lambda v: v == (1, "zeta-box")),
    "placement_gates.KIND_GUARD_SQL": (
        lambda c, i: c.execute(__import__("hermes_cli.kanban_placement_gates", fromlist=["x"]).KIND_GUARD_SQL,
                               {"since": NOW - 10_000}).fetchone()[0],
        lambda v: v == 1),
    "placement_gates.check_i1": (
        lambda c, i: __import__("hermes_cli.kanban_placement_gates", fromlist=["x"]).check_i1(c, NOW - 10_000),
        lambda v: isinstance(v, dict) and bool(v)),
}


@pytest.mark.parametrize("name", sorted(READERS))
def test_reader_same_answer_on_old_and_new_kind_names(home, name):
    read, signal = READERS[name]
    old, new = _both(home, read)
    assert signal(old), f"{name}: old-name board gave {old!r}"
    assert new == old, f"{name}: new-name board gave {new!r}, old-name board {old!r}"


def test_cleared_wait_ends_episode_under_both_names(home):
    from hermes_cli import kanban_studio_allowlist as ksa
    ids = _boards(home)
    for slug, k in (("old", OLD), ("new", NEW)):
        with kb.connect_closing(board=slug) as c:
            _ev(c, ids[slug]["held"], k["wait"], {"cleared": True})
            c.commit()
            assert [kk for kk, _c, _p in ksa._episode_events(c, ids[slug]["held"], ())] == \
                ["placement_wait_cleared"], slug


def test_mixed_board_reads_latest_row_whichever_name(home):
    """A run spawned before the writer flip (old name) and its card's later wait (new name) on ONE board:
    the latest row wins, whatever its name."""
    from hermes_cli import kanban_host_affinity as kha
    ids = _boards(home)
    with kb.connect_closing(board="old") as c:
        h = ids["old"]["held"]
        _ev(c, h, "allocation_wait", {"code": "pin_host_full", "since": NOW - 5})
        c.commit()
        assert kha._last_payloads(c, [h], "placement_wait")[h]["code"] == "pin_host_full"
        assert kha._last_payloads(c, [h], "allocation_wait")[h]["code"] == "pin_host_full"
