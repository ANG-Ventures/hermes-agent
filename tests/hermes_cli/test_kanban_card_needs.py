"""card_needs(): a card whose body names Studio-only resources is placed on the
Studio or waits, never spilled to a pool host that cannot reach them (t_aea826a9).

Fixtures are excerpts of the two cards that spilled to ci-box on 2026-10-12 and
blocked within minutes: t_dec6d40f (vault + skills-shared) and t_6af0c092
(relay logs + deploy/claude-pool + ssh to sub-vps-5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp

DEC6D40F = (
    "# docs: record the 2026-10-12 fallback ruling (same-model skip removed)\n"
    "## Write (three surfaces, vault root `/Users/alexgierczyk/Obsidian/Ace Place`)\n"
    "3. Skill `claude-relay-pool-ops` -> `references/empty-reply-retry-ladder-doctrine.md` "
    "(the skill is under `~/.hermes/skills-shared/`; find the exact path with `skill_view`)\n"
    "- The vault files are COMMITTED (`git -C \"<vault>\" log -1` shows your commit).\n"
)
SIX_AF0C092 = (
    "# relay/apx: per-phase timing on every relayed turn\n"
    "Route `65a4c896`, alr, sub-vps-5, 2026-10-11 23:16:32 PT.\n"
    "**relay (`~/.hermes/deploy/claude-pool/claude_pool_relay.py`, repo ANG-Ventures/claude-pool):**\n"
    "- Live proof: deploy to sub-vps-5 only first, send ONE pinned small fable turn through alr.\n"
    "- Docs: `skills-shared/.../claude-relay-pool-ops/references/` gets `phase-timing.md`.\n"
)
PLAIN_CODE = (
    "Fix the off-by-one in hermes_cli/kanban_db_dispatch.py `_route_wait`; repo\n"
    "ANG-Ventures/hermes-agent. Test: tests/hermes_cli/test_kanban_worker_pool.py.\n"
)


# -- card_needs (pure) -------------------------------------------------------

def test_fixture_bodies_and_a_plain_code_card():
    assert kwp.card_needs(DEC6D40F) == {"vault", "skills_tree"}
    assert kwp.card_needs(SIX_AF0C092) == {"studio_relay", "sub_ssh"}
    assert kwp.card_needs(PLAIN_CODE) == set()
    assert kwp.card_needs(None) == set()


@pytest.mark.parametrize("body, need", [
    ("check ~/Library/LaunchAgents/ai.hermes.x.plist", "studio_launchd"),
    ("launchctl kickstart -k gui/501/ai.hermes.gateway", "studio_launchd"),
    ("curl 127.0.0.1:18812/health", "studio_relay"),
    ("the live gate is a pinned turn", "studio_relay"),
    ("ssh sub-vps-12 'journalctl -u apx'", "sub_ssh"),
    ("update the Vault note", "vault"),
])
def test_each_need_has_a_trigger(body, need):
    assert need in kwp.card_needs(body)


@pytest.mark.parametrize("body", [
    "the relay buffers the body",                 # bare 'relay' is prose
    "1Password vault-item read", "obsidian-vault repo",
    "port 18820 is not a relay port", "sub-vps-host naming",
    "skills-shared/coding/x (no home prefix)",
])
def test_prose_near_misses_need_nothing(body):
    assert kwp.card_needs(body) == set()


# -- caps reader + host filter ----------------------------------------------

NOW = 1_800_000_000.0


def _matrix(fleet: Path, rows: dict) -> None:
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.REACH_FILE).write_text(json.dumps({"schema": 1, "hosts": rows}))


def _host(name):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=0)


# t_50f2fa37 (run 33487, 2026-10-20): spilled to ci-box, which has no
# ~/.hermes, measured nothing and blocked (t_40abb42b).
FIFTY_F2FA37 = (
    "Continuation of t_f1184bc8 (step 6, 7-day acceptance). The detector is armed: "
    "~/.hermes/config/dpx-alias-key-leak.json page_after=1791169971.\n"
    "DO: run `~/.hermes/scripts/dpx-alias-key-leak-lint.py --hours 168 --dry-run --json`.\n"
)
REPO_SCRIPT = (
    "Fix scripts/x.py in ANG-Ventures/hermes-home and run `python3 scripts/x.py --check`.\n"
    "The live copy lands in ~/.hermes/scripts/x.py after deploy.\n"
)


@pytest.mark.parametrize("body", [
    FIFTY_F2FA37,
    "python3 ~/.hermes/scripts/lib/ups_pulled.py set --until tomorrow",
    "bash ~/.hermes/var/oneshot/main-red-reasons.sh 39064362674",
    "run /Users/someone/.hermes/state/pager/x.json through jq",
])
def test_running_a_home_tree_path_needs_the_studio_tree(body):
    assert "studio_tree" in kwp.card_needs(body)


def test_studio_tree_card_places_on_the_studio_and_a_repo_script_card_anywhere():
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),))

    def port(body):
        return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                            assignee="alpha", body=body, pool=pool)
    assert port(FIFTY_F2FA37) == (False, "studio_needs", None)
    assert kwp.needs_allowed_hosts(FIFTY_F2FA37, pool) == ()
    assert port(REPO_SCRIPT) == (True, None, "policy")
    assert kwp.card_needs(REPO_SCRIPT) == set()


def test_read_caps_fails_closed(tmp_path):
    fleet = tmp_path / "fleet"
    assert kwp.read_caps(fleet, now=NOW) == {}                       # absent
    _matrix(fleet, {
        "ci-box": {"at": NOW, "reach": {}, "caps": {"vault": True, "sub_ssh": False}},
        "ace-ai": {"at": NOW - 40 * 3600, "reach": {}, "caps": {"vault": True}},  # stale
        "ace-media": {"at": NOW, "reach": {}},                          # no caps
        "weird": {"at": NOW, "reach": {}, "caps": {"vault": "yes"}},    # not literal true
    })
    assert kwp.read_caps(fleet, now=NOW) == {"ci-box": {"vault"}, "weird": frozenset()}


def test_needs_allowed_hosts():
    pool = kwp.PoolConfig(pool_hosts=(_host("ci-box"), _host("ace-ai")),
                          caps={"ci-box": frozenset({"vault", "skills_tree"}),
                                "ace-ai": frozenset({"vault"})})
    assert kwp.needs_allowed_hosts(PLAIN_CODE, pool) is None
    assert kwp.needs_allowed_hosts(DEC6D40F, pool) == ("ci-box",)
    assert kwp.needs_allowed_hosts(SIX_AF0C092, pool) == ()
    assert kwp.needs_allowed_hosts(DEC6D40F, kwp.PoolConfig(pool_hosts=(_host("ci-box"),))) == ()


def test_portable_binds_studio_needs_and_a_pin_still_wins():
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),))

    def port(body):
        return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                            assignee="alpha", body=body, pool=pool)
    assert port(DEC6D40F) == (False, "studio_needs", None)
    assert port(SIX_AF0C092) == (False, "studio_needs", None)
    assert port(PLAIN_CODE) == (True, None, "policy")
    assert port("host:ci-box\n" + DEC6D40F) == (True, None, "pin")   # the operator's call
    assert kwp.studio_affinity(DEC6D40F, (), pool) == ("studio_needs", "needs:skills_tree,vault")


def test_explicit_requires_line_outranks_sniffed_prose_needs():
    """t_6d9e7d90 (Apollo 14:5x): a ``requires:`` line is the author's
    statement of need; prose (vault, skills-shared) no longer binds the card
    to the Studio. ``requires: none`` and a real capability word both count."""
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),))

    def port(body, layer_enforces=True):
        return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                            assignee="alpha", body=body, pool=pool,
                            body_signals=not layer_enforces)
    for decl in ("requires: none\n", "requires: fleet-ssh\n"):
        assert port(decl + DEC6D40F) == (True, None, "policy"), decl
        assert kwp.needs_allowed_hosts(decl + DEC6D40F, pool, honor_declared=True) is None
        # Prism cf1b3bd689e8: with the capability layer off nothing enforces
        # the declaration, so the prose needs still bind.
        assert port(decl + DEC6D40F, layer_enforces=False)[1] == "studio_needs", decl
        assert kwp.needs_allowed_hosts(decl + DEC6D40F, pool) == ()
    # the access word alone is not a capability declaration
    assert port("requires: control-plane-read\n" + DEC6D40F)[0] is False
    # prose that only MENTIONS the word keeps binding (not line-anchored)
    assert port("note: requires: none is not set here\n" + DEC6D40F)[1] == "studio_needs"


# -- dispatcher: ci-box free, Studio full ------------------------------------

@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch):
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


def _spill_plan(caps=None):
    p = kwp.plan([_host("ci-box")], {}, probe=lambda h: (1.0, 32))
    p.band, p.spill_reason = "paused", "load"
    if caps is not None:
        p.config = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",),
                                  pool_hosts=(_host("ci-box"),), caps=caps)
    return p


def _tick(conn, plan):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242
    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused="paused",
                            spawn_limit=0, spillover=plan, reconcile_orphans=False)
    return res, spawned


def test_dec6d40f_body_waits_with_a_named_reason_instead_of_spilling(kanban_home):
    """Red on base: main placed this body on ci-box (the 03:37 incident)."""
    with kb.connect_closing() as conn:
        docs = kb.create_task(conn, title="docs", assignee="alpha", body=DEC6D40F, priority=5)
        code = kb.create_task(conn, title="code", assignee="alpha", body=PLAIN_CODE, priority=1)
        res, spawned = _tick(conn, _spill_plan())
        assert spawned == [(code, "ci-box")]                  # plain code card: unchanged, spills
        assert res.placement_waits[docs] == "studio_affinity:studio_needs(skills_tree,vault)"
        assert res.placement_affinity[docs] == {"host": kwp.STUDIO_HOST, "rule": "studio_needs",
                                                "matched_by": "needs:skills_tree,vault"}
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (docs,)).fetchone()[0]
        assert status == "ready"
        waits = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='placement_wait'", (docs,))]
        assert waits and waits[-1]["reason"] == "studio_affinity:studio_needs(skills_tree,vault)"


def test_a_host_whose_caps_cover_the_needs_may_take_it(kanban_home):
    with kb.connect_closing() as conn:
        docs = kb.create_task(conn, title="docs", assignee="alpha", body=DEC6D40F)
        res, spawned = _tick(conn, _spill_plan(caps={"ci-box": frozenset({"vault", "skills_tree"})}))
        assert spawned == [(docs, "ci-box")]
        relay = kb.create_task(conn, title="relay", assignee="alpha", body=SIX_AF0C092)
        res, spawned = _tick(conn, _spill_plan(caps={"ci-box": frozenset({"vault", "skills_tree"})}))
        assert spawned == [] and res.placement_waits[relay].startswith("studio_affinity:studio_needs(")
