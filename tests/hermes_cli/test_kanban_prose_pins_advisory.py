"""A card's PROSE never pins it to the Mac Studio; only a declared need does (t_c82eded0).

Ace 2026-11-11 (Discord 1570214363137056788): kanban workers stay off the Studio unless truly necessary.
In the first 10 h after hermes-agent#216, 14 of 36 Studio runs were held only by words in the card body
(CARD_NEEDS / ``studio_needs``: a home-tree script path, the notes folder, launchctl, a sub-vps, the skills
tree, the relay) and 2 more by a skill NAMED in the body. Under ``kanban.placement.prose_pins_advisory``
(root config, read per tick, default on) those signals are recorded as an advisory and the card starts on a
pool host. A line-anchored ``requires:``, an attached skill and a ``host:`` pin still decide. False is the
kill switch: the hold before this card, byte for byte.
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
WAVE = "amazon-cart-ops-local"

# Excerpts of the measured shapes (card body): a home-tree script path, the notes folder, launchctl.
HOME_TREE = "DO: run `~/.hermes/scripts/dpx-alias-key-leak-lint.py --hours 168 --dry-run --json`.\n"
VAULT = "Write the result into the vault note under AI/Infrastructure.\n"
LAUNCHD = "Check the job with launchctl print gui/501/ai.hermes.gateway after the change.\n"
NAMED_SKILL = "Load `browser-access` and check the order page.\n"
SNIFF = "add these to the cart on :9222\n"


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


def _cfg(monkeypatch, *, prose=None, caps="enforce"):
    pl = {"capabilities": caps}
    if prose is not None:
        pl[khc.PROSE_KNOB] = prose
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {"placement": pl})


def _host(name, prio=0):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=prio)


def _plan():
    hosts = [_host("ace-ai", 0), _host("ace-media", 1)]
    cfg = kwp.PoolConfig(hosts=("ace-ai", "ace-media"), profiles=("alpha",), pool_hosts=tuple(hosts))
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def _tick(conn):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=0, spawn_paused="spilling",
                            spillover=_plan(), reconcile_orphans=False)
    return res, spawned


def _card(conn, body, skills=None):
    return kb.create_task(conn, title="card", assignee="alpha", body=body, skills=skills)


def _route(conn, tid):
    row = conn.execute("SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker "
                       "FROM tasks WHERE id=?", (tid,)).fetchone()
    plan = _plan()
    caps = kbd._capability_tick(plan.config)
    aff: dict = {}
    adv: dict = {}
    route = kbd._route_row(conn, row, plan, None, {}, {}, affinity_out=aff, caps=caps, advisory_out=adv)
    return route, aff, adv


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _advisory_comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))
            if r[0].startswith(khc.PROSE_ADVISORY_PREFIX)]


# -- the knob -----------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    (None, True), (True, True), ("on", True), ("typo", True),
    (False, False), ("off", False), ("false", False), ("no", False), (0, False),
])
def test_knob_defaults_on_and_only_an_explicit_off_disables_it(raw, want):
    cfg = {} if raw is None else {"placement": {khc.PROSE_KNOB: raw}}
    assert khc.read_prose_advisory(cfg) is want


# -- rule 1: a body match (CARD_NEEDS / studio_needs) never places on the Studio --

@pytest.mark.parametrize("body, need", [(HOME_TREE, "studio_tree"), (VAULT, "vault"),
                                        (LAUNCHD, "studio_launchd")])
def test_body_need_is_advice_not_a_hold(body, need):
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),))

    def port(prose):
        return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                            assignee="alpha", body=body, pool=pool, prose_advisory=prose)
    assert port(False) == (False, kwp.STUDIO_NEEDS_RULE, None)          # kill switch: the old hold
    assert port(True) == (True, None, "policy")
    assert kwp.needs_allowed_hosts(body, pool, prose_advisory=True) is None
    assert need in kwp.card_need_evidence(body)


def test_a_covering_pool_host_still_narrows_the_take():
    pool = kwp.PoolConfig(hosts=("ci-box", "ace-ai"), profiles=("alpha",),
                          pool_hosts=(_host("ci-box"), _host("ace-ai")),
                          caps={"ace-ai": frozenset({"vault"})})
    assert kwp.needs_allowed_hosts(VAULT, pool, prose_advisory=True) == ("ace-ai",)


def test_body_need_card_starts_on_a_pool_host_with_the_advisory_recorded(home, monkeypatch):
    _cfg(monkeypatch)    # no capability map: the legacy CARD_NEEDS path
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME_TREE)
        res, spawned = _tick(conn)
        assert [t for t, _ in spawned] == [tid] and spawned[0][1] in ("ace-ai", "ace-media")
        assert tid not in (res.placement_waits or {})
        placement = _events(conn, tid, "allocation")[-1]
        assert placement["advisory"] == [{"source": "body", "rule": "studio_needs", "need": "studio_tree",
                                          "evidence": placement["advisory"][0]["evidence"]}]
        assert "~/.hermes/scripts/" in placement["advisory"][0]["evidence"]
        got = _advisory_comments(conn, tid)
        assert len(got) == 1 and 'kind="capability"' in got[0] and "studio_tree" in got[0]


def test_kill_switch_restores_the_body_hold(home, monkeypatch):
    _cfg(monkeypatch, prose=False)
    with kb.connect_closing() as conn:
        tid = _card(conn, VAULT)
        res, spawned = _tick(conn)
        assert spawned == []
        assert res.placement_waits[tid] == "studio_affinity:studio_needs(vault)"
        assert _advisory_comments(conn, tid) == []


def test_advisory_comment_is_posted_once(home, monkeypatch):
    _cfg(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, LAUNCHD)
        _route_row, _aff, adv = _route(conn, tid)
        assert adv[tid][0]["need"] == "studio_launchd"
        assert khc.record_prose_advisories(conn, adv) == [tid]
        assert khc.record_prose_advisories(conn, adv) == []      # the earlier comment is the latch
        assert len(_advisory_comments(conn, tid)) == 1


# -- rule 1 (capability layer): a body sniff no pool host serves --------------

def test_body_sniffed_capability_is_advice(home, fleet, monkeypatch):
    _cfg(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, SNIFF)
        route, aff, adv = _route(conn, tid)
        assert route == ("portable", None)
        assert tid not in aff
        assert {"source": "body", "rule": "requires:browser-session", "capability": "browser-session",
                "evidence": ":9222"} in adv[tid]


def test_body_sniffed_capability_holds_with_the_kill_switch(home, fleet, monkeypatch):
    _cfg(monkeypatch, prose=False)
    with kb.connect_closing() as conn:
        tid = _card(conn, SNIFF)
        route, aff, adv = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert aff[tid]["matched_by"] == "body" and adv == {}


# -- rule 2: a need inferred from a skill NAMED in the body -------------------

def test_named_skill_is_advice(home, fleet, monkeypatch):
    _cfg(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, NAMED_SKILL)
        route, aff, adv = _route(conn, tid)
        assert route == ("portable", None)
        assert {a["source"] for a in adv[tid]} == {"named_skill"}
        assert {a["evidence"] for a in adv[tid]} == {"browser-access"}


def test_named_skill_holds_with_the_kill_switch(home, fleet, monkeypatch):
    _cfg(monkeypatch, prose=False)
    with kb.connect_closing() as conn:
        tid = _card(conn, NAMED_SKILL)
        route, aff, _adv = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert aff[tid]["matched_by"] == "named_skill"


# -- what still holds: a declared requires:, an attached skill ----------------

def test_declared_requires_line_still_holds(home, fleet, monkeypatch):
    _cfg(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, "requires: browser-session\n" + SNIFF)
        route, aff, _adv = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert aff[tid]["matched_by"] == "declared"


def test_attached_skill_still_holds(home, fleet, monkeypatch):
    _cfg(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, "restock the shelf", skills=[WAVE])
        route, aff, adv = _route(conn, tid)
        assert route == ("local", "studio_affinity:requires:browser-session")
        assert aff[tid]["matched_by"] == "skill" and tid not in adv


# -- rule 3: the hand-back a pool worker uses is the existing re-route ---------

def test_a_capability_block_from_a_pool_host_is_rerouted_to_the_studio():
    """The hand-back the advisory comment names: kanban_block(kind=capability) on a placed run is
    re-routed once to ``host: studio`` (kanban_placement_block), never left blocked silently."""
    from hermes_cli import kanban_placement_block as kpb
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),))
    verdict = kpb.classify(kind="capability", reason="needs the vault only the Studio has; re-run on the Studio",
                           placed_host="ci-box", body=VAULT, pool=pool, rerouted_before=False,
                           assignee="alpha", workspace_kind="scratch",
                           studio_reasons=frozenset({"obsidian-vault"}))   # no map = no Studio route (t_9392860c)
    assert verdict == ("reroute", kwp.PIN_STUDIO)
    assert 'kind="capability"' in khc.prose_advisory_comment(
        [{"source": "body", "rule": "studio_needs", "need": "vault", "evidence": "vault"}])
