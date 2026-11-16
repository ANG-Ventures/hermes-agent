"""Studio default-deny on a card that was PORTED to a pool host and came back (t_f914c529).

t_cf13ff3b, 2026-11-12: created with no ``requires:`` line, ported to ci-box (``worktree_ported``), blocked there
``kind=capability``, re-routed by the dispatcher (``placement_rerouted`` to_host ``studio``, body gains
``host: studio`` + ``requires: studio-state``). For 4.5 h every placement_wait read
``not_portable:requires:studio-state``: the old port event still bound the card to ci-box, so the route never reached
the default-deny check. No ``studio_default_deny`` code, no wake to the home session, ``kanban admit`` refused with
the bind's code, and ``studio-override`` changed nothing. A fresh card with the same body read
``studio_default_deny:studio-state`` and started 75 s after an override.

Rules pinned here, built from that event shape:
1. The port bind ends ONLY when the card now carries a hard requirement the port host cannot serve, and only after
   that host's clone is proven clean and pushed (remote GC's predicate). The card is then re-pointed to a FRESH path
   (``worktree_handed_over``: from host, clone path, branch, head) and waits ``studio_default_deny:<cap>``.
2. A dirty/unpushed/unreadable clone keeps the card held ``port_stranded:<host> (<clone path>: <why> ...)``.
3. A ported, re-routed card with no hard requirement its host lacks keeps its bind (today's behaviour).
4. The full tick writes the same ``mechanical_wake``; a hold whose capability CHANGES wakes again (t_17732be2).
5. ``studio-override`` then releases it; ``kanban admit --host studio`` names the override command, and the spawn
   gets the fresh path filled with the card's branch at the clone's head.
6. ``studio-override`` on a card whose latest wait is not a default-deny hold says the override does not release it.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_studio_allowlist as ksa
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_pins_and_carry import _row, _studio_last_resort_on  # noqa: F401
from tests.hermes_cli.test_kanban_studio_capability_only import _caps
from tests.hermes_cli.test_kanban_studio_default_deny import _cfg

# t_cf13ff3b's body after the dispatcher re-route (kanban_placement_block.pin_body)
REROUTED_BODY = ("host: studio\nrequires: studio-state  # re-routed after capability block: Placed on ci-box\n"
                 "Follow-up to t_73572f17: replay the Studio lcm.db")


@pytest.fixture
def clean_clone(monkeypatch):
    """The port host's clone passes remote GC's proof at HEAD ``abc123`` (no ssh in tests)."""
    calls = []

    def probe(host, path, runner=None):
        calls.append((host.name, path))
        return True, "abc123"
    monkeypatch.setattr(kwt, "probe_clone", probe)
    kwt._probe_memo.clear()
    return calls


@pytest.fixture
def operator(kanban_home, monkeypatch):
    p = kb.operator_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tok")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    return kanban_home


def _ported_card(conn, *, rerouted=True, host="ace-ai", body=REROUTED_BODY):
    """A ready scratch card with t_cf13ff3b's event history: ported to ``host``, ran there, blocked, re-routed."""
    tid = kb.create_task(conn, title="lcm writer fix", assignee="alpha", body=body)
    # the port persists the pool-host path; the hermetic home stands in for /Volumes/fleet-scratch
    path = str(kb.kanban_home() / "fleet-scratch" / "workspaces" / "default" / tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (path, tid))
        kb._append_event(conn, tid, kwt.PORTED_EVENT, {"host": host, "repo": "NousResearch/hermes-agent",
                                                        "mirror": "~/.hermes/mirrors/x.git",
                                                        "branch": f"wt/{tid}", "workspace": path})
        kb._append_event(conn, tid, "worker_allocated", {"host": host, "workspace": path, "reason": "overflow"})
        kb._append_event(conn, tid, "blocked", {"kind": "capability", "reason": "Placed on ci-box"})
        if rerouted:
            kb._append_event(conn, tid, "allocation_rerouted", {"from_host": host, "to_host": "studio", "run_id": 1})
    return tid, path


def _route(conn, tid):
    return kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=_caps(), rules_out={}, carry_out={},
                          studio_out={})


def test_ported_host_is_unchanged_by_a_reroute(kanban_home):
    """ported_host() still names the port host; the dispatcher decides whether the bind holds."""
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        assert kwt.ported_host(conn, tid, path) == "ace-ai"


# -- 1. the wait code and the hand-over --------------------------------------------------------------------------

def test_rerouted_ported_card_is_handed_over_and_waits_on_studio_default_deny(kanban_home, clean_clone):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        route = _route(conn, tid)
        new = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        ev = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                     (tid, kwt.HANDOVER_EVENT)).fetchone()[0])
    assert route[0] == "wait", route
    assert route[1].startswith("studio_default_deny:studio-state ("), route
    assert clean_clone == [("ace-ai", path)]
    assert new == f"{path}-from-ace-ai" and new != path
    assert ev == {"from_host": "ace-ai", "workspace": path, "path": new, "kind": "scratch",
                  "repo": "NousResearch/hermes-agent",
                  "mirror": "~/.hermes/mirrors/x.git", "branch": f"wt/{tid}", "head": "abc123",
                  "missing": ["studio-state"]}


def test_rerouted_ported_card_routes_local_when_default_deny_is_off(kanban_home, clean_clone):
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        assert _route(conn, tid)[0] == "local"


@pytest.mark.parametrize("why", ["dirty, ignored or untracked work", "local commits not on any remote",
                                 "probe failed: timed out"])
