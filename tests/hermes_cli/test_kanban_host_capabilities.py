"""Fleet Work Placement Phase 1: host capabilities (card t_8c474c2e).

Spec: Obsidian ``AI/Infrastructure/Fleet Work Placement -- Host Affinity and
Continuation (spec, 2026-10-11).md`` v2, s7 Phase 1 tests 1-9 (test 10 is the
mutation run recorded on the card). Hermetic: both JSON files come from
``tests/fixtures/kanban_placement``, the root config is monkeypatched, and the
real spawn path raises (AC-5 seam, same as test_kanban_worker_pool).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_pool as kwp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement"
_REAL_DEFAULT_SPAWN = kbd._default_spawn
WAVE = "amazon-cart-ops-local"


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (h / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    # The wave skill resolves for the profile, so the skill-existence
    # preflight never refuses a card the capability layer lets spill.
    sk = h / "profiles" / "alpha" / "skills" / "personal" / WAVE
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(f"---\nname: {WAVE}\ndescription: x\n---\nbody\n", encoding="utf-8")
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return h


@pytest.fixture
def fleet(home):
    d = home / "fleet"
    d.mkdir()
    for f in (khc.MAP_FILE, khc.INDEX_FILE):
        shutil.copy(FIXTURES / f, d / f)
    return d


def _mode(monkeypatch, mode="enforce", **extra):
    pl = {"capabilities": mode, **extra}
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {"placement": pl})


def _host(name, prio):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True,
                        priority=prio)


def _plan(free=2):
    hosts = [_host("ace-ai", 0), _host("ace-media", 1)]
    cfg = kwp.PoolConfig(hosts=("ace-ai", "ace-media"), profiles=("alpha",), pool_hosts=tuple(hosts))
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def _tick(conn, *, spawn_limit=0, spawned=None, **kw):
    spawned = [] if spawned is None else spawned

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=spawn_limit,
                            spawn_paused="spilling" if spawn_limit == 0 else None,
                            spillover=_plan(), reconcile_orphans=False, **kw)
    return res, spawned


def _card(conn, body=None, skills=None):
    return kb.create_task(conn, title="card", assignee="alpha", body=body, skills=skills)


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _comments(conn, tid, prefix=khc.COMMENT_PREFIX):
    return [r[0] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,)) if r[0].startswith(prefix)]


def _route(conn, tid, **kw):
    row = conn.execute("SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker "
                       "FROM tasks WHERE id=?", (tid,)).fetchone()
    plan = _plan()
    caps = kbd._capability_tick(plan.config)
    out: dict = {}
    return kbd._route_row(conn, row, plan, None, {}, {}, affinity_out=out, caps=caps, **kw), out


# -- 1-2: attached / named skills ---------------------------------------------

def test_requires_skill_attached_stays_local(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="restock the shelf", skills=[WAVE])
        (route, out) = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert out[tid]["rule"] == "requires:browser-session"
        assert out[tid]["matched_by"] == "skill" and out[tid]["strength"] == "hard"
        # The dry-run column shows the pin with no pool plan and writes nothing.
        dry = kbd.dispatch_once(conn, dry_run=True, max_spawn=8, spillover=None,
                                reconcile_orphans=False)
        assert dry.placement_affinity[tid]["rule"] == "requires:browser-session"
        assert _events(conn, tid, "placement_affinity") == [] and _comments(conn, tid) == []
        res, spawned = _tick(conn)
        assert tid not in [t for t, _ in spawned]
        assert res.placement_waits[tid] == "studio_affinity:requires:browser-session"


def test_requires_named_skill_in_body_stays_local(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="Load `browser-access` and check the order page.")
        (route, out) = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert out[tid]["matched_by"] == "named_skill" and out[tid]["strength"] == "soft"
        # A mention that is not a skill reference does not pin.
        plain = _card(conn, body="the browser-access-log rotation script")
        assert _route(conn, plain)[0] == ("portable", None)


# -- 3: body sniff, one comment, survives a restart ------------------------------

def test_body_sniff_pins_and_comments(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="add these to the cart on :9222")
        res, spawned = _tick(conn)
        assert spawned == []
        assert res.placement_affinity[tid]["rule"] == "requires:browser-session"
        assert res.placement_affinity[tid]["matched_by"] == "body"
        got = _comments(conn, tid)
        assert len(got) == 1 and got[0].startswith(
            'auto-pinned: browser-session (body ":9222")'), got
        assert "host: any" in got[0]
        _tick(conn)
        assert len(_comments(conn, tid)) == 1
    # Simulated dispatcher restart: a fresh connection, no in-process state.
    import importlib
    importlib.reload(khc)
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with kb.connect_closing() as conn:
        _tick(conn)
        assert len(_comments(conn, tid)) == 1
        assert len([e for e in _events(conn, tid, "placement_affinity") if not e.get("cleared")]) == 1


# -- 4: overrides and the rule order (F3) ----------------------------------------

def test_body_sniff_overridden_by_host_any(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="host: any\nadd these to the cart on :9222")
        assert _route(conn, tid)[0] == ("portable", None)


def test_named_skill_overridden_by_requires_none(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: none\nThe PAT lives in `github-login`; API work only.")
        assert _route(conn, tid)[0] == ("portable", None)
        # ``none`` never overrides a HARD requirement.
        hard = _card(conn, body="requires: none", skills=[WAVE])
        assert _route(conn, hard)[0] == ("local", "studio_affinity:requires:browser-session")


def test_skill_requires_not_overridden_by_host_any(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="host: any\nrestock", skills=[WAVE])
        assert _route(conn, tid)[0] == ("local", "studio_affinity:requires:browser-session")


def test_attached_skill_with_pool_pin_needs_input(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="host: ace-ai\nrestock", skills=[WAVE])
        res, spawned = _tick(conn)
        assert spawned == [] and tid not in [p[0] for p in res.placed]
        status = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        assert tuple(status) == ("blocked", "needs_input")
        got = _comments(conn, tid, prefix="pin host:ace-ai")
        assert got and got[0].startswith(
            "pin host:ace-ai lacks browser-session; remove the pin or the requirement")
        # A SOFT requirement never overrides an explicit pool pin.
        soft = _card(conn, body="host: ace-ai\nadd these to the cart")
        assert _route(conn, soft)[0] == ("pin", "ace-ai")


# -- 5: the control card ----------------------------------------------------------

def test_plain_coding_card_still_spills(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="Fix the flaky test in tests/foo.py; open a PR.")
        res, spawned = _tick(conn)
        assert spawned == [(tid, "ace-ai")]
        assert tid not in res.placement_affinity and _comments(conn, tid) == []


# -- 6: absent map, byte-identical routes ---------------------------------------

def test_absent_map_is_byte_identical(home, monkeypatch):
    """No fleet/host-capabilities.json: the route table of 50 real card bodies
    equals the one the BASE commit (e10ab66274) produced."""
    _mode(monkeypatch)
    sys.path.insert(0, str(FIXTURES))
    try:
        import bodies_driver as d
    finally:
        sys.path.remove(str(FIXTURES))
    assert not (home / "fleet" / khc.MAP_FILE).exists()
    with kb.connect_closing() as conn:
        ids = d.create_cards(kb, conn)
        assert d.routes(kb, kbd, kwp, conn, ids) == d.GOLDEN.read_text(encoding="utf-8")


# -- 7: kill switch warn (and off) ---------------------------------------------

def test_kill_switch_warn(home, fleet, monkeypatch):
    _mode(monkeypatch, "warn")
    with kb.connect_closing() as conn:
        tid = _card(conn, body="restock", skills=[WAVE])
        res, spawned = _tick(conn)
        assert spawned == [(tid, "ace-ai")]                      # route unchanged: it spills
        assert tid not in res.placement_affinity
        assert res.placement_caps[tid]["warn"]["rule"] == "requires:browser-session"
        aff = _events(conn, tid, "placement_affinity")
        assert aff and aff[-1]["mode"] == "warn" and aff[-1]["matched_by"] == "skill"
        assert _comments(conn, tid) == []                        # no comment in warn
        task = kb.get_task(conn, tid)
    # The spawn env carries the mode (F5): the real _default_spawn, Popen faked.
    import subprocess
    captured = {}

    class FakeProc:
        pid = 12345

    def fake_popen(cmd, *a, **kw):
        captured["env"] = kw.get("env", {})
        return FakeProc()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_default_spawn", _REAL_DEFAULT_SPAWN)
    assert task is not None
    kbd._default_spawn(task, str(home / "ws"))
    assert json.loads(captured["env"][khc.CAPS_ENV]) == {"*": "warn"}
    _mode(monkeypatch, "off")
    assert json.loads(khc.spawn_env(fleet, kbd._root_kanban_cfg())) == {"*": "off"}
    _mode(monkeypatch, "enforce")
    hard = {c for c, r in json.loads((fleet / khc.MAP_FILE).read_text())["capabilities"].items()
            if r.get("soft") is False}   # the map's per-class hardness (t_90887924)
    assert json.loads(khc.spawn_env(fleet, kbd._root_kanban_cfg())) == {
        "*": "warn", "browser-session": "enforce", "screen": "enforce", **{c: "enforce" for c in hard}}
    (fleet / khc.MAP_FILE).unlink()   # absent map = off for the workers too
    assert json.loads(khc.spawn_env(fleet, kbd._root_kanban_cfg())) == {"*": "off"}


# -- 8: unknown requires: word (F4) ----------------------------------------------

def test_unknown_capability_stays_local(home, fleet, monkeypatch):
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: browser_session\nlog in and fetch the invoice")
        res, spawned = _tick(conn)
        assert spawned == []
        assert res.placement_waits[tid] == "studio_affinity:requires:unknown:browser_session"
        got = _comments(conn, tid)
        assert len(got) == 1 and 'unknown capability "browser_session"' in got[0]
        assert "browser-session" in got[0] and "screen" in got[0]   # names the known words
        _tick(conn)
        assert len(_comments(conn, tid)) == 1


# -- 9: malformed map (F11) ------------------------------------------------------

def test_malformed_map_degrades_to_warn(home, fleet, monkeypatch):
    _mode(monkeypatch)
    (fleet / khc.MAP_FILE).write_text('{"schema": 1, "capabilities": {"browser-sess', encoding="utf-8")
    with kb.connect_closing() as conn:
        # A requirement only the map derives (attached wave skill) is warn now.
        browser = _card(conn, body="restock the shelf", skills=[WAVE])
        plain = _card(conn, body="Fix the flaky test in tests/foo.py")
        # #1877's own body rule is today's behaviour and stays live while the
        # map cannot be trusted (the conservative direction).
        legacy = _card(conn, body="attach to the signed-in browser")
        res, spawned = _tick(conn)
        # Pool not refused: both spill (the browser card is NOT pinned; warn only).
        assert sorted(spawned) == sorted([(browser, "ace-ai"), (plain, "ace-ai")])
        assert browser not in res.placement_affinity
        assert res.placement_waits[legacy] == "studio_affinity:studio_body"
        assert res.placement_caps["_map_error"].startswith(khc.MAP_FILE)
        inv = _events(conn, browser, khc.MAP_INVALID_EVENT)
        assert len(inv) == 1 and inv[0]["mode"] == "warn"
        assert json.loads(khc.spawn_env(fleet, kbd._root_kanban_cfg())) == {"*": "warn"}
        assert _comments(conn, browser) == []
