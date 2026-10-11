"""Host-bound cards never spill to a worker host with no proven SSH route
(card t_1e9ea7e9).

Three spilled workers on 2026-10-11 landed on ci-box / ace-ai with no route to
the host their card drove (studio-mint is a forced-command key, so they could
not even jump back). The rule: a card whose skills or body name a fleet host
is portable ONLY to a pool host whose fresh ``fleet/kanban-reach-matrix.json``
row proves ``rc == 0`` to every target; an absent / stale / malformed matrix
is NOT portable (fail closed). Every dispatcher test runs under the AC-5 seam.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in reach test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


NOW = 1_800_000_000.0
ALIASES = ("ace-ai", "ace-media", "ci-box", "mac-studio-m3u", "unifi")


def _matrix(fleet: Path, rows: dict, *, schema=1) -> Path:
    fleet.mkdir(parents=True, exist_ok=True)
    p = fleet / kwp.REACH_FILE
    p.write_text(json.dumps({"schema": schema, "hosts": rows}))
    return p


def _row(at=NOW, **rc):
    return {"at": at, "reach": {a: rc.get(a.replace("-", "_"), 255) for a in ALIASES}}


def _pool(fleet: Path) -> kwp.PoolConfig:
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    return kwp.PoolConfig(hosts=("ci-box", "ace-ai", "ace-media"), profiles=("alpha",),
                          reach=reach, reach_aliases=aliases, reach_status=status)


def _portable(pool, *, body=None, skills=()):
    return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="alpha", body=body, skills=skills, pool=pool)


# -- the matrix reader fails CLOSED --------------------------------------------

def test_absent_matrix_is_not_portable_for_a_host_bound_card(tmp_path):
    pool = _pool(tmp_path / "fleet")
    assert pool.reach_status == "absent" and pool.reach == {}
    assert _portable(pool, skills=("fleet-host-diagnosis",)) == (False, "host_reach", None)
    # An unbound card is untouched by the rule.
    assert _portable(pool) == (True, None, "policy")


def test_stale_matrix_is_not_portable(tmp_path):
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row(at=NOW - kwp.REACH_STALE_SECONDS - 1, ace_ai=0, unifi=0)})
    pool = _pool(fleet)
    assert pool.reach_status == "stale" and pool.reach == {}
    assert _portable(pool, body="ssh ace-ai and read dmesg", skills=("fleet-host-diagnosis",)) \
        == (False, "host_reach", None)


@pytest.mark.parametrize("text", ["not json", "[]", '{"schema": 2, "hosts": {}}',
                                  '{"schema": 1, "hosts": {"ci-box": {"at": "x", "reach": {}}}}'])
def test_malformed_matrix_is_unreadable_never_reachable(tmp_path, text):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / kwp.REACH_FILE).write_text(text)
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    assert (reach, aliases, status) == ({}, (), "unreadable")


def test_stale_rows_keep_their_aliases_in_the_vocabulary(tmp_path):
    """A target named only by a STALE row is still a target (Prism da8af4dbc8be):
    the fresh ci-box row proves ace-ai, the stale row is the only one naming
    mac-studio-m3u, and a card needing both must not spill on ace-ai alone."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": {"at": NOW, "reach": {"ace-ai": 0}},
                    "ace-media": {"at": NOW - 40 * 3600, "reach": {"ace-ai": 0, "mac-studio-m3u": 0}}})
    pool = _pool(fleet)
    assert pool.reach_status == "ok" and set(pool.reach) == {"ci-box"}
    assert set(pool.reach_aliases) == {"ace-ai", "mac-studio-m3u"}
    assert kwp.reach_allowed_hosts(body="ssh ace-ai", skills=(), pool=pool) == ("ci-box",)
    assert kwp.reach_allowed_hosts(body="ssh ace-ai then mac-studio-m3u", skills=(), pool=pool) == ()