def test_a_clone_with_work_on_the_port_host_stays_held_and_named(kanban_home, monkeypatch, why):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (False, why))
    kwt._probe_memo.clear()
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        route = _route(conn, tid)
        still = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        handed = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?",
                              (tid, kwt.HANDOVER_EVENT)).fetchone()[0]
    assert route == ("wait", route[1]) and route[1].startswith(f"port_stranded:ace-ai ({path}: {why};"), route
    assert "studio-state" in route[1]
    assert still == path and handed == 0


def test_an_existing_directory_at_the_handover_path_is_never_reused(kanban_home, clean_clone):
    import os
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        os.makedirs(f"{path}-from-ace-ai")
        route = _route(conn, tid)
    assert route[1].startswith("port_stranded:ace-ai") and "never reused" in route[1], route


def test_rerouted_port_without_a_hard_requirement_the_host_lacks_keeps_its_bind(kanban_home, clean_clone):
    """Operator send-back 1: 55 ported cards were re-routed in 7 days, most after hours of work in their clone.
    Only a hard requirement the port host cannot serve ends the bind."""
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn, body="host: studio\nfix the lint")
        route = _route(conn, tid)
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0] == path
    assert route[0] == "pin" and route[1] == "ace-ai", route
    assert clean_clone == []


def test_port_without_a_later_studio_reroute_still_binds(kanban_home, clean_clone):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn, rerouted=False, body="plain work")
        route = _route(conn, tid)
    assert route == ("pin", "ace-ai"), route


# -- 2. the notice ----------------------------------------------------------------------------------------------

def test_full_tick_wakes_the_home_session_once_with_the_override_command(kanban_home, monkeypatch,
                                                                          clean_clone):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        wakes = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake'", (tid,))]
        wait = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='allocation_wait' "
                                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    assert tid not in [t for t, _h in spawned], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:studio-state"), res.placement_waits
    assert wait["reason"].startswith("studio_default_deny:studio-state"), wait
    assert len(wakes) == 1, wakes
    assert wakes[0]["source"] == ksa.HOLD_WAKE_SOURCE
    assert f"hermes kanban studio-override {tid} --capability studio-state" in wakes[0]["text"]


def test_a_hold_whose_capability_changes_wakes_again(kanban_home):
    """t_17732be2: held studio-state, overridden, ran, sent back, held again as workspace_kind:origin_not_ours with
    no notice. A wake now records its capability; a new capability in the same episode is a new notice."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="requires: studio-state")
        first = "studio_default_deny:studio-state (declared: x; no pool host has it; not on the Studio allowlist)"
        second = ("studio_default_deny:workspace_kind:origin_not_ours (card: /w; no pool host has it; "
                  "not on the Studio allowlist)")
        assert ksa.notify_holds(conn, {tid: first}) == [tid]
        assert ksa.notify_holds(conn, {tid: first}) == []
        assert ksa.notify_holds(conn, {tid: second}) == [tid]
        assert ksa.notify_holds(conn, {tid: second}) == []
        wakes = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake' ORDER BY id", (tid,))]
    assert [w["capability"] for w in wakes] == ["studio-state", "workspace_kind:origin_not_ours"]
    assert (f"hermes kanban studio-override {tid} --capability workspace_kind:origin_not_ours"
            in wakes[1]["text"])


# -- 3. override + admit ----------------------------------------------------------------------------------------

def test_override_releases_the_rerouted_ported_card(operator, clean_clone):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        assert _route(conn, tid)[0] == "wait"
        ksa.override(conn, tid, "studio-state", "must replay the live Studio lcm.db", actor="apollo")
        assert _route(conn, tid)[0] == "local"


def test_admit_on_the_rerouted_ported_card_names_the_override_then_spawns_in_the_handed_over_clone(
        operator, monkeypatch, clean_clone):
    """The spawn reads ``workspace`` (operator send-back 2: the old stub ignored it). The Studio run gets a fresh path
    filled with the card's branch at the clone's head, never the port host's path."""
    from hermes_cli import kanban_admit as ka
    _cfg(operator, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    clones, spawned = [], []

    class Proc:
        returncode, stderr = 0, ""

    def runner(argv, **kw):
        clones.append(argv[-1])
        return Proc()
    real = kwt.materialize_handover
    monkeypatch.setattr(kwt, "materialize_handover",
                        lambda conn, task, workspace, placed_host=None, runner_=None: real(
                            conn, task, workspace, placed_host, runner=runner))

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio", str(workspace)))
        return 4242

    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
        assert spawned == [], spawned
        line = ka.refusal_from(res, tid)
        assert f"hermes kanban studio-override {tid} --capability studio-state" in line, line
        ksa.override(conn, tid, "studio-state", "must replay the live Studio lcm.db", actor="apollo")
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
    assert spawned == [(tid, "studio", f"{path}-from-ace-ai")], ka.refusal_from(res, tid)
    assert len(clones) == 1, clones
    script = clones[0]
    assert f"w={path}-from-ace-ai;" in script and f"wt/{tid}" in script and "abc123" in script, script
    assert "https://github.com/NousResearch/hermes-agent.git" in script


# -- 5. the override verb names a hold it cannot release --------------------------------------------------------

def test_override_on_a_non_deny_hold_says_it_does_not_release(operator, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="requires: studio-state")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "allocation_wait", {"code": "not_portable:requires:studio-state",
                                                           "reason": "not_portable:requires:studio-state",
                                                           "route": "wait", "host": None})
    rc = ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="studio-state",
                                                    why="must replay the live lcm.db", until=None, board=None))
    err = capsys.readouterr().err
    assert rc == 0
    assert "not_portable:requires:studio-state" in err and "does not release" in err, err


