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
    assert kwp.reach_allowed_hosts(body="reach: ace-ai", skills=(), pool=pool) == ("ci-box",)
    # Both hosts declared (t_efd1497a: only a reach: line binds).
    assert kwp.reach_allowed_hosts(body="reach: ace-ai, mac-studio-m3u", skills=(), pool=pool) == ()


def test_body_only_card_is_recognised_without_a_fresh_sample(tmp_path):
    """Prism 13bf80849c6b: a card whose body names a fleet host is host-bound
    even when the matrix is absent (pool hosts are always vocabulary) or
    wholly stale (the file's aliases survive); it then stays local."""
    fleet = tmp_path / "fleet"
    pool = _pool(fleet)
    assert pool.reach_status == "absent"
    assert _portable(pool, body="reach: ace-ai\nssh ace-ai nvidia-smi") == (False, "host_reach", None)
    _matrix(fleet, {"ci-box": _row(at=NOW - 40 * 3600, ace_ai=0, unifi=0, mac_studio_m3u=0)})
    pool = _pool(fleet)
    assert pool.reach_status == "stale" and pool.reach == {} and "unifi" in pool.reach_aliases
    assert _portable(pool, body="reach: unifi\nssh unifi 'unifi-os restart'") == (False, "host_reach", None)
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
    body = "reach: ace-ai\nfix the GPU fan curve: ssh ace-ai"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ci-box", "ace-media")
    assert _portable(pool, body=body) == (True, None, "policy")
    # Two targets: only the host that reaches both.
    assert kwp.reach_allowed_hosts(body=body + "\nreach: unifi", skills=(), pool=pool) == ("ace-media",)
    # Nobody reaches mac-studio-m3u: local.
    assert _portable(pool, body="reach: mac-studio-m3u\nssh mac-studio-m3u 'uptime'") == (False, "host_reach", None)
    # A skill-bound card with no named alias needs a host reaching every alias
    # SOME pool host reaches (t_641d1ab8: ace-ai + unifi here; mac-studio-m3u
    # and ace-media, reached by nobody, cannot hold it forever).
    assert kwp.reach_allowed_hosts(body=None, skills=("ssh-machines",), pool=pool) == ("ace-media",)
    assert _portable(pool, skills=("ssh-machines",)) == (True, None, "policy")
    # A remote pin to a non-reaching host is refused (waits), host:any rides the rule.
    assert _portable(pool, body="host: ace-ai\nreach: ace-ai", ) == (False, "host_reach", None)
    assert _portable(pool, body="host: ci-box\nreach: ace-ai") == (True, None, "pin")
    assert _portable(pool, body="host:any\nreach: ace-ai") == (True, None, "any")


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
    # Nobody reaches unifi: a card that DECLARES it stays local instead of spilling on policy.
    assert _portable(pool, body="reach: unifi\nssh unifi 'restart'") == (False, "host_reach", None)
    # ``host: unifi`` is no pin a worker host can take: held local, named (t_cb2b888d).
    assert _portable(pool, body="host: unifi\nssh unifi 'restart'") == (False, "pin_unknown_host", None)


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
def test_ssh_shaped_mention_is_reported_not_pinned_and_a_reach_line_holds(tmp_path, body, target):
    """t_efd1497a (Apollo 2026-11-19, t_a0f9580e): an ssh-shaped mention in prose is read (host_bound_targets)
    and reported (host_reach_prose) but no longer binds host_reach. The same body with ``reach: <target>`` keeps
    the property t_1e9ea7e9 bought: host-bound, and with no proven route it never spills."""
    pool = _pool(tmp_path / "fleet")
    vocab = (*pool.reach_aliases, *pool.hosts)
    bound, targets = kwp.host_bound_targets(body, (), vocab, pin_hosts=pool.hosts)
    assert bound and target in targets
    assert target in kwp.host_reach_prose(body, vocab, pin_hosts=pool.hosts)
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) is None
    assert _portable(pool, body=body)[1] != "host_reach"
    declared = f"reach: {target}\n{body}"
    assert kwp.host_reach_prose(declared, vocab, pin_hosts=pool.hosts) == ()
    assert kwp.reach_allowed_hosts(body=declared, skills=(), pool=pool) == ()
    ok, rule, _ = _portable(pool, body=declared)
    assert (ok, rule) == (False, "host_reach")


# -- t_f1b135a8: only the ssh TARGET position binds; word aliases need ~/.ssh/config --
SHORT = (*ALIASES, "ci", "ai", "media")
SSH_CFG = frozenset({"ci", "ace-ai", "ace-media", "unifi"})   # ai/media: no Host entry


@pytest.mark.parametrize("body", [
    "ssh in and check the ai lane",
    "scp media:/srv/x .",                       # media is no ssh Host
    "rsync the media library to the NAS",
    "ssh into the box and look at ci logs",
    "run ci:lint then report",                  # verbless word:path
    "ssh -o BatchMode=yes in, then grep the ai log",
    "mosh to it and tail the media log",
])
def test_word_alias_in_prose_does_not_bind(body):
    """RED ON BASE: any token after an ssh/rsync verb, and any ``<word>:``
    bound the card, so a short alias in prose made it non-portable."""
    assert kwp.host_bound_targets(body, (), SHORT, ssh_hosts=SSH_CFG) == (False, ())