def test_body_only_card_is_recognised_without_a_fresh_sample(tmp_path):
    """Prism 13bf80849c6b: a card whose body names a fleet host is host-bound
    even when the matrix is absent (pool hosts are always vocabulary) or
    wholly stale (the file's aliases survive); it then stays local."""
    fleet = tmp_path / "fleet"
    pool = _pool(fleet)
    assert pool.reach_status == "absent"
    assert _portable(pool, body="ssh ace-ai nvidia-smi") == (False, "host_reach", None)
    _matrix(fleet, {"ci-box": _row(at=NOW - 40 * 3600, ace_ai=0, unifi=0, mac_studio_m3u=0)})
    pool = _pool(fleet)
    assert pool.reach_status == "stale" and pool.reach == {} and "unifi" in pool.reach_aliases
    assert _portable(pool, body="ssh unifi 'unifi-os restart'") == (False, "host_reach", None)
    assert _portable(pool, body="write a doc") == (True, None, "policy")


def test_fresh_row_kept_stale_row_dropped(tmp_path):
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row(ace_ai=0), "ace-ai": _row(at=NOW - 40 * 3600, ace_ai=0)})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    assert status == "ok" and set(reach) == {"ci-box"} and "ace-ai" in aliases
    assert reach["ci-box"]["ace-ai"] == 0 and reach["ci-box"]["unifi"] == 255


# -- the classifier ---------------------------------------------------------

def test_body_alias_match_is_whole_word_and_skips_the_pin_line(tmp_path):
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row()})
    pool = _pool(fleet)
    bound, targets = kwp.host_bound_targets("host: ci-box\nssh ace-ai 'restart sshd', then ssh unifi.",
                                            (), pool.reach_aliases, pin_hosts=pool.hosts)
    assert (bound, targets) == (True, ("ace-ai", "unifi"))
    # Substrings are not aliases; the pin line is not a target.
    assert kwp.host_bound_targets("host: ci-box\nssh the ace-ai-lan box", (), pool.reach_aliases,
                                  pin_hosts=pool.hosts) == (False, ())


def test_host_bound_card_portable_only_to_a_reaching_host(tmp_path):
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row(ace_ai=0), "ace-ai": _row(), "ace-media": _row(ace_ai=0, unifi=0)})
    pool = _pool(fleet)
    body = "fix the GPU fan curve: ssh ace-ai"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ci-box", "ace-media")
    assert _portable(pool, body=body) == (True, None, "policy")
    # Two targets: only the host that reaches both.
    assert kwp.reach_allowed_hosts(body=body + "\nssh unifi", skills=(), pool=pool) == ("ace-media",)
    # Nobody reaches mac-studio-m3u: local.
    assert _portable(pool, body="ssh mac-studio-m3u 'uptime'") == (False, "host_reach", None)
    # A skill-bound card with no named alias needs a host reaching EVERYTHING.
    assert _portable(pool, skills=("ssh-machines",)) == (False, "host_reach", None)
    # A remote pin to a non-reaching host is refused (waits), host:any rides the rule.
    assert _portable(pool, body="host: ace-ai\nssh ace-ai", ) == (False, "host_reach", None)
    assert _portable(pool, body="host: ci-box\nssh ace-ai") == (True, None, "pin")
    assert _portable(pool, body="host:any\nssh ace-ai") == (True, None, "any")


