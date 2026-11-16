"""Mac Studio is the LAST RESORT for kanban placement (t_c2fce87e).

Ace 2026-11-08 17:05: fleet work goes to every other machine first. The real
``dispatch_once`` spawn loop with a fixture pool plan and a fake spawn_fn:

1. a card with no Studio need goes to a pool host even when the Studio has a slot;
2. a Studio-need card (``host: studio``) still runs on the Studio;
3. when every pool host refuses, a portable card falls back to the Studio;
4. a card whose persisted workspace is its OWN Studio ``.worktrees/<id>``
   (t_5e8801d8: ``rule=workspace_kind``, then 30+ min in ``local_full``)
   ports to a pool host when that tree is clean and its branch is on origin,
   and stays local when it is dirty, off its branch (ahead of origin or with no
   origin ref: only under the hand list; derived mode pushes then ports it,
   t_1d2c5939), or the shared setting is off (``host_preference`` in fleet/allocation-policy.json: no key, kanban
   switch off, or env FLEET_LAST_RESORT_HOSTS empty).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import kanban_worktree_port as kwt

SLUG = "ANG-Ventures/hermes-agent"
HOST_PREF = {"order": ["ci-box", "nas-fleet", "ace-ai", "ace-media", "mac-studio"],
             "last_resort": ["mac-studio"], "consumers": {"kanban": "on", "ci": "on"}}


def _write_policy(fleet: Path, host_preference=HOST_PREF) -> None:
    """A schema-1 policy carrying (or not) the shared ``host_preference`` key."""
    doc = {"schema": 1, "hosts": {"mac-studio": {"class": "studio-source", "targets": False}}}
    if host_preference is not None:
        doc["host_preference"] = host_preference
    (fleet / "allocation-policy.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)
    monkeypatch.delenv("FLEET_LAST_RESORT_HOSTS", raising=False)


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
    return h


def _plan(*, slots=4):
    hosts = [kwp.PoolHost(name="ci-box", ssh_host="ci-box", ssh_user=kwp.SSH_USER, slots=slots,
                          capacity_pct=0.8, absence="required", profiles=("alpha",), state="active",
                          enabled=True, priority=0)]
    cfg = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=tuple(hosts))
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason, p.default_remote = "admitting", None, True
    return p


def _tick(conn, plan, *, spawn_limit=4):
    """``spawn_limit`` > 0: the Studio HAS a free local slot this tick."""
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=spawn_limit,
                            spillover=plan, reconcile_orphans=False)
    return res, spawned


def _placement(conn, tid):
    (row,) = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='allocation'",
                          (tid,)).fetchall()
    return json.loads(row[0])


# -- 1-3: the order -----------------------------------------------------------

def test_no_studio_need_goes_to_the_pool_while_the_studio_has_a_slot(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha", body="Refactor the parser.")
        res, spawned = _tick(conn, _plan(), spawn_limit=4)
        assert spawned == [(tid, "ci-box")]
        assert _placement(conn, tid)["reason"] == "pool"


def test_a_studio_need_card_still_runs_on_the_studio(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha", body="Do the thing.\nhost: studio\n")
        res, spawned = _tick(conn, _plan(), spawn_limit=4)
        assert spawned == [(tid, "studio")]
        assert _placement(conn, tid)["route_class"] == "local"


def test_every_pool_host_refusing_falls_back_to_the_studio(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha", body="Refactor the parser.")
        res, spawned = _tick(conn, _plan(slots=0), spawn_limit=4)
        assert spawned == [(tid, "studio")]
        rec = _placement(conn, tid)
        assert rec["route_class"] == "portable" and rec["take_refusal"]


# -- 4: the card's own Studio worktree ------------------------------------------

def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name, cwd=repo)


def _sha(repo, ref):
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def studio_tree(home, tmp_path, monkeypatch):
    """A Studio repo with a card whose persisted path is its own linked
    ``.worktrees/<id>`` on ``wt/<id>``, pushed (origin ref == branch) and clean."""
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", f"https://github.com/{SLUG}.git", cwd=repo)
    _commit(repo, "base")
    kwt._origin_cache.clear()
    fleet = home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        SLUG: {"mirror": "/srv/ci/git-mirrors/hermes-agent.git", "hosts": ["ci-box"]}}}),
        encoding="utf-8")
    _write_policy(fleet)
    clones = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        clones.append((host.name, ws, branch)))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wt", assignee="alpha", body="Fix it.",
                             workspace_kind="worktree", workspace_path=str(repo))
        wt = repo / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=repo)
        _commit(wt, "work")
        _git("update-ref", f"refs/remotes/origin/wt/{tid}", f"wt/{tid}", cwd=repo)
        conn.execute("UPDATE tasks SET workspace_path=?, branch_name=? WHERE id=?",
                     (str(wt), f"wt/{tid}", tid))
        conn.commit()
    return tid, repo, wt, clones


def _route(tid):
    with kb.connect_closing() as conn:
        rules: dict = {}
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return kbd._route_row(conn, row, _plan(), rules_out=rules), rules.get(tid)


def test_own_pushed_clean_studio_worktree_goes_to_the_pool(studio_tree):
    tid, repo, wt, clones = studio_tree
    (route, _arg), rule = _route(tid)
    assert route == "portable" and rule != "workspace_kind"
    with kb.connect_closing() as conn:
        res, spawned = _tick(conn, _plan(), spawn_limit=4)
        row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()
    assert spawned == [(tid, "ci-box")]
    assert clones and clones[0][0] == "ci-box" and clones[0][2] == f"wt/{tid}"
    assert row["workspace_kind"] == "scratch" and row["workspace_path"] == clones[0][1]
    assert (wt / "work").exists()  # the Studio tree is left untouched


@pytest.mark.parametrize("damage", ["untracked", "modified", "ahead", "no_origin_ref", "other_branch"])
def test_a_studio_worktree_with_studio_only_work_stays_local(studio_tree, damage, monkeypatch):
    tid, repo, wt, _clones = studio_tree
    if damage == "untracked":
        (wt / "notes.txt").write_text("x", encoding="utf-8")
    elif damage == "modified":
        (wt / "work").write_text("changed", encoding="utf-8")
    elif damage == "ahead":
        _commit(wt, "local-only")
    elif damage == "no_origin_ref":
        _git("update-ref", "-d", f"refs/remotes/origin/wt/{tid}", cwd=repo)
    elif damage == "other_branch":
        _git("checkout", "-q", "-b", "side", cwd=wt)
        _commit(wt, "side-only")
    if damage in ("ahead", "no_origin_ref", "other_branch"):
        # Committed work origin lacks: derived mode (the default since #176, t_1d2c5939) pushes the
        # card branch then ports it; the hand list (its kill switch) still keeps it on the Studio. A tree on
        # another branch whose HEAD contains the card branch (t_20fa16c7) pushes that HEAD as the card branch.
        with kb.connect_closing() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            out: dict = {}
            assert kbd._route_row(conn, row, _plan(), worktree_out=out) == ("portable", None)
        push = out[tid][4]
        assert push is not None and push[1] == f"wt/{tid}"
        assert push[2] == ("" if damage == "no_origin_ref" else _sha(repo, f"refs/remotes/origin/wt/{tid}"))
        assert push[3] == _sha(wt, "HEAD")
        monkeypatch.setenv(kwt.DERIVED_ENV, "0")
    (route, _arg), rule = _route(tid)
    assert route == "local" and rule == "workspace_kind"


@pytest.mark.parametrize("how", ["no_key", "switch_off", "env_empty", "env_other_host"])
def test_shared_setting_off_keeps_the_studio_worktree_local(studio_tree, home, monkeypatch, how):
    tid, *_ = studio_tree
    if how == "no_key":
        _write_policy(home / "fleet", None)
    elif how == "switch_off":
        _write_policy(home / "fleet", {**HOST_PREF, "consumers": {"kanban": "off", "ci": "on"}})
    elif how == "env_empty":
        monkeypatch.setenv("FLEET_LAST_RESORT_HOSTS", "")
    else:
        monkeypatch.setenv("FLEET_LAST_RESORT_HOSTS", "nas-fleet")
    (route, _arg), rule = _route(tid)
    assert route == "local" and rule == "workspace_kind"


@pytest.mark.parametrize("hp, env, want", [
    (None, None, False),
    (HOST_PREF, None, True),
    ({**HOST_PREF, "consumers": {"kanban": "off"}}, None, False),
    ({**HOST_PREF, "consumers": {"ci": "on"}}, None, False),
    ({**HOST_PREF, "last_resort": ["Mac-Studio"]}, None, True),
    (HOST_PREF, "", False),
    (HOST_PREF, "nas-fleet,mac-studio", True),
    (None, "mac-studio", False),
])
def test_vendored_reader_follows_the_shared_key(tmp_path, monkeypatch, hp, env, want):
    from hermes_cli import allocation_policy as ppol
    if env is None:
        monkeypatch.delenv("FLEET_LAST_RESORT_HOSTS", raising=False)
    else:
        monkeypatch.setenv("FLEET_LAST_RESORT_HOSTS", env)
    _write_policy(tmp_path, hp)
    assert ppol.load(tmp_path).is_last_resort(kwp.STUDIO_HOST) is want