@pytest.mark.parametrize("body,target", [
    ("ssh ci sudo systemctl restart foo", "ci"),
    ("scp ci:/path .", "ci"),
    ("scp report.txt kanbanw@ci:/tmp/", "ci"),
    ("rsync -az ci:/var/log/ ./logs/", "ci"),
    ("ssh -p 22 -o BatchMode=yes kanbanw@ci 'uptime'", "ci"),
    ("ssh -i ~/.ssh/k ci uptime", "ci"),
    ("ssh ace-ai 'x' && ssh ci 'y'", "ci"),
    ("sftp ci", "ci"),
    ("docker -H ssh://ace-ai ps", "ace-ai"),
    ("see ace-media:/srv/drop for the file", "ace-media"),   # separator alias, verbless
])
def test_target_position_binds(body, target):
    bound, targets = kwp.host_bound_targets(body, (), SHORT, ssh_hosts=SSH_CFG)
    assert bound and target in targets


def test_word_alias_needs_an_ssh_config_host():
    """Target position alone is not enough for a word alias: ``ssh ai uptime``
    names a host only if ``ai`` is a Host in ~/.ssh/config. Unknown config
    (None) fails closed: the word alias binds."""
    assert kwp.host_bound_targets("ssh ai uptime", (), SHORT, ssh_hosts=SSH_CFG) == (False, ())
    assert kwp.host_bound_targets("ssh ai uptime", (), SHORT, ssh_hosts=SSH_CFG | {"ai"}) == (True, ("ai",))
    assert kwp.host_bound_targets("ssh ai uptime", (), SHORT, ssh_hosts=None) == (True, ("ai",))
    # Separator-bearing aliases never need the config.
    assert kwp.host_bound_targets("ssh ace-media uptime", (), SHORT, ssh_hosts=frozenset()) \
        == (True, ("ace-media",))


def test_ssh_config_hosts_reads_literal_hosts_and_includes(tmp_path):
    inc = tmp_path / "conf.d"
    inc.mkdir()
    (inc / "fleet").write_text("Host media ace-media\n  HostName 10.0.0.2\n")
    cfg = tmp_path / "config"
    cfg.write_text("Include conf.d/*\nHost ci *.lan !bad\n  User x\nhost   ai\n")
    assert kwp.ssh_config_hosts(cfg) == frozenset({"ci", "ai", "media", "ace-media"})
    assert kwp.ssh_config_hosts(tmp_path / "missing") is None


@pytest.mark.parametrize("body", [
    "sftp -p ci-box",                              # sftp -p is preserve, a FLAG
    "sftp -rp ci-box",
    "sftp -s /usr/libexec/sftp-server ci-box",     # sftp -s / -X take a value
    "sftp -X buffer=65536 ci-box",
    "mosh --port 60001 ci-box",                    # mosh long option, separate value
    "mosh -p 60001 ci-box",
    "ssh -p 2222 ci-box",                          # regression guard: ssh -p takes a value
])
def test_per_verb_option_arity_binds_the_target(body):
    """Prism 44c13730263b, RED ON ba92ce6c: one ssh(1) arity table was shared
    by sftp and mosh, so the host was swallowed as an option value."""
    assert kwp.host_bound_targets(body, (), SHORT, ssh_hosts=SSH_CFG) == (True, ("ci-box",))


def test_nested_relative_include_resolves_against_the_ssh_dir(tmp_path):
    """Prism aa5b13cf2e8a, RED ON ba92ce6c: a relative Include in a nested
    file resolves against ~/.ssh (ssh_config(5)), not the including file's
    directory. The single-level test could not see it: both are ~/.ssh."""
    ssh = tmp_path / "ssh"
    (ssh / "conf.d").mkdir(parents=True)
    (ssh / "config").write_text("Include conf.d/fleet\n")
    (ssh / "conf.d" / "fleet").write_text("Include targets\n")
    (ssh / "targets").write_text("Host ci\n")
    (ssh / "conf.d" / "targets").write_text("Host decoy\n")   # the wrong base
    hosts = kwp.ssh_config_hosts(ssh / "config")
    assert "ci" in hosts and "decoy" not in hosts


def test_ssh_config_keyword_equals_value_syntax(tmp_path):
    """ssh_config(5) allows ``Keyword=value`` with optional whitespace."""
    (tmp_path / "x").write_text("Host = media\n")
    (tmp_path / "y").write_text("Host=unifi\n")
    cfg = tmp_path / "config"
    cfg.write_text("Include=x\nInclude = y\nHost=ci\nHost =ai ace-ai\n")
    assert kwp.ssh_config_hosts(cfg) == frozenset({"ci", "ai", "ace-ai", "media", "unifi"})