# -- 6. send-back 13:18 / 13:36: stranded-clone notice, Studio-only hand-over, gated short probe ------------------

def test_full_tick_on_a_stranded_clone_wakes_the_home_session_once(kanban_home, monkeypatch):
    """Operator send-back 2 (13:18): a card held over a stranded clone notifies its home session, once."""
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (False, "local commits not on any remote"))
    kwt._probe_memo.clear()
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        wakes = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake'", (tid,))]
    assert tid not in [t for t, _h in spawned], spawned
    assert res.placement_waits[tid].startswith("port_stranded:ace-ai ("), res.placement_waits
    assert len(wakes) == 1, wakes
    assert wakes[0]["capability"] == "port_stranded:ace-ai"
    text = wakes[0]["text"]
    assert "ace-ai" in text and path in text and "push" in text, text


def test_stranded_hold_pages_after_an_hour(kanban_home):
    reason = "port_stranded:ace-ai (/w/t: local commits not on any remote; the card needs studio-state, ...)"
    pages = []

    class Pager:
        def page(self, key, text):
            pages.append((key, text))
            return "sent"
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="requires: studio-state")
        assert ksa.notify_holds(conn, {tid: reason}, pager_factory=Pager) == [tid]
        t0 = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='mechanical_wake'",
                          (tid,)).fetchone()[0]
        ksa.notify_holds(conn, {tid: reason}, now=t0 + 59 * 60, pager_factory=Pager)
        assert pages == []
        ksa.notify_holds(conn, {tid: reason}, now=t0 + 61 * 60, pager_factory=Pager)
        ksa.notify_holds(conn, {tid: reason}, now=t0 + 90 * 60, pager_factory=Pager)
    assert len(pages) == 1 and pages[0][0] == f"held:{tid}" and "/w/t" in pages[0][1], pages


def test_a_hard_requirement_another_pool_host_serves_keeps_the_bind(kanban_home, clean_clone):
    """Operator ruling 13:36 (Prism d6cfdbb53981): the hand-over goes to the Studio only. A requirement the port host
    lacks but another POOL host serves keeps today's bind: no pool-to-pool hand-over."""
    import re
    from hermes_cli import kanban_host_capabilities as khc
    allcaps = ("gpu", "studio-state")
    cmap = khc.CapabilityMap(capabilities=allcaps,
                             hosts={khc.STUDIO_ROW: frozenset({"studio-state"}), "ace-ai": frozenset(),
                                    "ace-media": frozenset({"gpu"})},
                             sniff=(("studio-state", re.compile(r"(?!x)x")),), soft={c: False for c in allcaps})
    caps = khc.CapabilityTick(cmap, khc.ENFORCE, allcaps, ("ace-ai", "ace-media"))
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn, body="requires: gpu\nrender it")
        row = _row(conn, tid)
        assert kwt.hard_missing(row, caps, "ace-ai") == []
        kbd._route_row(conn, row, _plan(free=2), caps=caps, rules_out={}, carry_out={}, studio_out={})
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0] == path
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?",
                            (tid, kwt.HANDOVER_EVENT)).fetchone()[0] == 0
    assert clean_clone == []


class _Grant:
    def __init__(self, host="ace-ai", rid="r-1", reason=""):
        self.host, self.reservation_id, self.reason, self.consumer = host, rid, reason, "kanban"


def _pool_host():
    from hermes_cli.kanban_worker_pool import PoolHost
    return PoolHost("ace-ai", "ace-ai", "kanbanw", 4, 100.0, "wait", (), "active", True, 0)


def test_probe_runs_under_a_router_reservation_with_a_short_timeout(monkeypatch):
    from hermes_cli.fleet_placement import alloc_client
    released, calls = [], []
    monkeypatch.setattr(alloc_client, "admit", lambda consumer, **kw: _Grant())
    monkeypatch.setattr(alloc_client, "release", lambda rid, consumer: released.append(rid) or True)

    class Proc:
        returncode, stdout, stderr = 0, "abc123\n", ""

    def runner(argv, **kw):
        calls.append(kw)
        return Proc()
    assert kwt.probe_clone(_pool_host(), "/w/t", runner) == (True, "abc123")
    assert calls[0]["env"]["ALLOC_RESERVATION"] == "r-1"
    assert calls[0]["timeout"] <= 10 and calls[0]["encoding"] == "utf-8"
    assert released == ["r-1"]


def test_probe_timeout_or_refused_admit_is_not_proven(monkeypatch):
    import subprocess
    from hermes_cli.fleet_placement import alloc_client
    released = []
    monkeypatch.setattr(alloc_client, "release", lambda rid, consumer: released.append(rid) or True)

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(alloc_client, "admit", lambda consumer, **kw: _Grant())
    ok, why = kwt.probe_clone(_pool_host(), "/w/t", slow)
    assert not ok and "probe failed" in why and released == ["r-1"]
    ran = []
    monkeypatch.setattr(alloc_client, "admit", lambda consumer, **kw: _Grant(host=None, rid=None, reason="all_closed"))
    ok, why = kwt.probe_clone(_pool_host(), "/w/t", lambda argv, **kw: ran.append(argv))
    assert not ok and "all_closed" in why and ran == []


def _granted(monkeypatch):
    from hermes_cli.fleet_placement import alloc_client
    monkeypatch.setattr(alloc_client, "admit", lambda consumer, **kw: _Grant())
    monkeypatch.setattr(alloc_client, "release", lambda rid, consumer: True)


class _Failed:
    returncode, stdout, stderr = 13, "", "dirty, ignored or untracked work\n"