def test_prose_host_line_naming_a_non_pool_alias_is_scanned(tmp_path):
    """Prism c63ba2e150ec: ``host: unifi`` is not a pin (unifi is no pool host),
    so the line is scanned as prose, and prose binds only in an ssh shape
    (t_696a9a06). A real pin line (a pool host) is still skipped."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row()})
    pool = _pool(fleet)
    assert kwp.host_bound_targets("host: unifi\nssh unifi 'restart'", (), pool.reach_aliases,
                                  pin_hosts=pool.hosts) == (True, ("unifi",))
    assert kwp.host_bound_targets("host: unifi\nrestart the controller", (), pool.reach_aliases,
                                  pin_hosts=pool.hosts) == (False, ())
    assert kwp.host_bound_targets("host: ci-box\nwrite a doc", (), pool.reach_aliases,
                                  pin_hosts=pool.hosts) == (False, ())
    # Nobody reaches unifi: the card stays local instead of spilling on policy.
    assert _portable(pool, body="host: unifi\nssh unifi 'restart'") == (False, "host_reach", None)


BURST = ("origin: ruling \"ci-box carries the bulk\".\n\nhost:ci-box\n"
         "Burst card for t_40282b7b (6 concurrent placements on ci-box). Do exactly this:\n"
         "1. Run with your terminal tool: hostname; sleep 90; echo ok\n")


@pytest.mark.parametrize("body", [
    BURST,
    "host: ci-box\nCI Box Build Sheet: the ci-box storage section, then ace-ai's RAM.",
    "check nvidia-smi on ace-ai",
    "the dashboard at http://ace-ai:8080/x is slow",
    "load the ssh-machines skill notes for ci-box",   # ssh-machines inside a word
])
def test_bare_alias_mention_is_not_host_bound(tmp_path, body):
    """t_696a9a06 RED ON BASE: a body that merely MENTIONS an alias drives
    nothing over ssh. With the live empty-for-members matrix every such card
    was held host_reach; now it routes like any card (a pool pin places)."""
    pool = _pool(tmp_path / "fleet")   # absent matrix: nothing reachable
    assert kwp.host_bound_targets(body, (), (*pool.reach_aliases, *pool.hosts),
                                  pin_hosts=pool.hosts) == (False, ())
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) is None
    assert _portable(pool, body=body)[0] is True


@pytest.mark.parametrize("body,target", [
    ("ssh ace-ai 'nvidia-smi'", "ace-ai"),
    ("run: ssh -o BatchMode=yes kanbanw@ace-ai 'mount | grep fleet'", "ace-ai"),
    ("scp report.txt ace-media:/tmp/", "ace-media"),
    ("rsync -a out/ ace-ai:~/drop/", "ace-ai"),
    ("docker -H ssh://ace-ai ps", "ace-ai"),
    ("host: ci-box\nthen `ssh ace-ai 'dmesg'`", "ace-ai"),
])
def test_ssh_shaped_mention_is_host_bound_and_held_on_an_empty_matrix(tmp_path, body, target):
    """The property t_1e9ea7e9 bought survives: an ssh-shaped card is still
    host-bound, and with no proven route it never spills."""
    pool = _pool(tmp_path / "fleet")
    bound, targets = kwp.host_bound_targets(body, (), (*pool.reach_aliases, *pool.hosts),
                                            pin_hosts=pool.hosts)
    assert bound and target in targets
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ()
    ok, rule, _ = _portable(pool, body=body)
    assert (ok, rule) == (False, "host_reach")


def test_burst_card_pinned_to_ci_box_places_on_a_members_reach_nothing_matrix(kanban_home):
    """t_696a9a06 RED ON BASE, the live shape: every pool member reaches
    nothing, only the Studio row is green; a ci-box-pinned burst card spawns
    on ci-box while an ``ssh ace-ai`` card is still held."""
    fleet = kanban_home / "fleet"
    _matrix(fleet, {"ci-box": _row(), "ace-ai": _row(),
                    "mac-studio-m3u": _row(ace_ai=0, ci_box=0, ace_media=0, mac_studio_m3u=0)})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    cfg = kwp.PoolConfig(hosts=("ci-box", "ace-ai"), profiles=("alpha",),
                         reach=reach, reach_aliases=aliases, reach_status=status)
    plan = kwp.plan([_fleet_host("ci-box", 0), _fleet_host("ace-ai", 1)], {},
                    probe=lambda h: (1.0, 16), config=cfg)
    plan.band, plan.spill_reason = "paused", "load"
    with kb.connect_closing() as conn:
        burst = kb.create_task(conn, title="ci-box burst 2/6 (t_40282b7b): hostname; sleep 90",
                               assignee="alpha", body=BURST)
        bound = kb.create_task(conn, title="gpu", assignee="alpha", body="ssh ace-ai 'nvidia-smi'")
        res, spawned = _tick(conn, plan)
    assert spawned == [(burst, "ci-box")]
    assert bound not in dict(spawned)
    # Only the Studio (no pool host) proves ace-ai: held, never spilled.
    assert res.placement_waits[bound] == "host_reach"


def test_path_qualified_skill_id_is_host_bound(tmp_path):
    """Prism e5e1f41acb09: ``operations/ssh-machines`` is the ssh-machines skill."""
    pool = _pool(tmp_path / "fleet")
    assert kwp.host_bound_targets(None, ("operations/ssh-machines",), ()) == (True, ())
    assert _portable(pool, skills=("operations/ssh-machines",)) == (False, "host_reach", None)
    assert _portable(pool, skills=("operations/not-a-host-skill",)) == (True, None, "policy")


def test_reach_rows_and_pool_hosts_compare_case_folded(tmp_path):
    """Prism d4be9ccd2c7f: reach rows are lower-cased, pool keys and pins are
    not; a mixed-case pool host that the matrix proves must still be allowed."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"CI-Box": _row(ace_ai=0)})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    cfg = kwp.PoolConfig(hosts=("CI-Box",), profiles=("alpha",), reach=reach,
                         reach_aliases=aliases, reach_status=status)
    assert _portable(cfg, body="host: CI-Box\nssh ace-ai") == (True, None, "pin")
    host = kwp.PoolHost(name="CI-Box", ssh_host="CI-Box", ssh_user=kwp.SSH_USER, slots=2,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=0)
    p = kwp.plan([host], {}, probe=lambda h: (1.0, 16), config=cfg)
    only = kwp.reach_allowed_hosts(body="ssh ace-ai", skills=(), pool=cfg)
    assert only == ("ci-box",)
    assert p.take("alpha", only=only).name == "CI-Box"
    assert p.take("alpha", pin="ci-box").name == "CI-Box"