def test_pool_ssh_hosts_threads_into_reach_allowed_hosts(tmp_path):
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": {"at": NOW, "reach": {"ci": 0}}})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), reach=reach,
                          reach_aliases=aliases, reach_status=status, ssh_hosts=frozenset({"ci"}))
    # The ssh-shape reader keeps its word-alias rule (reported, t_efd1497a); only a reach: line binds.
    assert kwp.host_reach_prose("ssh ci sudo reboot", aliases, ssh_hosts=pool.ssh_hosts) == ("ci",)
    assert kwp.host_reach_prose("ssh in and run the ci suite", aliases, ssh_hosts=pool.ssh_hosts) == ()
    assert kwp.reach_allowed_hosts(body="ssh ci sudo reboot", skills=(), pool=pool) is None
    assert kwp.reach_allowed_hosts(body="reach: ci\nssh ci sudo reboot", skills=(), pool=pool) == ("ci-box",)


def test_known_ssh_hosts_adds_every_alias_a_matrix_row_ever_reached(tmp_path):
    """A word alias with no Host entry (DNS-resolved ``unifi``) is still a real
    destination if any matrix row, fresh OR stale, reached it (rc 0): a stale
    matrix must not unbind ``ssh unifi ...`` and let it spill (fail closed)."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": {"at": NOW, "reach": {"media": 255}},
                    "ace-ai": {"at": NOW - 40 * 3600, "reach": {"unifi": 0}}})
    cfg = tmp_path / "config"
    cfg.write_text("Host ci\n")
    known = kwp.known_ssh_hosts(fleet, ssh_config=cfg)
    assert known == frozenset({"ci", "unifi"})
    assert kwp.host_bound_targets("ssh unifi 'unifi-os restart'", (), SHORT, ssh_hosts=known) == (True, ("unifi",))
    assert kwp.host_bound_targets("ssh media uptime", (), SHORT, ssh_hosts=known) == (False, ())
    assert kwp.host_bound_targets("ssh in and restart the unifi app", (), SHORT, ssh_hosts=known) == (False, ())
    assert kwp.known_ssh_hosts(fleet, ssh_config=tmp_path / "missing") is None


def test_burst_card_pinned_to_ci_box_places_on_a_members_reach_nothing_matrix(kanban_home):
    """t_696a9a06 RED ON BASE, the live shape: every pool member reaches
    nothing, only the Studio row is green; a ci-box-pinned burst card spawns
    on ci-box while an ``ssh ace-ai`` card is still held."""
    fleet = kanban_home / "fleet"
    # ace-ai is a pool host that is DOWN (no row), so it is no candidate for its own
    # card: the self rule (t_8c580b20) cannot place it.
    _matrix(fleet, {"ci-box": _row(),
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
        bound = kb.create_task(conn, title="gpu", assignee="alpha", body="reach: ace-ai\nssh ace-ai 'nvidia-smi'")
        # ace-media is a host outside the pool: same property, no self rule in play.
        outside = kb.create_task(conn, title="gpu2", assignee="alpha", body="reach: ace-media\nssh ace-media 'nvidia-smi'")
        res, spawned = _tick(conn, plan)
    assert spawned == [(burst, "ci-box")]
    assert bound not in dict(spawned) and outside not in dict(spawned)
    # Only the Studio (no pool host) proves ace-ai / ace-media: held, never spilled.
    assert res.placement_waits[bound] == "host_reach"
    assert res.placement_waits[outside] == "host_reach"


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
    assert _portable(cfg, body="host: CI-Box\nreach: ace-ai") == (True, None, "pin")
    host = kwp.PoolHost(name="CI-Box", ssh_host="CI-Box", ssh_user=kwp.SSH_USER, slots=2,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=0)
    p = kwp.plan([host], {}, probe=lambda h: (1.0, 16), config=cfg)
    only = kwp.reach_allowed_hosts(body="reach: ace-ai", skills=(), pool=cfg)
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
                               body="read the Studio's OrbStack LNP state")
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
        bound = kb.create_task(conn, title="gpu", assignee="alpha", body="reach: ace-ai\nssh ace-ai nvidia-smi")
        res, spawned = _tick(conn, plan)
    assert spawned == [(bound, "ace-media")]


# -- t_efd1497a: the create/edit notice that replaced the prose bind ---------------------------
# t_a0f9580e as created 2026-11-19 22:59 (design only) quoted the doctor-row command; it was held host_reach.
DELL_DESIGN = ("This card is design and code only: it needs no access to that machine. A doctor row proves "
               "`ssh root@dell-rugged true` from each granted host.")


def _notes(conn, tid, prefix):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))
            if r[0].startswith(prefix)]


def test_create_names_the_ssh_alias_and_the_fix_and_does_not_pin(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_create_placement as kcp
    from hermes_cli import kanban_studio_pin as ksp
    fleet = kanban_home / "fleet"
    _matrix(fleet, {"ci-box": _row(), "ace-ai": _row()})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    aliases = (*aliases, "dell-rugged")
    cfg = kwp.PoolConfig(hosts=("ci-box", "ace-ai"), profiles=("alpha",), reach=reach,
                         reach_aliases=aliases, reach_status=status)
    monkeypatch.setattr(kcp, "_pool_config", lambda: cfg)
    assert kwp.reach_allowed_hosts(body=DELL_DESIGN, skills=(), pool=cfg) is None   # RED ON BASE: () = held
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="dell-admin design", assignee="alpha", body=DELL_DESIGN)
        notes = _notes(conn, tid, ksp.HR_PROSE_PREFIX)
        assert len(notes) == 1 and "`dell-rugged`" in notes[0] and "`reach: dell-rugged`" in notes[0]
        declared = kb.create_task(conn, title="dell-admin run", assignee="alpha",
                                  body="reach: dell-rugged\n" + DELL_DESIGN)
        assert _notes(conn, declared, ksp.HR_PROSE_PREFIX) == []
    assert ksp.HR_PROSE_PREFIX in capsys.readouterr().err
    assert kwp.reach_allowed_hosts(body="reach: dell-rugged\n" + DELL_DESIGN, skills=(), pool=cfg) == ()


def test_declared_reach_reads_the_line_only():
    vocab = ("dell-rugged", "ci-box", "ace-ai")
    assert kwp.declared_reach("reach: dell-rugged, ci-box -- admin shell", vocab) == ("dell-rugged", "ci-box")
    assert kwp.declared_reach("  REACH: `ace-ai`", vocab) == ("ace-ai",)
    assert kwp.declared_reach("we reach: dell-rugged later", vocab) == ()     # not line-anchored
    assert kwp.declared_reach("reach: nowhere", vocab) == ()                  # not in the vocabulary


def test_skill_bound_card_keeps_its_ssh_named_target(tmp_path):
    """Prism ac47bd4b21d9: ssh-machines + `ssh mac-studio-m3u uptime` must still need mac-studio-m3u (nobody
    reaches it: held), not fall back to the aliases some host reaches and spill."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row(ace_ai=0), "ace-media": _row(ace_ai=0, unifi=0)})
    pool = _pool(fleet)
    body = "ssh mac-studio-m3u uptime"
    assert kwp.reach_allowed_hosts(body=body, skills=("ssh-machines",), pool=pool) == ()
    assert _portable(pool, body=body, skills=("ssh-machines",)) == (False, "host_reach", None)
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) is None     # prose alone: not bound