def test_a_failed_clone_proof_is_not_proven(monkeypatch):
    """Operator send-back 14:24 (mutant M3: ``if proc.returncode != 0`` -> ``if False``): rc 13 is not proven."""
    _granted(monkeypatch)
    assert kwt.probe_clone(_pool_host(), "/w/t", lambda argv, **kw: _Failed()) == (
        False, "dirty, ignored or untracked work")


def test_full_tick_over_a_failed_clone_proof_never_hands_over(kanban_home, monkeypatch):
    """The real probe_clone (ssh runner stubbed to rc 13) inside a full tick: the card stays port_stranded on its
    clone path and no worktree_handed_over is written."""
    _cfg(kanban_home, True, [])
    _granted(monkeypatch)
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    real = kwt.probe_clone
    monkeypatch.setattr(kwt, "probe_clone",
                        lambda host, path, runner=None: real(host, path, lambda argv, **kw: _Failed()))
    kwt._probe_memo.clear()
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        still = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        handed = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?",
                              (tid, kwt.HANDOVER_EVENT)).fetchone()[0]
    assert tid not in [t for t, _h in spawned], spawned
    assert res.placement_waits[tid].startswith(f"port_stranded:ace-ai ({path}: dirty, ignored or untracked work;")
    assert still == path and handed == 0


def test_the_remote_proof_check_mode_against_real_repos(tmp_path):
    """kanban_survivor._REMOTE_PROOF in check mode, local python, temp git repos (no ssh): clean and pushed gives rc 0
    and HEAD; an uncommitted edit, a stash, an unpushed commit give rc 13; a missing path gives rc 0 and no output.
    Check mode never removes the directory."""
    import os
    import subprocess
    import sys
    root = tmp_path.resolve()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def proof(path):
        return subprocess.run([sys.executable, "-c", _gh(root), str(path), "check"], capture_output=True,
                              text=True, env=env, timeout=60)

    def clone(name):
        d = root / name
        git(root, "clone", "-q", str(root / "remote.git"), str(d))
        return d

    git(root, "init", "-q", "--bare", "remote.git")
    seed = clone("seed")
    (seed / "f.txt").write_text("one\n")
    git(seed, "add", "f.txt")
    git(seed, "commit", "-qm", "one")
    git(seed, "push", "-q", "origin", "HEAD")

    clean = clone("clean")
    r = proof(clean)
    assert (r.returncode, r.stdout.strip()) == (0, git(clean, "rev-parse", "HEAD")), r.stderr

    dirty = clone("dirty")
    (dirty / "f.txt").write_text("edited\n")
    stashed = clone("stashed")
    (stashed / "f.txt").write_text("stashed\n")
    git(stashed, "stash", "-q")
    unpushed = clone("unpushed")
    (unpushed / "f.txt").write_text("two\n")
    git(unpushed, "commit", "-qam", "two")
    for d in (dirty, stashed, unpushed):
        r = proof(d)
        assert r.returncode == 13 and r.stdout == "", (d.name, r.returncode, r.stdout, r.stderr)
    for d in (clean, dirty, stashed, unpushed):
        assert (d / ".git").is_dir() and (d / "f.txt").is_file(), d

    r = proof(root / "gone")
    assert (r.returncode, r.stdout) == (0, ""), r


def test_a_brace_in_the_clone_path_does_not_break_the_held_reason(kanban_home, monkeypatch):
    """Prism P2 1ca2affda3f7: the held reason was built with an f-string and then .format(why=...); a brace in the
    path, the host or a capability raised inside the tick."""
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (False, "stashes"))
    kwt._probe_memo.clear()
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        braced = path + "-{x}{0}"
        reason = kwt.release_port_bind(conn, {"id": tid, "workspace_path": braced}, "ace-ai", ["cap{y}"],
                                       [_pool_host()])
    assert reason.startswith(f"port_stranded:ace-ai ({braced}: stashes; the card needs cap{{y}},"), reason


def test_a_handed_over_card_never_materializes_on_a_pool_host(kanban_home, clean_clone):
    """Prism d6cfdbb53981: a pool start would leave its clone with no port binding. The hand-over is Studio-only."""
    _cfg(kanban_home, True, [])
    ran = []
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        _route(conn, tid)
        task = kb.get_task(conn, tid)
        with pytest.raises(RuntimeError, match="Studio only"):
            kwt.materialize_handover(conn, task, task.workspace_path, _pool_host(),
                                     runner=lambda argv, **kw: ran.append(argv))
    assert task.workspace_path == f"{path}-from-ace-ai" and ran == []


def _tag_repos(root):
    """remote.git with main + an annotated tag on a commit no branch holds (courierwatch-v1 shape)."""
    import os
    import subprocess
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def clone(name):
        d = root / name
        git(root, "clone", "-q", str(root / "remote.git"), str(d))
        return d

    git(root, "init", "-q", "--bare", "remote.git")
    seed = clone("seed")
    (seed / "f.txt").write_text("one\n")
    git(seed, "add", "f.txt")
    git(seed, "commit", "-qm", "one")
    git(seed, "push", "-q", "origin", "HEAD")
    git(seed, "checkout", "-q", "--detach")
    (seed / "f.txt").write_text("off-branch\n")
    git(seed, "commit", "-qam", "off-branch")
    git(seed, "tag", "-a", "-m", "v1", "courierwatch-v1")
    git(seed, "push", "-q", "origin", "refs/tags/courierwatch-v1")
    return git, clone, env


