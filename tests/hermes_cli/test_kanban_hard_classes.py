"""Control-Plane Protection spec v0.3, Phase 2-pre: the HARD classes (card t_90887924).

AC-5 (docker), AC-11 (a) studio-state, (b) fleet-ssh through BOTH doors.
Hermetic: the fixture map carries the three classes with ``soft: false`` and a
Studio ``studio_tier: {docker: identity-only}``; the reach matrix is a fixture
PoolConfig. The flip (``kanban.placement.default: remote``) does not exist yet
(Phase 2, uncut), so "under default: remote with a healthy pool" is exercised
as the spill band with every pool host ok and free (the only path today that
offers a portable row to the pool); the assertion is on the ROUTE, which is
the same function either way.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_pool as kwp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement"


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
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    d = h / "fleet"
    d.mkdir()
    for f in (khc.MAP_FILE, khc.INDEX_FILE):
        shutil.copy(FIXTURES / f, d / f)
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {"placement": {"capabilities": "enforce"}})
    return h


def _map(home, mutate):
    p = home / "fleet" / khc.MAP_FILE
    doc = json.loads(p.read_text())
    mutate(doc)
    p.write_text(json.dumps(doc))


def _host(name, prio):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True,
                        priority=prio)


# The reach matrix as the writer now publishes it: `ci` is in the vocabulary
# (from ~/.ssh/config) and no pool row reaches it.
ALIASES = ("ci-box", "ace-ai", "ace-media", "ci", "ci.ace", "mac-studio-m3u")
REACH = {"ace-ai": {a: 255 for a in ALIASES if a not in ("ci", "ci.ace")},
         "ace-media": {a: 255 for a in ALIASES if a not in ("ci", "ci.ace")}}


def _plan(aliases=ALIASES):
    hosts = [_host("ace-ai", 0), _host("ace-media", 1)]
    cfg = kwp.PoolConfig(hosts=("ace-ai", "ace-media"), profiles=("alpha",), pool_hosts=tuple(hosts),
                         reach=REACH, reach_aliases=tuple(aliases), reach_status="ok")
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def _tick(conn, *, spawn_limit=4, plan=None):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=spawn_limit,
                            spawn_paused=None if spawn_limit else "spilling",
                            spillover=plan or _plan(), reconcile_orphans=False)
    return res, spawned


def _route(conn, tid, plan=None):
    row = conn.execute("SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker "
                       "FROM tasks WHERE id=?", (tid,)).fetchone()
    plan = plan or _plan()
    caps = kbd._capability_tick(plan.config)
    aff, out = {}, {}
    return kbd._route_row(conn, row, plan, None, {}, {}, affinity_out=aff, caps=caps,
                          caps_out=out), aff, out


def _card(conn, body):
    return kb.create_task(conn, title="card", assignee="alpha", body=body)


def _comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))
            if r[0].startswith(khc.COMMENT_PREFIX)]


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]


# -- AC-11(a) studio-state --------------------------------------------------------

def test_kanban_db_body_routes_local_with_a_healthy_pool(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, "Count blocked runs: sqlite3 -readonly ~/.hermes/kanban.db 'select ...'")
        (route, aff, _out) = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:studio-state")
        res, spawned = _tick(conn)
        assert spawned == [(tid, "studio")]
        assert not res.placed


def test_studio_state_soft_true_spills(home):
    """Kill switch / negative: the same body under `soft: true` routes portable."""
    _map(home, lambda d: d["capabilities"]["studio-state"].update(soft=True))
    with kb.connect_closing() as conn:
        tid = _card(conn, "Count blocked runs: sqlite3 -readonly ~/.hermes/kanban.db 'select ...'")
        assert _route(conn, tid)[0] == ("portable", None)


# -- AC-11(b) fleet-ssh, two doors ----------------------------------------------------

def test_ssh_ci_sudo_is_local_through_both_doors(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, "Step 1: ssh ci sudo setpci -s 80:01.1 CAP_EXP+30.w=0003:000f")
        (route, _aff, out) = _route(conn, tid)
        assert route[0] == "local"
        reasons = out["reasons"][tid]
        assert "host_reach" in reasons and "requires:fleet-ssh" in reasons, reasons


def test_without_the_matrix_alias_only_the_map_door_holds(home):
    """MUTATION (AC-11b): `ci` absent from the matrix aliases -> only fleet-ssh."""
    with kb.connect_closing() as conn:
        tid = _card(conn, "Step 1: ssh ci sudo setpci -s 80:01.1 CAP_EXP+30.w=0003:000f")
        plan = _plan(aliases=tuple(a for a in ALIASES if a not in ("ci", "ci.ace")))
        (route, _aff, out) = _route(conn, tid, plan)
        assert route[0] == "local"
        assert out["reasons"][tid] == ["requires:fleet-ssh"]


def test_fleet_ssh_soft_true_leaves_only_the_matrix(home):
    _map(home, lambda d: d["capabilities"]["fleet-ssh"].update(soft=True))
    with kb.connect_closing() as conn:
        tid = _card(conn, "Step 1: ssh ci sudo setpci -s 80:01.1 CAP_EXP+30.w=0003:000f")
        (route, _aff, out) = _route(conn, tid)
        assert route == ("local", None) and tid not in out.get("reasons", {})


# -- AC-5 docker -----------------------------------------------------------------------

DOCKER = "Bring the drill stack up: docker compose up -d, then run the e2e."


def test_docker_waits_when_no_pool_host_advertises(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, DOCKER)
        res, spawned = _tick(conn)
        assert spawned == [] and not res.placed
        assert res.placement_waits[tid] == "requires_unsatisfied:docker"
        assert "spawned" not in _kinds(conn, tid)
        (comment,) = _comments(conn, tid)
        assert comment.startswith("auto-pinned: docker (body ") and "host: studio" in comment


def test_docker_goes_only_to_the_advertising_host(home):
    _map(home, lambda d: d["hosts"]["ace-media"]["capabilities"].append("docker"))
    with kb.connect_closing() as conn:
        tid = _card(conn, DOCKER)
        res, spawned = _tick(conn)
        assert spawned == [(tid, "ace-media")]       # ace-ai is first in priority and free
        assert res.placed == [(tid, "ace-media")]
        assert _comments(conn, tid)[0].startswith("auto-pinned: docker")


def test_docker_with_a_studio_pin_spawns_on_the_studio(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, "host: studio\n" + DOCKER)
        res, spawned = _tick(conn)
        assert spawned == [(tid, "studio")] and not res.placed


def test_docker_soft_true_no_advertiser_spawns_locally(home):
    """MUTATION (AC-5): `soft: true` -> the no-advertiser case spawns locally."""
    _map(home, lambda d: d["capabilities"]["docker"].update(soft=True))
    with kb.connect_closing() as conn:
        tid = _card(conn, DOCKER)
        _res, spawned = _tick(conn)
        assert spawned == [(tid, "studio")]


def test_docker_missing_from_sniff_routes_plain_portable(home):
    """MUTATION (AC-5): delete docker from the sniff list -> plain portable to a
    non-docker host, no auto-pin comment."""
    def m(d):
        d["capabilities"]["docker"]["sniff"] = []
        d["hosts"]["ace-media"]["capabilities"].append("docker")
    _map(home, m)
    with kb.connect_closing() as conn:
        tid = _card(conn, DOCKER)
        _res, spawned = _tick(conn, spawn_limit=0)
        assert spawned == [(tid, "ace-ai")] and _comments(conn, tid) == []


def test_kanbanw_in_the_docker_group_fails_the_rootless_probe(home):
    """The probe text is the map's contract: a rootful engine reports no
    `rootless` security option, so grep -q exits 1 (Momus R8)."""
    import subprocess
    probe = json.loads((home / "fleet" / khc.MAP_FILE).read_text())["capabilities"]["docker"]["probe"]
    rootful = "echo '27.3.1 [name=apparmor name=seccomp,profile=builtin name=cgroupns]'"
    rootless = "echo '27.3.1 [name=seccomp,profile=builtin name=rootless name=cgroupns]'"
    for fake, rc in ((rootful, 1), (rootless, 0)):
        cmd = probe.replace("docker info --format '{{.ServerVersion}} {{.SecurityOptions}}'", fake)
        assert subprocess.run(["sh", "-c", cmd]).returncode == rc