# -- t_efd1497a round 2 (Apollo 2026-11-20): prose is a SOFT narrowing, never a hold -------------------------
def _soft_pool(tmp_path, rows, hosts):
    fleet = tmp_path / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.REACH_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        h: {"at": NOW, "reach": r} for h, r in rows.items()}}))
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    return kwp.PoolConfig(hosts=hosts, profiles=("alpha",), reach=reach, reach_aliases=aliases,
                          reach_status=status)


class _Plan:
    def __init__(self, config):
        self.config = config
        self.prose_advisory = False


def _only(pool, body, skills=()):
    row = {"body": body, "skills": json.dumps(list(skills))}
    return kbd._portable_only(None, row, "alpha", _Plan(pool), None, None, None)[0]


SOFT_HOSTS = ("ci-box", "ace-ai", "ace-media", "mbp-2026")
NAS_ROWS = {"ci-box": {"nas-fleet": 255}, "ace-ai": {"nas-fleet": 255}, "ace-media": {"nas-fleet": 255},
            "mbp-2026": {"nas-fleet": 0}}


def test_prose_alias_narrows_to_the_hosts_that_reach_it(tmp_path):
    """t_a0d8ac3e / t_592555dd shape: only mbp-2026 reaches nas-fleet. The card lands only there, and prose
    never makes portable() hold it."""
    pool = _soft_pool(tmp_path, NAS_ROWS, SOFT_HOSTS)
    body = "scp out nas-fleet:/srv/drop/"
    assert _only(pool, body) == ("mbp-2026",)
    assert _portable(pool, body=body)[1] != "host_reach"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) is None
    # a host: line decides placement on its own; the preference does not apply
    assert _only(pool, "host: ci-box\n" + body) is None


def test_prose_alias_no_host_reaches_places_anywhere(tmp_path):
    pool = _soft_pool(tmp_path, {"ci-box": {"dell-rugged": 255}, "ace-ai": {"dell-rugged": 255}},
                      ("ci-box", "ace-ai"))
    body = "ssh dell-rugged true"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) is None
    assert kwp.prose_reach_preference(body=body, skills=(), pool=pool) is None
    assert _only(pool, body) is None
    assert _portable(pool, body=body)[0] is True


def test_prose_preference_never_empties_a_narrower_set(tmp_path, monkeypatch):
    """Soft: if another rule already narrowed to hosts that do not reach the alias, the preference is dropped."""
    pool = _soft_pool(tmp_path, NAS_ROWS, SOFT_HOSTS)
    monkeypatch.setattr(kwp, "needs_allowed_hosts", lambda *a, **k: ("ci-box",))
    assert _only(pool, "scp out nas-fleet:/srv/drop/") == ("ci-box",)