def test_stranded_probe_ignores_a_tag_origin_already_has(tmp_path):
    """t_19c48fb7: a clean clone that fetched an origin tag sitting on no remote branch is NOT stranded (the old
    `rev-list --branches --tags --not --remotes` returned the tag's commit and held the card port_stranded); a
    local-only tag on an unpushed commit and an unpushed branch commit ARE stranded; unreadable remote tags hold."""
    import subprocess
    import sys
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)

    def proof(path):
        return subprocess.run([sys.executable, "-c", _gh(root), str(path), "check"], capture_output=True,
                              text=True, env=env, timeout=60)

    clean = clone("clean")
    assert git(clean, "tag", "-l") == "courierwatch-v1"
    assert git(clean, "rev-list", "--tags", "--not", "--remotes")  # the old probe's false positive
    r = proof(clean)
    assert (r.returncode, r.stdout.strip()) == (0, git(clean, "rev-parse", "HEAD")), r.stderr

    local_tag = clone("local_tag")
    git(local_tag, "checkout", "-q", "--detach")
    (local_tag / "f.txt").write_text("mine\n")
    git(local_tag, "commit", "-qam", "mine")
    git(local_tag, "tag", "mine-v1")
    git(local_tag, "checkout", "-q", "-")
    r = proof(local_tag)
    assert r.returncode == 13 and "local commits not on any remote" in r.stderr, (r.returncode, r.stderr)

    moved_tag = clone("moved_tag")  # same NAME as origin's tag, different object: still local work
    git(moved_tag, "checkout", "-q", "--detach")
    (moved_tag / "f.txt").write_text("retagged\n")
    git(moved_tag, "commit", "-qam", "retagged")
    git(moved_tag, "tag", "-f", "-a", "-m", "v1 moved", "courierwatch-v1")
    git(moved_tag, "checkout", "-q", "-")
    r = proof(moved_tag)
    assert r.returncode == 13 and "local commits not on any remote" in r.stderr, (r.returncode, r.stderr)

    branch = clone("branch")
    (branch / "f.txt").write_text("two\n")
    git(branch, "commit", "-qam", "two")
    r = proof(branch)
    assert r.returncode == 13 and "local commits not on any remote" in r.stderr, (r.returncode, r.stderr)

    blind = clone("blind")  # origin tags unreadable: unknown, held -- never "clean"
    git(blind, "remote", "set-url", "origin", str(root / "gone.git"))
    r = proof(blind)
    assert r.returncode == 13 and "unknown" in r.stderr, (r.returncode, r.stderr)


def _gh(root, monkeypatch=None):
    """The proof certifies only GitHub remotes, for the exact-tip answer too (t_f47e4e3f). A test remote is a local
    bare repo, so the TEST proof also accepts URLs under `root`: G gets one extra prefix in this text copy only; the
    shipped _REMOTE_PROOF has no env, flag or path hook that widens G. `url.<bare>.insteadOf=https://github.com/...` does NOT work for
    this: the proof reads the URL git actually fetches from (`ls-remote --get-url`, insteadOf applied), so a
    rewritten GitHub URL is a local path and is held (test_remote_proof_refuses_each_unsupported_shape)."""
    from hermes_cli import kanban_survivor as ks
    g = "G=('https://github.com/',"
    assert ks._REMOTE_PROOF.count(g) == 1
    return ks._REMOTE_PROOF.replace(g, g + repr(str(root) + "/") + ",")


def _run(script, path, env):
    import subprocess
    import sys
    return subprocess.run([sys.executable, "-c", script, str(path), "check"], capture_output=True, text=True,
                          env=env, timeout=60)


def _moved(root, git, clone, *names):
    """Clones of remote.git, then origin's branch moves one commit on: each clone's HEAD is behind the tip."""
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")  # no tags: the stranded() tag read is not the gate
    ds = [clone(n) for n in names]
    seed, branch = root / "seed", git(ds[0], "symbolic-ref", "--short", "HEAD")
    git(seed, "checkout", "-q", "-B", branch, f"origin/{branch}")
    (seed / "f.txt").write_text("two\n")
    git(seed, "commit", "-qam", "two")
    git(seed, "push", "-q", "origin", branch)
    return ds, git(seed, "rev-parse", "HEAD~1"), git(seed, "rev-parse", "HEAD")


def test_remote_proof_tip_equality_mutant_holds_the_behind_clone(tmp_path, monkeypatch):
    """Mutation: drop the reachability walk (tip-equality only, the pre-t_3af8cae9 rule) and the test below is red."""
    from hermes_cli import kanban_survivor as ks
    anchor = "if k and git(d,'rev-list'"
    assert ks._REMOTE_PROOF.count(anchor) == 1
    monkeypatch.setattr(ks, "_REMOTE_PROOF", ks._REMOTE_PROOF.replace(anchor, "if 0 and git(d,'rev-list'"))
    with pytest.raises(AssertionError, match="HEAD has no independently durable ref"):
        test_remote_proof_head_reachable_not_tip_equal(tmp_path)


