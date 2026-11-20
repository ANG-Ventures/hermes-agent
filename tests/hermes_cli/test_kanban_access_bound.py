"""Access-bound placement rule (Traceability-vs-Placement spec v1.2 §2.6,
Phase 2c, AC-23; card t_e99ef75c).

A card whose WORK is on host X (or the board) but whose ACCESS exists only on
the Studio is named ``access_bound:<cap>``: ``ssh-root:<host>`` (``ssh <alias>
... sudo ...``) or ``control-plane-read`` (``requires: control-plane-read``; since t_b720fdea a
live path to kanban.db / gateway.log / load_gate.json / kanban-pool.json no
longer binds, it is a create-time notice). It
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
KANBAN_DB_PROSE = "P0-1: sqlite3 -readonly ~/.hermes/kanban.db 'select count(*) from task_links'\n"
KANBAN_DB = "requires: control-plane-read\n" + KANBAN_DB_PROSE


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
    "requires: control-plane-read\nRead the board.\n",
    "requires: control-plane-read\ntail -n 200 ~/.hermes/logs/gateway.log | grep pin_host\n",
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


@pytest.mark.parametrize("body", [
    # t_e32e34f0's shape: a PR that edits the TRACKED repo copy (every pool clone has it).
    "Edit fleet/kanban-pool.json in hermes-home: add ace-media to priority, open a PR.\n",
    "files: `fleet/kanban-pool.json`, scripts/kanban-pool-lint.py\n",
    "- M fleet/kanban-pool.json\n- A tests/fixtures/state/kanban.db\n",
    "scripts/tools/load_gate.json is the fixture; tests/data/gateway.log the sample\n",
])
def test_repo_relative_control_plane_path_is_portable(body):
    """RED ON BASE: (False, 'access_bound:control-plane-read', None) - any path shape bound (t_e7c03f48)."""
    assert _portable(body) == (True, None, "policy")


LIVE_PATHS = [
    (KANBAN_DB_PROSE, "~/.hermes/kanban.db"),                                   # ~-rooted live board
    ("sqlite3 /Users/alexgierczyk/.hermes/kanban.db .tables\n", "/Users/alexgierczyk/.hermes/kanban.db"),
    ("jq . $HOME/.hermes/fleet/kanban-pool.json\n", "$HOME/.hermes/fleet/kanban-pool.json"),
    ("tail -n 200 ~/.hermes/logs/gateway.log | grep pin_host\n", "~/.hermes/logs/gateway.log"),
    ("jq . $HERMES_HOME/load_gate.json\n", "$HERMES_HOME/load_gate.json"),
    ("jq . \"${HERMES_HOME}/load_gate.json\"\n", "${HERMES_HOME}/load_gate.json"),
    ("tail logs/gateway.log\n", "logs/gateway.log"),                             # the live logs/ dir
    ("jq . state/load_gate.json\n", "state/load_gate.json"),                     # the live state/ dir
    ("cat ./kanban.db\n", "./kanban.db"),
    ("Edit fleet/kanban-pool.json, then check ~/.hermes/kanban.db\n", "~/.hermes/kanban.db"),
]


@pytest.mark.parametrize("body,path", LIVE_PATHS)
def test_live_control_plane_path_no_longer_pins(body, path):
    """t_b720fdea, RED ON BASE: (False, 'access_bound:control-plane-read', None). A body that only NAMES a live
    control-plane file runs on a pool host (it reads the Studio copy over ``ssh studio``); the path is reported."""
    ok, rule, _rc = _portable(body)
    assert not (rule or "").startswith(pr.ACCESS_BOUND), rule
    assert kwp.control_plane_prose(body) == (path,)


@pytest.mark.parametrize("body,_path", LIVE_PATHS)
def test_declared_control_plane_read_still_binds_and_is_not_reported(body, _path):
    """MUTATION (bind nothing): the explicit declaration keeps the card on the Studio, and no notice fires."""
    declared = "requires: control-plane-read\n" + body
    assert _portable(declared) == (False, "access_bound:control-plane-read", None)
    assert kwp.control_plane_prose(declared) == ()


@pytest.mark.parametrize("body", [
    "Edit fleet/kanban-pool.json in hermes-home: add ace-media to priority, open a PR.\n",
    "Fix the kanban.db schema migration in hermes_cli/kanban_db.py\n",
    "The board database file in the Studio home is read over ssh studio.\n",
])
def test_prose_without_a_live_path_is_not_reported(body):
    assert kwp.control_plane_prose(body) == ()


def test_quoted_remote_command_with_and_and_still_binds():
    assert _portable("ssh ci-box 'uptime && sudo dmesg | tail'\n") == \
        (False, "access_bound:ssh-root:ci-box", None)


def test_access_bound_is_decided_before_host_reach_on_a_stale_matrix():
    """Momus R-F: with a stale matrix host_reach would claim the card; the name
    must not depend on matrix freshness."""
    stale = _cfg(reach={}, status="stale")
    assert _portable(SETPCI, stale) == (False, "access_bound:ssh-root:ci-box", None)
    assert _portable("reach: ci-box\nssh ci-box uptime\n", stale) == (False, "host_reach", None)


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


# -- t_79c2259b: ssh-root access a pool capability serves -------------------------

NAS = "Run: ssh nas-truenas 'id -un; sudo -n id -u; sudo -n /sbin/zfs list -H -o name boot-pool'\n"
NAS_MAP = {
    "schema": 1,
    "capabilities": {"nas-admin": {"what": "root on the NAS", "soft": False, "ssh_root": ["NAS-TrueNAS"]},
                     "docker": {}},
    "hosts": {"studio": {"capabilities": ["nas-admin", "docker"]},
              "ci-box": {"capabilities": ["nas-admin"]}, "ace-media": {"capabilities": ["nas-admin"]},
              "ace-ai": {"capabilities": []}},
}


def _nas_cfg():
    aliases = (*ALIASES, "nas-truenas")
    reach = {h: {a: 0 for a in aliases} for h in ("ci-box", "ace-ai", "ace-media")}
    return kwp.PoolConfig(hosts=("ci-box", "ace-ai", "ace-media"), profiles=("alpha",),
                          reach=reach, reach_aliases=aliases, reach_status="ok",
                          ssh_hosts=frozenset({"ci", "ci-box", "ace-ai", "ace-media", "nas-truenas"}),
                          pin_aliases=PIN_ALIASES)


def _nas_ctx(doc=None, mode="enforce", pool=("ci-box", "ace-ai", "ace-media")):
    import json as _json
    from hermes_cli import kanban_host_capabilities as khc
    cmap = khc.map_from_bytes(_json.dumps(doc or NAS_MAP).encode(), b"{}")
    return khc.CapabilityTick(cmap, mode, (), tuple(pool))


def test_served_ssh_root_alias_is_skipped_and_the_next_access_still_binds():
    al = ("nas-truenas", "ci-box")
    assert kwp.access_bound_cap(NAS, al) == "ssh-root:nas-truenas"
    assert kwp.access_bound_cap(NAS, al, served={"nas-truenas"}) is None
    # Only the served alias is released: a second, unserved ssh-root on the card still binds.
    assert kwp.access_bound_cap(NAS + "ssh ci-box sudo dmesg\n", al, served={"nas-truenas"}) == \
        "ssh-root:ci-box"
    assert kwp.access_bound_cap(NAS + KANBAN_DB, al, served={"nas-truenas"}) == "control-plane-read"


def test_portable_releases_only_a_served_access():
    """RED ON BASE: portable() had no access_served; every NAS card was access_bound (t_f6720e3e)."""
    base = dict(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                assignee="alpha", body=NAS, pool=_nas_cfg())
    assert kwp.portable(**base) == (False, "access_bound:ssh-root:nas-truenas", None)
    assert kwp.portable(**base, access_served=frozenset({"nas-truenas"})) == (True, None, "policy")


def test_map_parses_ssh_root_and_rejects_a_bad_shape():
    from hermes_cli import kanban_host_capabilities as khc
    ctx = _nas_ctx()
    assert ctx.cmap.valid and ctx.cmap.ssh_root == {"nas-truenas": "nas-admin"}
    bad = {**NAS_MAP, "capabilities": {**NAS_MAP["capabilities"], "nas-admin": {"ssh_root": "nas-truenas"}}}
    cm = _nas_ctx(bad).cmap
    assert not cm.valid and "ssh_root" in cm.error and cm.ssh_root == {}
    assert khc.ssh_root_hits("ssh nas-truenas uptime\n", ctx.cmap) == []   # no sudo: not root access


def test_ssh_root_on_a_served_alias_derives_a_hard_requirement():
    from hermes_cli import kanban_host_capabilities as khc
    reqs, _none = khc.derive(NAS, (), _nas_ctx().cmap)
    (req,) = [r for r in reqs if r.cap == "nas-admin"]
    assert (req.strength, req.matched_by, req.term) == (khc.HARD, khc.ACCESS, "ssh-root:nas-truenas")
    v = khc.evaluate(NAS, (), _nas_ctx())
    assert v.only == ("ci-box", "ace-media") and v.hard_rule is None


@pytest.mark.parametrize("variant", ["prose_advisory", "identity_only", "tighten"])
def test_served_ssh_root_requirement_is_never_dropped_as_prose(variant, monkeypatch):
    """Prism 86b7cb2c2b27: served_ssh_root() releases access_bound, so the HARD requirement must survive every
    body/prose drop path (prose_pins_advisory, body_requires_mode advisory, studio tighten) when no pool host
    advertises the capability. RED ON 4ee4a8cf: matched_by was BODY and each path dropped it -> card portable."""
    import dataclasses
    from hermes_cli import kanban_host_capabilities as khc
    from hermes_cli import kanban_studio_capability as ksc
    row = {"what": "root on the NAS", "ssh_root": ["nas-truenas"]}   # no ``soft: false``: not a hard class
    doc = {**NAS_MAP, "capabilities": {**NAS_MAP["capabilities"], "nas-admin": row},
           "hosts": {"studio": {"capabilities": ["nas-admin", "docker"]}, "ci-box": {"capabilities": []},
                     "ace-media": {"capabilities": []}, "ace-ai": {"capabilities": []}}}
    if variant == "identity_only":
        doc["hosts"]["studio"]["studio_tier"] = {"nas-admin": khc.IDENTITY_ONLY}
    cmap = khc.map_from_bytes(__import__("json").dumps(doc).encode(), b"{}")
    assert cmap.valid, cmap.error
    ctx = khc.CapabilityTick(cmap, "enforce", ("nas-admin",), ("ci-box", "ace-ai", "ace-media"),
                             body_mode=khc.ADVISORY, prose_advisory=(variant == "prose_advisory"))
    monkeypatch.setattr(ksc, "BODY_TIGHTENED", frozenset({"nas-admin"}))
    assert khc.served_ssh_root(ctx) == {"nas-truenas"}   # access_bound lets go of the alias...
    v = khc.evaluate(NAS, (), ctx)
    if variant == "tighten":
        v = ksc.tighten(v)
    # ...so the capability layer must still hold the card: no pool host may run it.
    assert not v.body_ignored, v.body_ignored
    assert v.hard_rule == "requires:nas-admin" or v.wait == "requires_unsatisfied:nas-admin", dataclasses.asdict(v)
    ok, rule, _ = kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                               assignee="alpha", body=NAS, pool=_nas_cfg(), requires=v.for_portable(),
                               access_served=khc.served_ssh_root(ctx))
    assert not ok and rule == "requires:nas-admin", (ok, rule)


@pytest.mark.parametrize("mode,pool,want", [
    ("enforce", ("ci-box", "ace-ai", "ace-media"), {"nas-truenas"}),   # a real hold: placed on advertisers
    ("warn", ("ci-box", "ace-media"), {"nas-truenas"}),                # warn, but every pool host has it
    ("warn", ("ci-box", "ace-ai", "ace-media"), set()),                # warn and ace-ai lacks it: Studio, as today
    ("off", ("ci-box", "ace-media"), set()),
])
def test_served_ssh_root_needs_a_hold_or_a_whole_pool(mode, pool, want):
    from hermes_cli import kanban_host_capabilities as khc
    assert khc.served_ssh_root(_nas_ctx(mode=mode, pool=pool)) == want
    assert khc.served_ssh_root(None) == frozenset()


def test_nas_admin_card_starts_on_a_pool_host_that_advertises_it(home, monkeypatch):
    """Acceptance (t_79c2259b F2): t_f6720e3e's body was placed local by access_bound:ssh-root:nas-truenas.
    MUTATION (drop access_served in _route_row): the card spawns on the Studio -> RED."""
    ctx = _nas_ctx()
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: ctx)
    p = kwp.plan([_host("ci-box", 0), _host("ace-ai", 1), _host("ace-media", 2)], {},
                 probe=lambda h: (1.0, 16), config=_nas_cfg())
    p.band, p.spill_reason = "spilling", "load"
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="nas probe", assignee="alpha", body=NAS)
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spillover=p,
                                reconcile_orphans=False)
    assert spawned and spawned[0][0] == tid and spawned[0][1] in ("ci-box", "ace-media"), (spawned, res.routes)


# -- t_b720fdea: the create-time notice that replaced the body pin --------------------------

def _comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]


def test_create_names_the_live_path_and_the_fix(home, capsys):
    """RED ON BASE: no notice (the path pinned the card to the Studio instead)."""
    from hermes_cli import kanban_studio_pin as ksp
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p0", assignee="alpha", body=KANBAN_DB_PROSE)
        notes = [c for c in _comments(conn, tid) if c.startswith(ksp.CP_PROSE_PREFIX)]
    assert len(notes) == 1, notes
    assert "`~/.hermes/kanban.db`" in notes[0]
    assert "requires: control-plane-read" in notes[0] and "ssh studio" in notes[0]
    assert ksp.CP_PROSE_PREFIX in capsys.readouterr().err


def test_create_with_the_declaration_posts_no_notice(home, capsys):
    from hermes_cli import kanban_studio_pin as ksp
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p0", assignee="alpha", body=KANBAN_DB)
        assert not [c for c in _comments(conn, tid) if c.startswith(ksp.CP_PROSE_PREFIX)]
    assert ksp.CP_PROSE_PREFIX not in capsys.readouterr().err
