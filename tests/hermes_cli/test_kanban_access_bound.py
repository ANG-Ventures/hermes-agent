"""Access-bound placement rule (Traceability-vs-Placement spec v1.2 §2.6,
Phase 2c, AC-23; card t_e99ef75c).

A card whose WORK is on host X (or the board) but whose ACCESS exists only on
the Studio is named ``access_bound:<cap>``: ``ssh-root:<host>`` (``ssh <alias>
... sudo ...``) or ``control-plane-read`` (a path to kanban.db / gateway.log /
load_gate.json / kanban-pool.json, or ``requires: control-plane-read``). It
used to fall out as ``host_reach`` (fail-closed residue) or needed a
hand-typed ``host: studio``. Evaluated before ``host_reach`` (Momus pass 2
R-F) and before every pin branch: a pool pin on such a card parks
``needs_input`` with the §5 contradiction line, it never waits on the pin.
Hermetic: temp HERMES_HOME, fixture PoolConfig, no spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops
from hermes_cli import kanban_placement_reasons as pr
from hermes_cli import kanban_worker_pool as kwp

NOW = 1_800_000_000.0
ALIASES = ("ci-box", "ace-ai", "ace-media", "ci", "ci.ace", "mac-studio-m3u")
PIN_ALIASES = {"ci": "ci-box", "ci.ace": "ci-box", "ci-box": "ci-box", "ace-ai": "ace-ai",
               "ace-media": "ace-media", "studio": "studio"}
SETPCI = "Step 1: ssh ci-box sudo setpci -s 01:00.0 CAP_EXP+30.w=0003:000f\n"
KANBAN_DB = "P0-1: sqlite3 -readonly ~/.hermes/kanban.db 'select count(*) from task_links'\n"


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in access-bound test")
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
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {})
    (h / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return h


def _cfg(reach=None, status="ok"):
    # Every pool host reaches every alias: host_reach would ADMIT these cards,
    # so only the access rule can hold them.
    reach = {h: {a: 0 for a in ALIASES} for h in ("ci-box", "ace-ai", "ace-media")} \
        if reach is None else reach
    return kwp.PoolConfig(hosts=("ci-box", "ace-ai", "ace-media"), profiles=("alpha",),
                          reach=reach, reach_aliases=ALIASES, reach_status=status,
                          ssh_hosts=frozenset({"ci", "ci-box", "ace-ai", "ace-media"}),
                          pin_aliases=PIN_ALIASES)


def _portable(body, cfg=None, skills=()):
    return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="alpha", body=body, skills=skills, pool=cfg or _cfg())


def _host(name, prio):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True,
                        priority=prio)


def _plan(cfg=None):
    cfg = cfg or _cfg()
    hosts = [_host("ci-box", 0), _host("ace-ai", 1), _host("ace-media", 2)]
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def _tick(conn, *, dry_run=False, spawn_limit=4):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=spawn_limit,
                            spillover=_plan(), reconcile_orphans=False, dry_run=dry_run)
    return res, spawned


# -- AC-23: the classifier ------------------------------------------------------

def test_ssh_sudo_setpci_is_access_bound_ssh_root():
    """RED ON BASE: (True, None, 'policy') with a matrix that reaches ci-box."""
    assert _portable(SETPCI) == (False, "access_bound:ssh-root:ci-box", None)


def test_ssh_alias_canonicalised_to_the_pool_host():
    """t_53129ff1's literal shape (``ssh ci sudo ...``) names the HOST, not the alias."""
    assert _portable("ssh ci sudo fio --readonly /dev/nvme0n1\n") == \
        (False, "access_bound:ssh-root:ci-box", None)


def test_kanban_db_path_is_access_bound_control_plane_read():
    assert _portable(KANBAN_DB) == (False, "access_bound:control-plane-read", None)


@pytest.mark.parametrize("body", [
    "tail -n 200 ~/.hermes/logs/gateway.log | grep pin_host\n",
    "cat ~/.hermes/fleet/kanban-pool.json\n",
    "jq . $HERMES_HOME/load_gate.json\n",
    "jq . \"${HERMES_HOME}/load_gate.json\"\n",
    "requires: control-plane-read\nRead the board.\n",
])
def test_every_control_plane_trigger(body):
    assert _portable(body) == (False, "access_bound:control-plane-read", None)


@pytest.mark.parametrize("body", [
    "Update the ci-box build sheet: storage section and the RAM table.\n",   # AC-23 bare word
    "Fix the kanban.db schema migration in hermes_cli/kanban_db.py\n",       # R-F: bare word
    "the gateway.log rotation knob and the load_gate.json writer\n",
    "ssh ci-box uptime && sudo reboot\n",          # sudo runs on the Studio, not on ci-box
    "run sudo setpci on ci-box\n",                 # no ssh shape
    "host: ci-box\nsudo is not needed here\n",     # a pin line is never a target
])
def test_prose_is_not_access_bound(body):
    """MUTATION (bare word): deriving from a bare word holds these prose cards."""
    ok, rule, _rc = _portable(body)
    assert not (rule or "").startswith(pr.ACCESS_BOUND), rule


def test_quoted_remote_command_with_and_and_still_binds():
    assert _portable("ssh ci-box 'uptime && sudo dmesg | tail'\n") == \
        (False, "access_bound:ssh-root:ci-box", None)