def test_skill_path_ignores_the_reach_line_note(tmp_path):
    """Prism ac47bd4b21d9 repro: the reach: line's own note is not ssh prose."""
    fleet = tmp_path / "fleet"
    _matrix(fleet, {"ci-box": _row(ace_ai=0), "ace-media": _row(ace_ai=0, unifi=0)})
    pool = _pool(fleet)
    body = "reach: ace-ai -- example: ssh mac-studio-m3u uptime"
    assert kwp.reach_allowed_hosts(body=body, skills=("ssh-machines",), pool=pool) == ("ci-box", "ace-media")
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ci-box", "ace-media")


@pytest.mark.parametrize("body", ["Use ace-media:~/drop/ as the destination.", "pdsh -Hace-media uptime"])
def test_notice_fires_for_every_shape_the_reader_binds(kanban_home, monkeypatch, body):
    """Prism 703b3483074b: the prefilter must not skip `alias:~/path` or an attached `-Halias`."""
    from hermes_cli import kanban_create_placement as kcp
    from hermes_cli import kanban_studio_pin as ksp
    fleet = kanban_home / "fleet"
    _matrix(fleet, {"ci-box": _row(ace_media=0)})
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    cfg = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), reach=reach, reach_aliases=aliases,
                         reach_status=status)
    monkeypatch.setattr(kcp, "_pool_config", lambda: cfg)
    assert ksp.host_reach_prose(body) == ("ace-media",)


# -- t_8c580b20: a host reaches itself, as the worker account -------------------

W = "kanbanw"
# What `ssh -G <alias>` resolves to for the worker on each host when the alias sets no User (the worker itself).
SELF_LOGINS = {"ci-box": {"ci-box": W, "ci.ace": W}, "ace-ai": {"ace-ai": W},
               "ace-media": {"ace-media": W, "ace-media-ubuntu": W}}


def _live_shape(fleet: Path, extra=None, logins=SELF_LOGINS) -> kwp.PoolConfig:
    """The live matrix shape (2026-11-19): every pool row is rc 255 to its own
    alias, and the second names (ci.ace, ace-media-ubuntu) are vocabulary only.
    ``logins`` is each row's ``{alias: ssh -G user}`` (None: the row has none)."""
    rows = {}
    pool_hosts = ("ci-box", "ace-ai", "ace-media")
    for h in pool_hosts:
        rows[h] = {"at": NOW, "reach": {a: (255 if a == h else 0) for a in pool_hosts}}
        if logins is not None and h in logins:
            rows[h]["logins"] = logins[h]
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.REACH_FILE).write_text(json.dumps({
        "schema": 1, "hosts": rows, "aliases": [*pool_hosts, "ci.ace", "ace-media-ubuntu", *(extra or ())]}))
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    return kwp.PoolConfig(hosts=pool_hosts, profiles=("alpha",), reach=reach, reach_aliases=aliases,
                          reach_status=status, reach_logins=kwp.read_reach_logins(fleet),
                          pin_aliases={"ci-box": "ci-box", "ci.ace": "ci-box", "ci": "ci-box",
                                       "ace-ai": "ace-ai", "ace-media": "ace-media",
                                       "ace-media-ubuntu": "ace-media"})


def test_a_card_reading_ci_box_is_placed_on_ci_box(tmp_path):
    """t_b58d9b08's target, ci-box by its second name. No row probes ci.ace, so
    before t_8c580b20 no pool host reached it and the card needed a Studio
    override (host_reach). ci-box reaches itself AS THE WORKER ACCOUNT: the alias
    resolves to kanbanw there, or the line names kanbanw.
    Mutation: drop the self rule in reaching_hosts -> () and host_reach (RED)."""
    pool = _live_shape(tmp_path / "fleet")
    body = "reach: ci.ace\n2. Check the preconditions on ci-box (ssh ci.ace, read-only): free space.\n"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ci-box",)
    assert _portable(pool, body=body) == (True, None, "policy")
    assert kwp.reach_allowed_hosts(body="reach: ci.ace\nssh kanbanw@ci.ace df", skills=(), pool=pool) == ("ci-box",)
    # The canonical id works the same way, and a pin to it is honoured.
    assert kwp.reach_allowed_hosts(body="reach: ci-box", skills=(), pool=pool) == ("ci-box", "ace-ai", "ace-media")
    assert _portable(pool, body="host: ci-box\nreach: ci-box") == (True, None, "pin")


