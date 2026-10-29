"""A structurally-held card is placed when its body says where, and reported
ONCE when it cannot be (t_641d1ab8).

2026-10-19: t_b904be5a (p85, media-homelab-servarr) and t_7beef6f4 (p70,
+local-dns) sat ready 16 h / 13 h on ``not_portable:host_reach``. Their bodies
said "on ACE-AI", never ``ssh ace-ai``, so the unnamed host-bound fallback
asked for a host reaching EVERY probed alias, and haos/unifi are rc 255 from
every host. t_cb47a348 sat 2 h on ``studio_needs`` because its ORIGIN line
named an Obsidian note with "vault" in the title.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_placement_starvation as kps
from hermes_cli import kanban_worker_pool as kwp

NOW = 1_800_000_000.0
POOL = ("ci-box", "ace-ai", "ace-media")
# The live matrix shape of 2026-10-19 (fleet/kanban-reach-matrix.json).
_LINUX = {"ace-ai": 0, "ace-media": 0, "ci-box": 0, "fleet-nas": 0, "haos": 255,
          "mac-studio-m3u": 126, "studio-mint": 2, "unifi": 255}
LIVE = {"ace-ai": dict(_LINUX), "ace-media": dict(_LINUX), "ci-box": dict(_LINUX),
        "mac-studio-m3u": {"ace-ai": 0, "ace-media": 0, "ci-box": 0, "fleet-nas": 255,
                           "haos": 255, "mac-studio-m3u": 0, "studio-mint": 255, "unifi": 255}}
B904 = (
    "origin: discord Daemonarchy / #computet-swaps · session 20261016_213426_0583ff26\n"
    "MEASURED: ace-media-boot-heal.service on ACE-AI is FAILED since 21:30:36 on 10-18.\n"
    "Then: shims.py lint green, boot-heal re-run by hand.\n"
)


def _pool(fleet: Path, rows=None, *, hosts=POOL) -> kwp.PoolConfig:
    fleet.mkdir(parents=True, exist_ok=True)
    rows = LIVE if rows is None else rows
    (fleet / kwp.REACH_FILE).write_text(json.dumps(
        {"schema": 1, "hosts": {h: {"at": NOW, "reach": r} for h, r in rows.items()}}))
    reach, aliases, status = kwp.read_reach(fleet, now=NOW)
    return kwp.PoolConfig(hosts=hosts, profiles=("alpha",), reach=reach,
                          reach_aliases=aliases, reach_status=status)


def _portable(pool, body, skills):
    return kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="alpha", body=body, skills=skills, pool=pool)


# -- (a) an unnamed host-bound card is placed --------------------------------

def test_unnamed_servarr_card_with_the_live_matrix_places_on_ace_ai(tmp_path):
    """RED ON BASE: () forever (haos/unifi at 255 from every host)."""
    pool = _pool(tmp_path / "fleet")
    skills = ("media-homelab-servarr",)
    assert kwp.body_host_targets(B904, pool) == ("ace-ai",)
    allowed = kwp.reach_allowed_hosts(body=B904, skills=skills, pool=pool)
    assert "ace-ai" in allowed and set(POOL) <= set(allowed)
    assert _portable(pool, B904, skills) == (True, None, "policy")
    # The real card also carried `host:ace-ai`: a pin it can now honour.
    assert _portable(pool, "host:ace-ai\n" + B904, skills) == (True, None, "pin")
    # And the second card (two host-bound skills, no host word at all) places
    # on the hosts that reach everything SOME pool host reaches.
    assert _portable(pool, "rollback restores the Caddy snapshot", (
        "media-homelab-servarr", "local-dns")) == (True, None, "policy")


def test_body_host_targets_ignores_the_header_and_dashed_unit_names(tmp_path):
    pool = _pool(tmp_path / "fleet")
    assert kwp.body_host_targets("origin: ace-ai session\nfix the doc", pool) == ()
    assert kwp.body_host_targets("restart ace-media-boot-heal.service", pool) == ()
    assert kwp.body_host_targets("run it on ACE-AI, then ci-box.", pool) == ("ace-ai", "ci-box")


def test_unreached_alias_is_excluded_only_from_the_fallback(tmp_path):
    """fleet-nas is reached by the pool hosts (not by the Studio): kept; haos /
    unifi reached by no pool host: dropped."""
    pool = _pool(tmp_path / "fleet")
    got = kwp.reached_aliases(pool.reach, pool.reach_aliases, pool.hosts)
    assert "fleet-nas" in got and "haos" not in got and "unifi" not in got
    assert "mac-studio-m3u" not in got  # rc 126 from every pool host


# -- (b) a card whose only target nobody reaches still waits -------------------

@pytest.mark.parametrize("body", ["ssh haos 'ha core info'", "ssh unifi 'info'"])
def test_card_naming_only_an_unreachable_alias_still_waits(tmp_path, body):
    pool = _pool(tmp_path / "fleet")
    pool = kwp.PoolConfig(**{**pool.__dict__, "ssh_hosts": frozenset({"haos", "unifi"})})
    assert kwp.reach_allowed_hosts(body=body, skills=("media-homelab-servarr",), pool=pool) == ()
    assert _portable(pool, body, ("media-homelab-servarr",)) == (False, "host_reach", None)


def test_unnamed_card_on_an_empty_or_unreached_matrix_still_waits(tmp_path):
    empty = _pool(tmp_path / "a", rows={})
    assert _portable(empty, "fix it", ("ssh-machines",)) == (False, "host_reach", None)
    dark = _pool(tmp_path / "b", rows={h: {a: 255 for a in _LINUX} for h in POOL})
    assert _portable(dark, "fix it", ("ssh-machines",)) == (False, "host_reach", None)
    # A named pool host nobody reaches waits too, never widens to the fallback.
    lone = _pool(tmp_path / "c", rows={h: {**_LINUX, "ace-ai": 255} for h in POOL})
    assert _portable(lone, B904, ("media-homelab-servarr",)) == (False, "host_reach", None)


# -- t_cb47a348: the origin line is never a need --------------------------------

def test_origin_line_vault_is_not_a_studio_need():
    body = ("origin: discord · Obsidian note 'Final Sweep, vault containers'\n"
            "Prism lineage: root t_4cb8fcc5 (vault)\n"
            "move the containers listed in docs/nas-final-sweep.md\n")
    assert kwp.card_needs(body) == set()
    assert kwp.card_needs(body + "update the vault note\n") == {"vault"}


# -- (c) the starvation comment fires once, not per tick -----------------------

@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("real spawn")))
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _fleet_host(name, prio):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=4,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=prio)


def _tick(conn, plan):
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, max_spawn=64,
                             spawn_paused="test", spawn_limit=0, spillover=plan,
                             reconcile_orphans=False)


def _comments(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = ?",
        (tid, kps.COMMENT_AUTHOR))]


def test_structural_hold_gets_one_comment_across_ticks(kanban_home, monkeypatch):
    """RED ON BASE: no comment at all. A pinned host-bound card no pool host
    reaches waits on host_reach; after 30 min it gets ONE comment naming the
    rule and the body fix, and later ticks add nothing."""
    pool = _pool(kanban_home / "fleet", rows={h: {**_LINUX, "ace-ai": 255} for h in POOL})
    plan = kwp.plan([_fleet_host(h, i) for i, h in enumerate(POOL)], {},
                    probe=lambda h: (1.0, 16), config=pool)
    plan.band, plan.spill_reason = "paused", "load"
    clock = [int(NOW)]
    monkeypatch.setattr(kbd.time, "time", lambda: clock[0])
    with kb.connect_closing() as conn:
        held = kb.create_task(conn, title="boot-heal", assignee="alpha", priority=85,
                              skills=["media-homelab-servarr"], body="host: ace-media\nssh ace-ai x")
        low = kb.create_task(conn, title="low", assignee="alpha", priority=10,
                             skills=["media-homelab-servarr"], body="host: ace-media\nssh ace-ai y")
        res = _tick(conn, plan)
        assert res.placement_waits[held] == "not_portable:host_reach"
        assert _comments(conn, held) == []          # fresh: not yet
        clock[0] += kps.STARVED_WAIT_SECONDS + 60
        _tick(conn, plan)
        (body,) = _comments(conn, held)
        assert "not_portable:host_reach" in body and "ssh <host>" in body
        clock[0] += 20 * 60                         # past the 900 s refresh: a new wait row
        _tick(conn, plan)
        assert len(_comments(conn, held)) == 1      # once, not per tick
        # The board event is the latch: the pure check finds nothing to post.
        assert kps.starved_holds(conn, {held: "not_portable:host_reach"}, clock[0]) == []
        assert _comments(conn, low) == []           # below the priority floor


def test_low_priority_hold_repeating_n_ticks_surfaces_once(kanban_home, monkeypatch):
    """t_250a7fc5: t_041ad167 (p2) sat 20 h on ``not_portable:linked`` with 13+
    identical placement_wait rows and nobody told; the priority floor hid it.
    After STARVED_REPEAT_EVENTS same-code rows it gets ONE comment and ONE
    #logs line, whatever its priority."""
    sent = []
    from hermes_cli import kanban_budget as _kbudget
    monkeypatch.setattr(_kbudget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(_kbudget, "_run_notify", lambda argv: sent.append(argv) or True)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p2", assignee="alpha", priority=2, body="x")
        wait = {"code": "not_portable:linked", "detail": "after-merge->t_84e763a3",
                "reason": "not_portable:linked(after-merge->t_84e763a3)", "route": "wait",
                "host": None, "since": int(NOW)}
        waits = {tid: wait["reason"]}
        for i in range(4):  # STARVED_REPEAT_EVENTS
            assert kps.notify_starved_holds(conn, waits, int(NOW) + i * 900) == []
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "placement_wait", wait)
        (hit,) = kps.notify_starved_holds(conn, waits, int(NOW) + 4 * 900)
        assert hit["code"] == "not_portable:linked"
        assert hit["events"] == 4
        (body,) = _comments(conn, tid)
        assert "not_portable:linked" in body and "derived-from" in body
        assert len(sent) == 1 and "--target" in sent[0] and tid in sent[0][-1]
        # Latched: later ticks post nothing more.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "placement_wait", wait)
        assert kps.notify_starved_holds(conn, waits, int(NOW) + 6 * 900) == []
        assert len(_comments(conn, tid)) == 1 and len(sent) == 1


def test_capacity_waits_are_never_reported(kanban_home):
    assert kps.fix_for("local_full") is None
    assert kps.fix_for("pool_full") is None
    assert kps.fix_for("pin_host_full") is None
    assert kps.fix_for("not_portable:host_reach") is not None
    assert kps.fix_for("requires_unsatisfied:docker+studio_needs") is not None