def test_access_bound_is_decided_before_host_reach_on_a_stale_matrix():
    """Momus R-F: with a stale matrix host_reach would claim the card; the name
    must not depend on matrix freshness."""
    stale = _cfg(reach={}, status="stale")
    assert _portable(SETPCI, stale) == (False, "access_bound:ssh-root:ci-box", None)
    assert _portable("ssh ci-box uptime\n", stale) == (False, "host_reach", None)


def test_access_bound_beats_the_pool_pin_in_portable():
    """MUTATION (let the pin win): the pin branch returns (True, None, 'pin')."""
    assert _portable("host: ci-box\n" + SETPCI) == (False, "access_bound:ssh-root:ci-box", None)


# -- AC-23: the dispatcher ------------------------------------------------------

def test_unpinned_access_bound_card_runs_local_and_says_why(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="setpci", assignee="alpha", body=SETPCI)
        res, _spawned = _tick(conn, dry_run=True)
    assert [t for t, _w, _s in res.spawned] == [tid] and not res.placed
    (entry,) = [e for e in res.routes if e["task_id"] == tid]
    assert entry["rule"] == "access_bound:ssh-root:ci-box"
    line = kanban_ops._route_line(entry, True)
    assert "rule=access_bound:ssh-root:ci-box" in line and "host_reach" not in line


def test_access_bound_card_held_local_records_the_rule(home):
    """No local slot: the hold reason is the rule, never host_reach / local_full."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p0", assignee="alpha", body=KANBAN_DB)
        res, spawned = _tick(conn, spawn_limit=0)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, pr.PLACEMENT_WAIT_EVENT)).fetchall()
    assert spawned == []
    assert res.placement_waits[tid] == "access_bound:control-plane-read"
    assert ev and "access_bound:control-plane-read" in ev[-1][0]


def test_access_bound_plus_pool_pin_parks_needs_input(home):
    """AC-23 last arm: never waits on ci-box. MUTATION (let the pin win): it
    spawns on / waits for ci-box -> RED."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="setpci", assignee="alpha", body="host: ci-box\n" + SETPCI)
        res, spawned = _tick(conn)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        comments = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (tid,))]
    assert spawned == []
    assert row[0] == "blocked"
    want = "access ssh-root:ci-box is Studio-only; remove host:ci-box or run it from the Studio"
    assert res.access_contradictions == {tid: want}
    assert want in comments
    assert tid not in (res.placement_waits or {})


def test_dry_run_reports_the_contradiction_without_parking(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="setpci", assignee="alpha", body="host: ci-box\n" + SETPCI)
        res, _spawned = _tick(conn, dry_run=True)
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert not res.spawned and status == "ready"
    assert tid in res.access_contradictions
    assert res.placement_waits[tid] == "access_bound:ssh-root:ci-box"


def test_host_studio_pin_on_access_bound_card_is_not_a_contradiction(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p0", assignee="alpha", body="host: studio\n" + KANBAN_DB)
        res, _spawned = _tick(conn, dry_run=True)
    assert [t for t, _w, _s in res.spawned] == [tid] and not res.placed
    assert res.access_contradictions == {}


# -- Prism 5bdfe1dcf55b: separators glued to a word or a closing quote ----------

@pytest.mark.parametrize("body", [
    "ssh ci uptime; sudo -n true\n",           # ``;`` attached to the word
    "ssh ci-box uptime&&sudo reboot\n",        # ``&&`` glued on both sides
    "ssh ci-box x|sudo tee /etc/motd\n",       # ``|`` glued on both sides
    "ssh ci-box \"echo hi\"; sudo x\n",         # separator right after a closing quote
    "ssh ci-box uptime ||sudo reboot\n",
    "ssh ci-box uptime& sudo reboot\n",
])
def test_attached_separator_ends_the_remote_command(body):
    """RED ON BASE (8eb418ee): the whole-token check bound these to ssh-root:ci-box."""
    ok, rule, _rc = _portable(body)
    assert not (rule or "").startswith(pr.ACCESS_BOUND), rule


@pytest.mark.parametrize("body", [
    "ssh ci-box 'uptime && sudo dmesg'\n",
    "ssh ci-box 'uptime;sudo dmesg'\n",
    "ssh ci-box \"a|sudo tee /x\"\n",
    "ssh ci-box sudo dmesg 2>&1 | tail\n",     # a redirection is not a separator
])
def test_separator_inside_quotes_still_binds(body):
    assert _portable(body) == (False, "access_bound:ssh-root:ci-box", None)


def test_pool_pinned_attached_separator_card_is_not_parked(home):
    """Prism repro: ``host: ci\\nssh ci uptime; sudo -n true`` must not park needs_input."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="up", assignee="alpha", body="host: ci-box\nssh ci uptime; sudo -n true\n")
        res, _spawned = _tick(conn, dry_run=True)
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert tid not in res.access_contradictions
    assert status == "ready"


# -- Prism 229da8245003: ownership before parking -------------------------------

def test_foreign_access_contradiction_is_not_parked(home, monkeypatch):
    """RED ON BASE: a home whose dispatch_profiles excludes ``alpha`` parked
    alpha's card blocked/needs_input."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: False))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p0", assignee="alpha",
                             body="host: ci-box\nrequires: control-plane-read\n")
        res, spawned = _tick(conn)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        n_comments = conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?",
                                  (tid,)).fetchone()[0]
    assert spawned == []
    assert row[0] == "ready"
    assert n_comments == 0
    assert tid not in res.access_contradictions