def test_take_honours_the_reaching_set():
    def host(name, prio):
        return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2,
                            capacity_pct=0.8, absence="required", profiles=("alpha",),
                            state="active", enabled=True, priority=prio)
    p = kwp.plan([host("ci-box", 0), host("ace-media", 1)], {}, probe=lambda h: (1.0, 16))
    assert p.take("alpha").name == "ci-box"
    assert p.take("alpha", only=("ace-media",)).name == "ace-media"
    assert p.take("alpha", only=()) is None and p.refusal == "host_reach"
    assert p.take("alpha", only=("mac-studio-m3u",)) is None and p.refusal == "host_reach"


# -- the dispatcher: red on main --------------------------------------------

def _fleet_host(name, prio):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=4,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=prio)


def _tick(conn, plan, *, spawn_limit=0):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused="test",
                            spawn_limit=spawn_limit, spillover=plan, reconcile_orphans=False)
    return res, spawned


def test_fleet_host_diagnosis_card_is_never_spilled_without_a_matrix(kanban_home, caplog):
    """RED ON MAIN: a t_63ab10ab-shaped card (skill fleet-host-diagnosis, Studio-
    local body) spilled to ci-box under load. Now it classifies local and waits
    for a local slot instead."""
    fleet = kanban_home / "fleet"
    fleet.mkdir()
    pool = kwp.read_pool(fleet)  # no pool files: reach absent
    cfg = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), reach_status=pool.reach_status)
    plan = kwp.plan([_fleet_host("ci-box", 0)], {}, probe=lambda h: (1.0, 16), config=cfg)
    plan.band, plan.spill_reason = "paused", "load"
    with kb.connect_closing() as conn:
        bound = kb.create_task(conn, title="diag", assignee="alpha", skills=["fleet-host-diagnosis"],
                               body="host: mac-studio-m3u\nread the Studio's OrbStack LNP state")
        plain = kb.create_task(conn, title="plain", assignee="alpha", body="write a doc")
        with caplog.at_level("INFO", logger=kb._log.name):
            res, spawned = _tick(conn, plan)
    assert spawned == [(plain, "ci-box")]
    assert res.placement_waits[bound] == "local_full"
    assert any("classified local (host_reach)" in r.getMessage() and bound in r.getMessage()
               for r in caplog.records)


def test_host_bound_card_spills_only_to_the_reaching_host(kanban_home):
    fleet = kanban_home / "fleet"
    _matrix(fleet, {"ci-box": _row(), "ace-media": _row(ace_ai=0)})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    cfg = kwp.PoolConfig(hosts=("ci-box", "ace-media"), profiles=("alpha",),
                         reach=reach, reach_aliases=aliases, reach_status=status)
    plan = kwp.plan([_fleet_host("ci-box", 0), _fleet_host("ace-media", 1)], {},
                    probe=lambda h: (1.0, 16), config=cfg)
    plan.band, plan.spill_reason = "paused", "load"
    with kb.connect_closing() as conn:
        bound = kb.create_task(conn, title="gpu", assignee="alpha", body="ssh ace-ai nvidia-smi")
        res, spawned = _tick(conn, plan)
    assert spawned == [(bound, "ace-media")]