@pytest.mark.parametrize("body", [
    "2. Check the preconditions on ci-box (ssh ace@ci.ace, read-only): free space.\n",  # t_b58d9b08 verbatim
    "ssh -l ace ci.ace df",
    "ssh -lace ci.ace df",
    "ssh -oUser=ace ci.ace df",       # Prism 5ffa30ab3a7a
    "ssh -o User=ace ci.ace df",
    "ssh -o 'User ace' ci.ace df",
    "scp ace@ci.ace:/var/log/syslog .",
    "ssh ci.ace df\nssh ace@ci.ace cat /etc/hostname",
])
def test_self_reach_only_as_the_worker_account(tmp_path, body):
    """Send-back 2 (t_8c580b20): `ssh ace@ci.ace` is user ace on ci-box. A worker
    placed on ci-box is kanbanw, so ci-box does not reach that target by being it.
    A peer's rc 0 is still the proof for a named foreign login.
    Mutation: drop the login check in reaching_hosts -> ("ci-box",) (RED)."""
    pool = _live_shape(tmp_path / "fleet")
    body = "reach: ci.ace\n" + body
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ()
    assert _portable(pool, body=body) == (False, "host_reach", None)
    # A peer whose fresh row proves ci.ace still takes it: the probe, not the self rule.
    rows = dict(pool.reach)
    rows["ace-ai"] = {**rows["ace-ai"], "ci.ace": 0}
    routed = kwp.PoolConfig(**{**pool.__dict__, "reach": rows})
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=routed) == ("ace-ai",)


@pytest.mark.parametrize("sibling", [
    "ssh ace@ci.ace cat /etc/hostname",
    "ssh -l ace ci.ace cat /etc/hostname",
    "ssh -o User=ace ci.ace cat /etc/hostname",
    "scp ace@ci.ace:/var/log/syslog .",
    "ssh ci.ace cat /etc/hostname",      # ci.ace's ssh config User is fleetw on ci-box
])
def test_a_foreign_login_on_a_sibling_alias_blocks_self_reach(tmp_path, sibling):
    """Round-4 reader repro (Apollo 2026-11-20 02:3x): `reach: ci-box` logs in as the worker (alias A), but the
    body also runs a step on ci.ace (alias B of the same host) as another account. Self-reach for a host holds
    only if EVERY alias of it the body names resolves to the worker; one foreign login on any alias and the
    host needs a real rc 0 like any peer.
    Mutation: judge only the declared targets' logins in reaching_hosts -> ci-box admitted (RED)."""
    logins = {**SELF_LOGINS, "ci-box": {"ci-box": W, "ci.ace": "fleetw"}}
    pool = _live_shape(tmp_path / "fleet", logins=logins)
    body = "reach: ci-box\nssh ci-box df\n" + sibling
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ace-ai", "ace-media")
    assert _portable(pool, body="host: ci-box\n" + body) == (False, "host_reach", None)
    # With every alias of ci-box resolving to the worker, ci-box self-reaches again.
    assert kwp.reach_allowed_hosts(body="reach: ci-box\nssh ci-box df\nssh kanbanw@ci.ace df", skills=(),
                                   pool=pool) == ("ci-box", "ace-ai", "ace-media")
    # A real probe still counts: ci-box's own row proving ci.ace (rc 0) admits it as written.
    rows = dict(pool.reach)
    rows["ci-box"] = {**rows["ci-box"], "ci.ace": 0}
    routed = kwp.PoolConfig(**{**pool.__dict__, "reach": rows})
    assert "ci-box" in kwp.reach_allowed_hosts(body=body, skills=(), pool=routed)
    # The same rule through the soft prose preference (no reach: line).
    prose = "ssh ci-box df\n" + sibling
    assert "ci-box" not in (kwp.prose_reach_preference(body=prose, skills=(), pool=pool) or ())


def test_self_reach_follows_the_alias_ssh_config_login(tmp_path):
    """Operator ruling (Apollo 2026-11-20 01:3x): a bare `ssh ci.ace` whose ssh config
    on ci-box sets User fleetw is fleetw, not the worker: ci-box does not self-reach
    it; a peer that reaches it as written takes the card. No recorded config login
    (an older matrix row) proves nothing. An explicit kanbanw@ login is the worker.
    Mutation: ignore config_logins in reaching_hosts -> ("ci-box",) (RED)."""
    fleetw = {**SELF_LOGINS, "ci-box": {"ci-box": "fleetw", "ci.ace": "fleetw"}}
    pool = _live_shape(tmp_path / "a", logins=fleetw)
    body = "reach: ci.ace\nssh ci.ace df"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ()
    assert _portable(pool, body=body) == (False, "host_reach", None)
    rows = dict(pool.reach)
    rows["ace-ai"] = {**rows["ace-ai"], "ci.ace": 0}
    routed = kwp.PoolConfig(**{**pool.__dict__, "reach": rows})
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=routed) == ("ace-ai",)
    assert kwp.reach_allowed_hosts(body="reach: ci.ace\nssh kanbanw@ci.ace df", skills=(),
                                   pool=pool) == ("ci-box",)
    # A row with no logins table: fail closed.
    bare = _live_shape(tmp_path / "b", logins=None)
    assert bare.reach_logins == {}
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=bare) == ()
    assert kwp.reaching_hosts({"ci-box": {}}, ("ci.ace",), (), {"ci.ace": "ci-box"},
                              config_logins={"ci-box": {"ci.ace": "fleetw"}}) == ()
    assert kwp.reaching_hosts({"ci-box": {}}, ("ci.ace",), (), {"ci.ace": "ci-box"},
                              config_logins={"CI-Box": {"CI.ACE": W}}) == ("ci-box",)


