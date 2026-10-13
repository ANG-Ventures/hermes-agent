"""DRILL replay for t_3f962f1c (NOT for commit): the five fault-injection drills
and the kill-switch round trip through the real dispatch_once, against COPIES
of the LIVE fleet/host-capabilities.json + skill-host-requires.json."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_host_capabilities import (  # noqa: F401
    _no_real_spawn, home, _mode, _host, _card, _events, _comments, _route,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_pool as kwp

LIVE = Path("/Volumes/fleet-scratch/workspaces/default/t_3f962f1c/livesnap")
DRILL = "DRILL t_9647cf3d: do NOT open Amazon or any account; run hostname and report"
REPORT = []


def _plan(hosts=("ci-box", "ace-ai", "ace-media"), dead=()):
    hs = [_host(h, i) for i, h in enumerate(hosts)]
    sbs = tuple(json.loads((LIVE / "kanban-pool.json").read_text())["studio_bound_skills"])
    cfg = kwp.PoolConfig(hosts=tuple(hosts), profiles=("alpha",), pool_hosts=tuple(hs), studio_bound_skills=sbs)

    def probe(h):
        if h.name in dead:
            return None   # probe_host returns None on ssh failure
        return (1.0, 16)
    p = kwp.plan(hs, {}, probe=probe, config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def _tick(conn, plan=None):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242
    t0 = time.monotonic()
    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=0, spawn_paused="spilling",
                            spillover=plan or _plan(), reconcile_orphans=False)
    return res, spawned, time.monotonic() - t0


@pytest.fixture
def fleet(home):
    d = home / "fleet"
    d.mkdir()
    for f in (khc.MAP_FILE, khc.INDEX_FILE):
        shutil.copy(LIVE / f, d / f)
    return d


def rec(drill, **kw):
    REPORT.append({"drill": drill, **kw})
    out = Path(__file__).with_name("drill_report.jsonl")
    with out.open("a") as fh:
        fh.write(json.dumps({"drill": drill, **kw}, default=str) + "\n")


@pytest.fixture(autouse=True)
def _etsy_skill(home):
    sk = home / "profiles" / "alpha" / "skills" / "personal" / "etsy-login"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: etsy-login\ndescription: x\n---\nbody\n", encoding="utf-8")


def _status(conn, tid):
    r = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
    return tuple(r)


def test_d1_wrong_host_pin(home, fleet, monkeypatch):
    _mode(monkeypatch, "enforce")
    with kb.connect_closing() as conn:
        a = _card(conn, body=f"host: ace-ai\n{DRILL}", skills=["amazon-cart-ops-local"])
        b = _card(conn, body=f"host: ace-ai\nadd these to the cart on :9222\n{DRILL}")
        res, spawned, dt = _tick(conn)
        rec("1a host:ace-ai + attached wave skill", status=_status(conn, a), spawned=spawned,
            comments=_comments(conn, a, prefix="pin host:"), caps=res.placement_caps.get(a), tick_s=round(dt, 3))
        rec("1a' host:ace-ai + SOFT body sniff", route=_route(conn, b)[0])
        assert _status(conn, a) == ("blocked", "needs_input")
        assert a not in [s[0] for s in spawned]


def test_d2_origin_host_absent(home, fleet, monkeypatch):
    _mode(monkeypatch, "enforce")
    with kb.connect_closing() as conn:
        pinned = _card(conn, body=f"host: ace-ai\nFix tests/foo.py\n{DRILL}")
        plain = _card(conn, body=f"Fix tests/bar.py; open a PR.\n{DRILL}")
        res, spawned, _ = _tick(conn, _plan(dead=("ace-ai",)))
        rec("2 ace-ai unreachable", spawned=spawned, pinned_wait=res.placement_waits.get(pinned),
            plain_wait=res.placement_waits.get(plain),
            wait_events=_events(conn, pinned, "placement_wait"))
        assert pinned not in [s[0] for s in spawned]
        assert res.placement_waits.get(pinned)
        assert (plain, "ace-ai") not in spawned


def test_d3_gc_workspace(home, fleet, monkeypatch, tmp_path):
    _mode(monkeypatch, "enforce")
    with kb.connect_closing() as conn:
        tid = _card(conn, body=f"Fix tests/baz.py\n{DRILL}")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "state.txt").write_text("prior run state")
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), tid))
        r1 = _route(conn, tid)[0]
        (ws / "state.txt").unlink()   # GC'd between runs
        r2 = _route(conn, tid)[0]
        b = _card(conn, body=f"restock\n{DRILL}", skills=["amazon-cart-ops-local"])
        wb = tmp_path / "wsb"
        wb.mkdir()
        (wb / "cart.json").write_text("{}")
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wb), b))
        b1 = _route(conn, b)[0]
        (wb / "cart.json").unlink()
        b2 = _route(conn, b)[0]
        rec("3 workspace emptied between runs", route_with_files=r1, route_after_gc=r2,
            browser_with_files=b1, browser_after_gc=b2)
        assert r1[0] == "local" and r2 == ("portable", None)


def test_d4_map_truncated(home, fleet, monkeypatch):
    _mode(monkeypatch, "enforce")
    raw = (fleet / khc.MAP_FILE).read_text()
    (fleet / khc.MAP_FILE).write_text(raw[: len(raw) // 2])
    with kb.connect_closing() as conn:
        browser = _card(conn, body=f"restock\n{DRILL}", skills=["etsy-login"])   # not in studio_bound? see rec
        plain = _card(conn, body=f"Fix tests/qux.py; open a PR.\n{DRILL}")
        res, spawned, _ = _tick(conn)
        _tick(conn)
        rec("4 truncated map", spawned=spawned, map_error=res.placement_caps.get("_map_error"),
            invalid_events_browser=_events(conn, browser, khc.MAP_INVALID_EVENT),
            invalid_events_plain=_events(conn, plain, khc.MAP_INVALID_EVENT),
            browser_affinity=res.placement_affinity.get(browser),
            spawn_env=khc.spawn_env(fleet, kbd._root_kanban_cfg()))
        assert (plain, spawned and spawned[0][1]) in spawned
        (fleet / khc.MAP_FILE).write_text(raw)   # restore
        res2, spawned2, _ = _tick(conn)
        b2 = _card(conn, body=f"restock\n{DRILL}", skills=["amazon-cart-ops-local"])
        res3, spawned3, _ = _tick(conn)
        rec("4 restored, next tick", map_error=res3.placement_caps.get("_map_error"),
            new_browser_card_route=res3.placement_affinity.get(b2), spawned=spawned3,
            spawn_env=khc.spawn_env(fleet, kbd._root_kanban_cfg()))
        assert b2 not in [s[0] for s in spawned3]


def test_d5_files_disagree(home, fleet, monkeypatch):
    _mode(monkeypatch, "enforce")
    idx = json.loads((fleet / khc.INDEX_FILE).read_text())
    idx["skills"]["drill-skill"] = ["teleport"]          # index names a cap the map lacks
    idx["source_hash"] = khc.index_hash(idx["skills"])
    (fleet / khc.INDEX_FILE).write_text(json.dumps(idx))
    with kb.connect_closing() as conn:
        a = _card(conn, body=f"x\n{DRILL}", skills=["drill-skill"])
        res, spawned, _ = _tick(conn)
        _tick(conn)
        rec("5a index names undefined cap", spawned=spawned, wait=res.placement_waits.get(a),
            comments=_comments(conn, a))
        assert a not in [s[0] for s in spawned] and len(_comments(conn, a)) == 1
    # 5b: the map drops browser-session (capabilities + every host row) the index uses
    m = json.loads((LIVE / khc.MAP_FILE).read_text())
    m["capabilities"].pop("browser-session")
    for h in m["hosts"].values():
        h["capabilities"] = [c for c in h["capabilities"] if c != "browser-session"]
    (fleet / khc.MAP_FILE).write_text(json.dumps(m))
    shutil.copy(LIVE / khc.INDEX_FILE, fleet / khc.INDEX_FILE)
    with kb.connect_closing() as conn:
        b = _card(conn, body=f"x\n{DRILL}", skills=["etsy-login"])
        res, spawned, _ = _tick(conn)
        rec("5b map drops browser-session", spawned=spawned, wait=res.placement_waits.get(b),
            comments=_comments(conn, b))
        assert b not in [s[0] for s in spawned] and len(_comments(conn, b)) == 1


@pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
def test_kill_switch(home, fleet, monkeypatch, mode):
    _mode(monkeypatch, mode)
    with kb.connect_closing() as conn:
        a = _card(conn, body=f"restock\n{DRILL}", skills=["etsy-login"])
        c = _card(conn, body=f"restock\n{DRILL}", skills=["amazon-cart-ops-local"])
        res, spawned, _ = _tick(conn)
        rec(f"kill-switch {mode}", spawned=spawned, etsy_aff=res.placement_affinity.get(a),
            amazon_aff=res.placement_affinity.get(c), amazon_wait=res.placement_waits.get(c),
            comments=_comments(conn, a) + _comments(conn, c),
            env=khc.spawn_env(fleet, kbd._root_kanban_cfg()))


@pytest.mark.parametrize("cfg", [{}, {"placement": {"capabilities": "enforce"}}])
def test_live_default_mode_pin_order(home, fleet, monkeypatch, cfg):
    """Live root config has no kanban.placement.capabilities key (= warn)."""
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: cfg)
    with kb.connect_closing() as conn:
        a = _card(conn, body=f"host: ace-ai\n{DRILL}", skills=["amazon-cart-ops-local"])
        b = _card(conn, body=f"host: any\n{DRILL}", skills=["amazon-cart-ops-local"])
        res, spawned, _ = _tick(conn)
        rec(f"F3 under cfg={cfg or 'ABSENT (live)'}", spawned=spawned, a_status=_status(conn, a),
            b_status=_status(conn, b), b_wait=res.placement_waits.get(b),
            env=khc.spawn_env(fleet, cfg))