def test_remote_proof_head_reachable_not_tip_equal(tmp_path):
    """t_3af8cae9: a clean clone whose HEAD is one commit behind origin's branch tip (main moved after the claim; the
    clone never fetched it) is NOT held -- the old proof wanted HEAD to be the exact TIP of a remote ref and held
    t_6e7909d1/t_a982e7eb `HEAD has no independently durable ref`. A detached commit on no remote ref IS held; a
    remote that cannot be read holds as unknown, never clean."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    (behind, detached, blind), head, tip = _moved(root, git, clone, "behind", "detached", "blind")
    proof = _gh(root)
    assert git(behind, "rev-parse", "HEAD") == head != tip
    assert head not in git(behind, "ls-remote", "origin")  # no remote ref is AT the clone's HEAD
    r = _run(proof, behind, env)
    assert (r.returncode, r.stdout.strip()) == (0, head), r.stderr

    git(detached, "checkout", "-q", "--detach")
    (detached / "f.txt").write_text("mine\n")
    git(detached, "commit", "-qam", "mine")
    r = _run(proof, detached, env)
    assert r.returncode == 13 and "HEAD has no independently durable ref" in r.stderr, (r.returncode, r.stderr)

    git(blind, "remote", "set-url", "origin", str(root / "gone.git"))
    r = _run(proof, blind, env)
    assert r.returncode == 13 and "unknown" in r.stderr, (r.returncode, r.stderr)


def test_remote_proof_holds_head_a_shallow_remote_lacks(tmp_path):
    """Prism 813395673ff1 repro: full local history A -> B, branch reset to A with B still cached, the ONLY remote a
    depth-1 clone holding B and not A. Local reachability from cached B proves nothing about the remote: HELD
    (a path remote gives no ancestry proof)."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    full = clone("full")
    (full / "f.txt").write_text("two\n")
    git(full, "commit", "-qam", "B")
    git(full, "push", "-q", "origin", "HEAD")
    a, b = git(full, "rev-parse", "HEAD~1"), git(full, "rev-parse", "HEAD")
    git(root, "clone", "-q", "--bare", "--depth", "1", (root / "remote.git").as_uri(), "shallow.git")
    assert git(root / "shallow.git", "rev-parse", "--is-shallow-repository") == "true"
    git(full, "remote", "set-url", "origin", str(root / "shallow.git"))
    git(full, "reset", "-q", "--hard", a)
    git(full, "fetch", "-q", "origin")
    assert git(full, "cat-file", "-t", b) == "commit" and git(full, "merge-base", "--is-ancestor", a, b) == ""
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    r = _run(_REMOTE_PROOF, full, env)
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, (r.returncode, r.stderr)


def test_remote_proof_one_readable_remote_proves_despite_an_unreadable_one(tmp_path):
    """Prism P2 329090a45473: decide per remote. An unreadable first remote does not hold a clone a second remote
    proves; unknown only when no remote proves."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    (behind,), _, _ = _moved(root, git, clone, "behind")
    git(behind, "remote", "rename", "origin", "zz")
    git(behind, "remote", "add", "aa", str(root / "gone.git"))
    assert git(behind, "remote").split() == ["aa", "zz"]
    r = _run(_gh(root), behind, env)
    assert r.returncode == 0, r.stderr
    git(behind, "remote", "set-url", "zz", str(root / "gone2.git"))
    r = _run(_gh(root), behind, env)
    assert r.returncode == 13 and "unknown" in r.stderr, (r.returncode, r.stderr)


def test_remote_proof_ignores_local_grafts_and_replace_refs(tmp_path):
    """Prism 051b2f1f71e3 repro: the remote holds A -> B; the clone has a detached, unpushed H parented by A and a
    local graft (or replace ref) making B descend from H. Reachability must be the remote's history: HELD."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    (graft, replace), a, b = _moved(root, git, clone, "graft", "replace")
    for d, why in ((graft, "unsupported:grafts"), (replace, "unsupported:replace_refs")):
        git(d, "checkout", "-q", "--detach", a)
        (d / "f.txt").write_text("H\n")
        git(d, "commit", "-qam", "H")
        h = git(d, "rev-parse", "HEAD")
        git(d, "fetch", "-q", "origin")  # B cached locally
        if d is graft:
            (d / ".git" / "info" / "grafts").write_text(f"{b} {h}\n")
        else:
            git(d, "replace", "--graft", b, h)
        assert git(d, "merge-base", "--is-ancestor", h, b) == ""  # the forged history says H is on the remote
        r = _run(_gh(root), d, env)
        assert r.returncode == 13 and why in r.stderr, (why, r.returncode, r.stdout, r.stderr)