def test_self_reach_uses_each_hosts_own_worker_login(tmp_path):
    """Option A (t_5e87455b): a host cut over to its owner account (hosts.ci-box.ssh_user = ace) self-reaches
    as ace, not as kanbanw; a host still on the default keeps kanbanw.
    Mutation: compare against the module SSH_USER instead of PoolHost.ssh_user -> () for ace@ (RED)."""
    pool = _live_shape(tmp_path / "fleet")
    hosts = tuple(kwp.PoolHost(name=h, ssh_host=h, ssh_user=("ace" if h == "ci-box" else W), slots=None,
                               capacity_pct=1.0, absence="optional", profiles=("alpha",), state="active",
                               enabled=True, priority=i) for i, h in enumerate(pool.hosts))
    cut = kwp.PoolConfig(**{**pool.__dict__, "pool_hosts": hosts})
    assert kwp.reach_allowed_hosts(body="reach: ci.ace\nssh ace@ci.ace df", skills=(), pool=cut) == ("ci-box",)
    assert kwp.reach_allowed_hosts(body="reach: ci.ace\nssh kanbanw@ci.ace df", skills=(), pool=cut) == ()
    assert "ci-box" in (kwp.prose_reach_preference(body="ssh ace@ci.ace df", skills=(), pool=cut) or ())
    # ace-ai stays on kanbanw: unchanged.
    assert kwp.reach_allowed_hosts(body="reach: ace-ai\nssh kanbanw@ace-ai df", skills=(),
                                   pool=cut) == ("ci-box", "ace-ai", "ace-media")


def test_prose_preference_applies_the_self_and_login_rules(tmp_path):
    """Row-17 reader (Apollo 2026-11-20): prose_reach_preference passed only 3 arguments to
    reaching_hosts, so prose `ssh ci.ace` had no self-reach (canon) and no login rule.
    Mutation: drop canon/logins/config_logins from that call -> None (RED)."""
    pool = _live_shape(tmp_path / "a")
    assert kwp.prose_reach_preference(body="ssh ci.ace df", skills=(), pool=pool) == ("ci-box",)
    # A named foreign login: ci-box is no self match; nobody else reaches ci.ace -> no preference.
    assert kwp.prose_reach_preference(body="ssh ace@ci.ace df", skills=(), pool=pool) is None
    fleetw = {**SELF_LOGINS, "ci-box": {"ci-box": "fleetw", "ci.ace": "fleetw"}}
    pool = _live_shape(tmp_path / "b", logins=fleetw)
    rows = dict(pool.reach)
    rows["ace-ai"] = {**rows["ace-ai"], "ci.ace": 0}
    routed = kwp.PoolConfig(**{**pool.__dict__, "reach": rows})
    assert kwp.prose_reach_preference(body="ssh ci.ace df", skills=(), pool=routed) == ("ace-ai",)


def test_read_reach_logins_is_fail_closed(tmp_path):
    fleet = tmp_path / "fleet"
    assert kwp.read_reach_logins(fleet) == {}
    fleet.mkdir()
    (fleet / kwp.REACH_FILE).write_text("{not json")
    assert kwp.read_reach_logins(fleet) == {}
    (fleet / kwp.REACH_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "CI-Box": {"logins": {"CI.ACE": "fleetw", "x": 3, "": "u"}}, "ace-ai": {"logins": []}, "n": 1}}))
    assert kwp.read_reach_logins(fleet) == {"ci-box": {"ci.ace": "fleetw"}}


def test_ssh_target_logins_reads_user_at_and_dash_l():
    got = kwp.ssh_target_logins("ssh ace@ci.ace df\nssh -l root ace-ai uptime\nrsync -a bob@nas:/x .\n"
                                "ssh ci-box true\nmail kyz@example.com")
    assert got == {"ci.ace": {"ace"}, "ace-ai": {"root"}, "nas": {"bob"}, "example.com": {"kyz"}}
    assert kwp._ssh_target("-l root ace-ai uptime") == "ace-ai"
    assert kwp._ssh_target_login("-p 22 -lroot ace-ai") == ("ace-ai", "root")
    assert kwp._ssh_target_login("ace@ci.ace") == ("ci.ace", "ace")
    assert kwp._ssh_target_login("ci.ace df") == ("ci.ace", None)
    assert kwp._ssh_target_login("-oUser=ace ci.ace df") == ("ci.ace", "ace")
    assert kwp._ssh_target_login("-o user=ace ci.ace") == ("ci.ace", "ace")
    assert kwp._ssh_target_login("-o 'User ace' ci.ace") == ("ci.ace", "ace")
    assert kwp._ssh_target_login("-o BatchMode=yes ci.ace") == ("ci.ace", None)


def test_a_card_naming_a_second_alias_of_a_pool_host_is_placed_on_a_pool_host(tmp_path):
    """`reach: ace-media-ubuntu` names ace-media by its second registry name: ace-media
    reaches itself. Two targets on two hosts still need ONE host reaching both, and
    an alias no registry row claims never self-matches (fail closed)."""
    pool = _live_shape(tmp_path / "fleet", extra=("unifi",))
    body = "reach: ace-media-ubuntu\nssh ace-media-ubuntu 'df -h'"
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=pool) == ("ace-media",)
    assert _portable(pool, body=body) == (True, None, "policy")
    assert kwp.reach_allowed_hosts(body="reach: ace-media-ubuntu, ci.ace", skills=(), pool=pool) == ()
    assert _portable(pool, body="reach: unifi\nssh unifi 'uptime'") == (False, "host_reach", None)
    # A stale row proves nothing, its own host included.
    stale = kwp.PoolConfig(hosts=pool.hosts, profiles=pool.profiles, reach={}, reach_aliases=pool.reach_aliases,
                           reach_status="stale", pin_aliases=pool.pin_aliases, reach_logins=pool.reach_logins)
    assert kwp.reach_allowed_hosts(body=body, skills=(), pool=stale) == ()


def test_reaching_hosts_self_rule_is_canonical_and_case_folded():
    reach = {"ci-box": {"ace-ai": 0}, "ace-ai": {"ci-box": 0}}
    cfg = {"ci-box": {"ci.ace": W, "ci-box": W}, "ace-ai": {"ace-ai": W}}
    assert kwp.reaching_hosts(reach, ("ci.ace",), (), {"CI.ACE": "CI-Box"}, config_logins=cfg) == ("ci-box",)
    assert kwp.reaching_hosts(reach, ("ci.ace",), (), config_logins=cfg) == ()   # no alias map: no self match
    assert kwp.reaching_hosts(reach, ("ace-ai",), (), config_logins=cfg) == ("ci-box", "ace-ai")
    # Prism 1fb15e606226: a mixed-case target folds like the host id it names.
    assert kwp.reaching_hosts({"CI-Box": {"CI-Box": 255}}, ("CI-Box",), (), config_logins=cfg) == ("CI-Box",)
    assert kwp.reaching_hosts({"ci-box": {}}, ("ci-box",), (), logins={"CI-BOX": {"ace"}}, config_logins=cfg) == ()


# -- Prism 77c519869eff (deferred to t_8c580b20): the prose preference is soft in take() too ---------------------

def _soft_plan(kanban_home, profiles_mbp=("alpha",)):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.REACH_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ci-box": {"at": NOW, "reach": {"nas-fleet": 255}}, "mbp-2026": {"at": NOW, "reach": {"nas-fleet": 0}}}}))
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    cfg = kwp.PoolConfig(hosts=("ci-box", "mbp-2026"), profiles=("alpha",), reach=reach,
                         reach_aliases=aliases, reach_status=status)
    mbp = kwp.PoolHost(name="mbp-2026", ssh_host="mbp-2026", ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                       absence="required", profiles=profiles_mbp, state="active", enabled=True, priority=1)
    plan = kwp.plan([_fleet_host("ci-box", 0), mbp], {}, probe=lambda h: (1.0, 16), config=cfg)
    plan.band, plan.spill_reason = "paused", "load"
    return plan


def test_prose_preference_lands_on_the_preferred_host_when_it_can(kanban_home):
    plan = _soft_plan(kanban_home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="drop", assignee="alpha", body="scp out nas-fleet:/srv/drop/")
        res, spawned = _tick(conn, plan)
    assert spawned == [(tid, "mbp-2026")]


def test_prose_preference_falls_back_when_the_preferred_host_cannot_take(kanban_home):
    """mbp-2026 alone reaches nas-fleet but does not serve alpha: take(only=pref) said pool_full and the card
    waited. A preference falls back to the other eligible hosts.
    Mutation: pass only=_prefer to take() with no fallback -> placement_waits pool_full (RED)."""
    plan = _soft_plan(kanban_home, profiles_mbp=("beta",))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="drop", assignee="alpha", body="scp out nas-fleet:/srv/drop/")
        res, spawned = _tick(conn, plan)
    assert spawned == [(tid, "ci-box")], res.placement_waits


def test_take_prefer_is_tried_first_then_only(kanban_home):
    plan = _soft_plan(kanban_home, profiles_mbp=("beta",))
    assert plan.take("alpha", prefer=("mbp-2026",)).name == "ci-box"
    plan = _soft_plan(kanban_home)
    assert plan.take("alpha", prefer=("mbp-2026",)).name == "mbp-2026"
    # prefer never widens a hard set
    plan = _soft_plan(kanban_home)
    assert plan.take("alpha", only=("ci-box",), prefer=("mbp-2026",)).name == "ci-box"


def test_admit_outside_the_prose_preference_is_not_host_reach(kanban_home):
    """`kanban admit --host ci-box` on a card whose prose prefers mbp-2026: the preference is not eligibility."""
    plan = _soft_plan(kanban_home)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="drop", assignee="alpha", body="scp out nas-fleet:/srv/drop/")
        spawned = []

        def spawn(task, workspace, *, board=None, placement=None):
            spawned.append((task.id, placement.name if placement else None))
            return 4242

        res = kbd.admit_once(conn, tid, "ci-box", spawn_fn=spawn, spillover=plan, max_spawn=64)
    assert spawned == [(tid, "ci-box")], res.placement_waits