def test_remote_proof_holds_a_replace_ref_even_at_the_exact_tip(tmp_path):
    """Prism b6d2f9d152e3 repro: HEAD is the exact remote tip, but refs/replace/<HEAD> points at a commit that
    exists only in this clone (refs/replace is under no branch, so stranded() never sees it). Deleting the clone
    loses it: HELD unsupported:replace_refs, before the tip match."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    d = clone("tip")
    head = git(d, "rev-parse", "HEAD")
    assert head in git(d, "ls-remote", "origin")
    x = git(d, "commit-tree", "-m", "only here", git(d, "rev-parse", "HEAD^{tree}"))
    git(d, "replace", head, x)
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    r = _run(_REMOTE_PROOF, d, env)
    assert r.returncode == 13 and "unsupported:replace_refs" in r.stderr, (r.returncode, r.stdout, r.stderr)


def test_remote_proof_holds_a_shared_local_remote_that_borrows_the_clone(tmp_path):
    """Prism 4ad6b4e63f59 repro: the only remote is a `clone --shared` of the clone itself (its objects ARE the
    clone's). Its branch holds a child of HEAD, so HEAD is "reachable" there, but deleting the clone destroys the
    remote's copy too: HELD, a path remote gives no ancestry proof."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    d = clone("borrowed")
    git(d, "checkout", "-q", "--detach")
    for msg in ("H", "H2"):
        (d / "f.txt").write_text(msg + "\n")
        git(d, "commit", "-qam", msg)
    git(root, "clone", "-q", "--bare", "--shared", str(d), "dep.git")
    git(root / "dep.git", "update-ref", "refs/heads/h2", git(d, "rev-parse", "HEAD"))
    git(d, "reset", "-q", "--hard", "HEAD~1")
    git(d, "remote", "set-url", "origin", str(root / "dep.git"))
    assert (root / "dep.git" / "objects" / "info" / "alternates").is_file()
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    r = _run(_REMOTE_PROOF, d, env)
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, (r.returncode, r.stderr)


def test_remote_proof_refuses_each_unsupported_shape(tmp_path):
    """Apollo ruling t_3af8cae9-refuse-dont-model: reachability is accepted only for a GitHub remote and a clone with
    no alternates, grafts, replace refs, shallow file or partial-clone setting. Each refusal FIRES on a behind clone
    that otherwise passes (the control), with its own reason."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    names = ("control", "alternates", "grafts", "replace_refs", "shallow", "partial_clone", "path", "insteadof")
    ds, a, b = _moved(root, git, clone, *names)
    d = dict(zip(names, ds))
    (d["alternates"] / ".git" / "objects" / "info" / "alternates").write_text(str(root / "remote.git" / "objects") + "\n")
    (d["grafts"] / ".git" / "info" / "grafts").write_text(f"{a}\n")
    git(d["replace_refs"], "fetch", "-q", "origin")
    git(d["replace_refs"], "replace", "--graft", b)
    (d["shallow"] / ".git" / "shallow").write_text(f"{a}\n")
    git(d["partial_clone"], "config", "remote.origin.promisor", "true")
    gh = "https://github.com/o/r.git"
    git(d["insteadof"], "config", f"url.{root / 'remote.git'}.insteadOf", gh)
    git(d["insteadof"], "remote", "set-url", "origin", gh)
    assert git(d["insteadof"], "ls-remote", "--get-url", "origin") == str(root / "remote.git")
    proof = _gh(root)
    r = _run(proof, d["control"], env)
    assert (r.returncode, r.stdout.strip()) == (0, a), r.stderr
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    r = _run(_REMOTE_PROOF, d["path"], env)  # the shipped proof: a path remote is not GitHub
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, r.stderr
    r = _run(_REMOTE_PROOF, d["insteadof"], env)  # a github.com URL rewritten to a path is still a path
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, r.stderr
    for why in ("alternates", "grafts", "replace_refs", "shallow", "partial_clone"):
        r = _run(proof, d[why], env)
        assert r.returncode == 13 and f"unsupported:{why}" in r.stderr, (why, r.returncode, r.stderr)
    git(d["control"], "pull", "-q", "--ff-only")  # exact tip: accepted only from a GitHub remote (t_f47e4e3f)
    r = _run(proof, d["control"], env)
    assert (r.returncode, r.stdout.strip()) == (0, b), r.stderr
    r = _run(_REMOTE_PROOF, d["control"], env)
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, r.stderr
    git(d["shallow"], "pull", "-q", "--ff-only")  # the shape refusals run before the exact-tip answer too
    assert b in git(d["shallow"], "ls-remote", "origin")
    r = _run(proof, d["shallow"], env)
    assert r.returncode == 13 and "unsupported:shallow" in r.stderr, (r.returncode, r.stderr)


def test_reclaim_uses_the_same_stranded_predicate(tmp_path):
    """kanban_reclaim's call site: the origin-tag clone is clean, an unpushed commit holds, unreadable tags hold."""
    from hermes_cli import kanban_reclaim as kr
    root = tmp_path.resolve()
    git, clone, _env = _tag_repos(root)
    clean = clone("clean")
    assert kr._repo_dirt(clean, "clean", root, {clean}, []) is None
    seed = root / "seed"  # t_3af8cae9: origin moves on; the clone's HEAD is now behind the tip, still not held
    br = git(clean, "symbolic-ref", "--short", "HEAD")
    git(seed, "checkout", "-q", "-B", br, f"origin/{br}")
    (seed / "f.txt").write_text("moved\n")
    git(seed, "commit", "-qam", "moved")
    git(seed, "push", "-q", "origin", br)
    assert kr._repo_dirt(clean, "clean", root, {clean}, []) is None
    branch = clone("branch")
    (branch / "f.txt").write_text("two\n")
    git(branch, "commit", "-qam", "two")
    with pytest.raises(kr.ReclaimHeld, match="local commits not on any remote"):
        kr._repo_dirt(branch, "branch", root, {branch}, [])
    blind = clone("blind")
    git(blind, "remote", "set-url", "origin", str(root / "gone.git"))
    with pytest.raises(kr.ReclaimHeld, match="unknown"):
        kr._repo_dirt(blind, "blind", root, {blind}, [])


def test_remote_proof_holds_an_exact_tip_on_a_shared_local_remote(tmp_path):
    """t_f47e4e3f (neg-318.sh row KNOWN): clone d at X; `git clone --bare --shared d r.git`; d's origin -> r.git.
    r.git advertises X exactly, but its objects ARE d's: deleting d destroys the only copy. HELD, never rc 0."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    d = clone("d")
    x = git(d, "rev-parse", "HEAD")
    git(root, "clone", "-q", "--bare", "--shared", str(d), "r.git")
    git(d, "remote", "set-url", "origin", str(root / "r.git"))
    git(d, "fetch", "-q", "--prune", "origin")
    assert x in git(d, "ls-remote", "origin") and (root / "r.git" / "objects" / "info" / "alternates").is_file()
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    r = _run(_REMOTE_PROOF, d, env)
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, (r.returncode, r.stdout, r.stderr)
    assert (d / ".git").is_dir()


def test_remote_proof_holds_when_any_remote_is_not_github(tmp_path):
    """t_f47e4e3f: one rule for both answers. A GitHub remote that proves HEAD at its exact tip does not license a
    clone that also carries a non-GitHub remote (the refusal runs before either answer)."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    d = clone("d")
    r = _run(_gh(root), d, env)
    assert r.returncode == 0, r.stderr
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere.git"
    git(root, "init", "-q", "--bare", str(elsewhere))
    git(d, "remote", "add", "zz", str(elsewhere))
    r = _run(_gh(root), d, env)
    assert r.returncode == 13 and "unsupported:remote_not_github" in r.stderr, (r.returncode, r.stderr)


_CAP_GIT = r'''#!/bin/sh
case " $* " in *" fetch "*)
  n=$(printf '%s\n' "$@" | grep -cE '^[0-9a-f]{40}$'); echo "$n" >> "$CAP_LOG"
  [ "$n" -gt "$CAP_N" ] && { echo "fetch of $n ids refused" >&2; exit 1; } ;;
esac
exec "$CAP_REAL" "$@"
'''


def _fetch_cap(root, env, cap):
    """A `git` first on PATH that logs each fetch's id count and refuses a fetch of more than `cap` ids: the
    stand-in for a remote with more heads than the proof's 12 s budget can fetch (hermes-home: 1,181 heads)."""
    import os
    import shutil
    bindir = root / "capbin"
    bindir.mkdir()
    (bindir / "git").write_text(_CAP_GIT)
    (bindir / "git").chmod(0o755)
    log = root / "fetches.log"
    return dict(env, PATH=str(bindir) + os.pathsep + env.get("PATH", ""), CAP_LOG=str(log), CAP_N=str(cap),
                CAP_REAL=shutil.which("git")), log


def _side_heads(root, git, tip, n):
    """`n` extra branch heads on remote.git, each a child of `tip`, written in the bare repo (no clone has them)."""
    import subprocess
    bare = root / "remote.git"
    tree = git(bare, "rev-parse", f"{tip}^{{tree}}")
    shas = [git(bare, "commit-tree", "-p", tip, "-m", f"side {i}", tree) for i in range(n)]
    subprocess.run(["git", "-C", str(bare), "update-ref", "--stdin"], check=True, text=True,
                   input="".join(f"create refs/heads/side-{i} {s}\n" for i, s in enumerate(shas)))


def test_remote_proof_does_not_fetch_every_head(tmp_path):
    """t_f47e4e3f: the ancestry answer fetched EVERY advertised head (hermes-home: 6 s of the 12 s budget). With
    300 extra heads and a git that refuses a fetch of more than 2 ids, a clone behind the remote's default branch
    is still proved: the remote's HEAD alone answers it (one fetch of one id)."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    (behind,), head, tip = _moved(root, git, clone, "behind")
    _side_heads(root, git, tip, 300)
    assert len(git(behind, "ls-remote", "--heads", "origin").splitlines()) == 301
    capped, log = _fetch_cap(root, env, 2)
    r = _run(_gh(root), behind, capped)
    assert (r.returncode, r.stdout.strip()) == (0, head), (r.returncode, r.stderr)
    assert log.read_text().split() == ["1"], log.read_text()


def test_remote_proof_cheap_answer_needs_no_fetch(tmp_path):
    """t_f47e4e3f: HEAD is an ancestor of an advertised head the clone already holds (main moved on, the clone
    fetched it, its branch did not advance): proved with NO fetch at all."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    (behind,), head, tip = _moved(root, git, clone, "behind")
    _side_heads(root, git, tip, 3)
    git(behind, "fetch", "-q", "origin")
    assert git(behind, "rev-parse", "HEAD") == head and git(behind, "cat-file", "-t", tip) == "commit"
    capped, log = _fetch_cap(root, env, 0)
    r = _run(_gh(root), behind, capped)
    assert (r.returncode, r.stdout.strip()) == (0, head), (r.returncode, r.stderr)
    assert not log.exists(), log.read_text()


def test_remote_proof_falls_back_to_the_other_heads(tmp_path):
    """t_f47e4e3f: HEAD is on a non-default branch only and that branch's tip is not in the clone: the cheap answer
    and the remote's HEAD do not prove it, the remaining heads do (one fetch). A commit on no remote head holds."""
    root = tmp_path.resolve()
    git, clone, env = _tag_repos(root)
    git(root / "remote.git", "tag", "-d", "courierwatch-v1")
    d, other = clone("d"), clone("other")
    git(other, "checkout", "-q", "-b", "side")
    (other / "f.txt").write_text("side\n")
    git(other, "commit", "-qam", "side 1")
    mine = git(other, "rev-parse", "HEAD")
    (other / "f.txt").write_text("side2\n")
    git(other, "commit", "-qam", "side 2")
    git(other, "push", "-q", "origin", "side")
    git(d, "fetch", "-q", "origin", "side")
    git(d, "checkout", "-q", "--detach", mine)
    git(d, "update-ref", "-d", "refs/remotes/origin/side")
    git(d, "reflog", "expire", "--expire=now", "--all")
    git(d, "gc", "-q", "--prune=now")
    side = git(d, "ls-remote", "origin", "refs/heads/side").split()[0]
    assert subprocess_rc(d, "cat-file", "-e", side) != 0  # the side tip is not in the clone
    capped, log = _fetch_cap(root, env, 2)
    r = _run(_gh(root), d, capped)
    assert (r.returncode, r.stdout.strip()) == (0, mine), (r.returncode, r.stderr)
    assert log.read_text().split() == ["1"], log.read_text()
    (d / "f.txt").write_text("mine\n")
    git(d, "commit", "-qam", "mine")
    r = _run(_gh(root), d, env)
    assert r.returncode == 13 and "HEAD has no independently durable ref" in r.stderr, (r.returncode, r.stderr)


def subprocess_rc(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode
