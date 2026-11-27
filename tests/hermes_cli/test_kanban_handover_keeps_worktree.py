"""A card handed over off its port host keeps its declared workspace (t_7eb1cbbb).

t_6e7909d1, 2026-11-13: created ``--workspace worktree:<repo>``, ported to ace-ai (``convert`` made it
``scratch`` at the pool path), blocked kind=capability, re-routed to the Studio. ``release_port_bind`` re-pointed
it to ``<old>-from-ace-ai`` (never created; ``head: null`` because the port host's clone path was already gone, so
the proof printed nothing), kind still ``scratch``. Admission then refused it ``stranded_by_mount_loss`` every tick
for 28 min on a mounted volume, so ``materialize_handover`` (spawn time) never ran; a manual
``workspace reset`` started it on an empty scratch dir with no repo.

Pinned here:
1. The port records the declared kind/path; the hand-over restores them (a worktree card goes back to
   ``worktree:<repo>``), else creates the scratch path it records.
2. The Studio spawn puts the card's branch at the handed-over head before the worktree is made.
3. A card already stranded on a never-created hand-over target is recovered by the dispatcher, named
   ``handover_target_never_created``, never refused as ``stranded_by_mount_loss``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_pins_and_carry import _row
from tests.hermes_cli.test_kanban_studio_capability_only import _caps
from tests.hermes_cli.test_kanban_studio_default_deny import _cfg
from tests.hermes_cli.test_kanban_studio_deny_ported_reroute import REROUTED_BODY, clean_clone, operator  # noqa: F401

SLUG = "ANG-Ventures/hermes-home"


def _route(conn, tid):
    return kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=_caps(), rules_out={}, carry_out={},
                          studio_out={})


def _ported_worktree_card(conn, repo, *, declared_in_port=True):
    """t_6e7909d1's history: a worktree card ported to ace-ai (scratch at the pool path), blocked, re-routed."""
    tid = kb.create_task(conn, title="sub-box fix", assignee="alpha", body=REROUTED_BODY,
                         workspace_kind="worktree", workspace_path=str(repo))
    pool = str(kb.kanban_home() / "fleet-scratch" / "workspaces" / "default" / tid)
    port = {"host": "ace-ai", "repo": SLUG, "mirror": "~/.hermes/mirrors/x.git", "branch": f"wt/{tid}",
            "workspace": pool}
    if declared_in_port:
        port.update(declared_kind="worktree", declared_path=str(repo))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=?, branch_name=? WHERE id=?",
                     (pool, f"wt/{tid}", tid))
        kb._append_event(conn, tid, kwt.PORTED_EVENT, port)
        kb._append_event(conn, tid, "blocked", {"kind": "capability", "reason": "needs sub-box ssh"})
        kb._append_event(conn, tid, "placement_rerouted", {"from_host": "ace-ai", "to_host": "studio", "run_id": 1})
    return tid, pool


def _card(conn, tid):
    return tuple(conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (tid,)).fetchone())


def _handovers(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kwt.HANDOVER_EVENT))]


# -- 1. the port records the declared workspace; the hand-over restores it --------------------------------------

def test_the_port_records_the_declared_workspace(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", workspace_kind="worktree",
                             workspace_path="/repo/a")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        task = kb.get_task(conn, tid)
        kwt.convert(conn, task, "/pool/x", f"wt/{tid}", host="ace-ai", slug=SLUG, mirror="~/m.git")
        (port,) = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kwt.PORTED_EVENT))]
    assert port["declared_kind"] == "worktree" and port["declared_path"] == "/repo/a", port


@pytest.mark.parametrize("declared_in_port", [True, False], ids=["port-event", "created-event-fallback"])
def test_a_handed_over_worktree_card_keeps_its_declared_kind_and_path(kanban_home, clean_clone, tmp_path,
                                                                       declared_in_port):
    _cfg(kanban_home, True, [])
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid, pool = _ported_worktree_card(conn, repo, declared_in_port=declared_in_port)
        route = _route(conn, tid)
        kind, path = _card(conn, tid)
        (ev,) = _handovers(conn, tid)
    # the ace-ai bind is gone: the card routes on the Studio as the worktree card it was declared
    assert route[0] in ("local", "wait") and not route[1].startswith("port_stranded"), route
    assert (kind, path) == ("worktree", str(repo))
    assert ev["kind"] == "worktree" and ev["path"] == str(repo) and ev["workspace"] == pool
    assert ev["head"] == "abc123" and ev["branch"] == f"wt/{tid}" and ev["repo"] == SLUG
    assert not os.path.lexists(f"{pool}-from-ace-ai")


def test_a_scratch_hand_over_creates_the_path_it_records(kanban_home, clean_clone):
    """A port event with no declared worktree (none in the event nor the created event) keeps the fresh-path
    hand-over, and the path it records exists, so admission cannot read it as a lost mount."""
    from tests.hermes_cli.test_kanban_studio_deny_ported_reroute import _ported_card
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        _route(conn, tid)
        kind, new = _card(conn, tid)
        (ev,) = _handovers(conn, tid)
    assert new == f"{path}-from-ace-ai" and ev["path"] == new and ev["kind"] == "scratch"
    assert kind == "scratch" and os.path.isdir(new)


# -- 2. the Studio spawn puts the branch at the handed-over head --------------------------------------------------

def _env():
    return dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=_env()).stdout.strip()


@pytest.fixture
def studio_repo(tmp_path, monkeypatch):
    """origin.git with main + the card branch two commits ahead; the Studio repo is a clone holding only main."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    root = tmp_path.resolve()
    _git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
    seed = root / "seed"
    _git(root, "clone", "-q", str(root / "origin.git"), str(seed))
    (seed / "f.txt").write_text("one\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "one")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    heads = []
    for n in ("two", "three"):
        (seed / "f.txt").write_text(n + "\n")
        _git(seed, "commit", "-qam", n)
        heads.append(_git(seed, "rev-parse", "HEAD"))
    studio = root / "studio"
    _git(root, "clone", "-q", str(root / "origin.git"), str(studio))

    def push_branch(branch):
        _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return studio, heads, push_branch


def _handed_over_task(conn, repo, push_branch, head):
    tid, _pool = _ported_worktree_card(conn, repo)
    push_branch(f"wt/{tid}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id=?", (str(repo), tid))
        kb._append_event(conn, tid, kwt.HANDOVER_EVENT, {
            "from_host": "ace-ai", "workspace": _pool, "path": str(repo), "kind": "worktree", "repo": SLUG,
            "mirror": "~/m.git", "branch": f"wt/{tid}", "head": head, "missing": ["studio-state"]})
    return kb.get_task(conn, tid)


@pytest.mark.parametrize("which", ["head", "no-head"])
def test_the_studio_worktree_starts_at_the_handed_over_head(kanban_home, studio_repo, which):
    from hermes_cli import kanban_db_workspace as kbw
    studio, heads, push_branch = studio_repo
    head = heads[0] if which == "head" else None  # the pushed tip is heads[1]
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, push_branch, head)
        assert kwt.materialize_handover(conn, task, str(studio)) is True
        tree, branch = kbw._resolve_worktree_workspace(task)
    assert branch == f"wt/{task.id}" and tree == studio / ".worktrees" / task.id
    assert _git(tree, "rev-parse", "HEAD") == (heads[0] if head else heads[1])
    assert _git(tree, "branch", "--show-current") == branch


def test_a_local_branch_without_the_handed_over_head_fails_the_spawn(kanban_home, studio_repo):
    studio, heads, push_branch = studio_repo
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, push_branch, heads[1])
        _git(studio, "branch", f"wt/{task.id}", "main")  # a stale local branch that lacks the head
        with pytest.raises(RuntimeError, match="does not contain"):
            kwt.materialize_handover(conn, task, str(studio))
    assert _git(studio, "rev-parse", f"wt/{task.id}") != heads[1]


# -- 3. a never-created hand-over target is recovered, not refused as a lost mount ---------------------------------

@pytest.fixture
def mounted_root(kanban_home, monkeypatch):
    """kanban.workspaces_root under a MOUNTED volume, as on the Studio (/Volumes/fleet-scratch/workspaces)."""
    volume = kb.kanban_home() / "fleet-scratch"
    root = volume / "workspaces"
    (root / "default").mkdir(parents=True)
    text = (kanban_home / "config.yaml").read_text() if (kanban_home / "config.yaml").exists() else "kanban:\n"
    (kanban_home / "config.yaml").write_text(
        text + f"  workspaces_root: {root}\n  workspaces_root_require_mount: true\n")
    monkeypatch.setattr("os.path.ismount", lambda p: Path(p) == volume)
    return root


def _strand_like_t_6e7909d1(conn, repo):
    """The pre-fix row: kind scratch, path ``<pool>-from-ace-ai`` that was never created, head null."""
    tid, pool = _ported_worktree_card(conn, repo)
    new = f"{pool}-from-ace-ai"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (new, tid))
        kb._append_event(conn, tid, kwt.HANDOVER_EVENT, {
            "from_host": "ace-ai", "workspace": pool, "path": new, "repo": SLUG, "mirror": "~/m.git",
            "branch": f"wt/{tid}", "head": None, "missing": ["studio-state"]})
    return tid, new


def test_a_never_created_hand_over_target_is_recovered_to_the_declared_worktree(mounted_root, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid, new = _strand_like_t_6e7909d1(conn, repo)
        result = kbd.DispatchResult()
        refused = kb._workspace_admission_refused(conn, tid, result, board="default", dry_run=False)
        kind, path = _card(conn, tid)
        evs = _handovers(conn, tid)
        stranded = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='stranded_by_mount_loss'",
                                (tid,)).fetchone()[0]
    assert refused is False and result.stranded_by_mount_loss == [] and result.workspace_refused == []
    assert (kind, path) == ("worktree", str(repo)) and stranded == 0
    assert evs[-1]["kind"] == "worktree" and evs[-1]["path"] == str(repo)
    assert evs[-1]["recovered"] == f"handover_target_never_created: {new}", evs[-1]
    assert not os.path.lexists(new)


def test_a_never_created_scratch_target_is_created_and_named(mounted_root):
    from tests.hermes_cli.test_kanban_studio_deny_ported_reroute import _ported_card
    with kb.connect_closing() as conn:
        tid, pool = _ported_card(conn)
        new = f"{pool}-from-ace-ai"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (new, tid))
            kb._append_event(conn, tid, kwt.HANDOVER_EVENT, {
                "from_host": "ace-ai", "workspace": pool, "path": new, "repo": SLUG, "mirror": "~/m.git",
                "branch": f"wt/{tid}", "head": None, "missing": ["studio-state"]})
        result = kbd.DispatchResult()
        refused = kb._workspace_admission_refused(conn, tid, result, board="default", dry_run=False)
        evs = _handovers(conn, tid)
    assert refused is False and result.stranded_by_mount_loss == []
    assert os.path.isdir(new) and evs[-1]["recovered"] == f"handover_target_never_created: {new}"


def test_a_missing_path_with_no_hand_over_is_still_stranded(mounted_root):
    """The recovery is for the hand-over's own target only: any other dead path stays fail-closed."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha")
        gone = str(mounted_root / "default" / tid)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (gone, tid))
        result = kbd.DispatchResult()
        assert kb._workspace_admission_refused(conn, tid, result, board="default", dry_run=False) is True
    assert result.stranded_by_mount_loss == [tid]


def test_a_hand_over_target_a_worker_already_ran_in_is_not_recovered(mounted_root, tmp_path):
    """Once a run spawned on the hand-over, a missing path is a real loss: stranded, not re-pointed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid, new = _strand_like_t_6e7909d1(conn, repo)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 1})
        result = kbd.DispatchResult()
        assert kb._workspace_admission_refused(conn, tid, result, board="default", dry_run=False) is True
        assert _card(conn, tid)[1] == new
    assert result.stranded_by_mount_loss == [tid]


def test_dry_run_never_recovers(mounted_root, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid, new = _strand_like_t_6e7909d1(conn, repo)
        kb._workspace_admission_refused(conn, tid, kbd.DispatchResult(), board="default", dry_run=True)
        assert _card(conn, tid) == ("scratch", new)


def test_recover_handover_ignores_a_live_path(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid, new = _strand_like_t_6e7909d1(conn, repo)
        os.makedirs(new)
        assert kwt.recover_handover(conn, kb.get_task(conn, tid)) is None
        assert _card(conn, tid) == ("scratch", new)


def test_admit_spawns_the_handed_over_worktree_card_at_the_clone_head(operator, studio_repo, clean_clone,
                                                                     monkeypatch):
    """End to end on the Studio: hand-over (route) -> override -> admit. The spawn gets the declared repo's
    ``.worktrees/<id>`` on the card's branch at the head the port clone held, kind ``worktree``."""
    from hermes_cli import kanban_studio_allowlist as ksa
    _cfg(operator, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    studio, heads, push_branch = studio_repo
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (True, heads[0]))
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, task.workspace_kind, str(workspace)))
        return 4242
    with kb.connect_closing() as conn:
        tid, _pool = _ported_worktree_card(conn, studio)
        push_branch(f"wt/{tid}")
        assert not _route(conn, tid)[1].startswith("port_stranded")
        ksa.override(conn, tid, "studio-state", "needs sub-box ssh", actor="apollo")
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
        kind, path = _card(conn, tid)
    tree = studio / ".worktrees" / tid
    assert spawned == [(tid, "worktree", str(tree))], res
    assert kind == "worktree"
    assert _git(tree, "rev-parse", "HEAD") == heads[0] and _git(tree, "branch", "--show-current") == f"wt/{tid}"


# -- Prism round 1 on #319 ----------------------------------------------------------------------------------------

def test_no_head_and_an_unreachable_origin_fails_the_spawn(kanban_home, studio_repo):
    """50ba052e20b3: with no recorded head, a failed fetch is not "the branch is absent"; trunk is never used."""
    studio, heads, push_branch = studio_repo
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, push_branch, None)
        _git(studio, "remote", "set-url", "origin", str(studio.parent / "gone.git"))
        with pytest.raises(RuntimeError, match="could not fetch"):
            kwt.materialize_handover(conn, task, str(studio))
    assert subprocess.run(["git", "-C", str(studio), "rev-parse", "-q", "--verify", f"refs/heads/wt/{task.id}"],
                          capture_output=True).returncode != 0


def test_no_head_and_no_branch_on_a_reachable_origin_branches_off_trunk(kanban_home, studio_repo):
    """Control for 50ba052e20b3: an origin that answers "no such branch" still means a fresh branch."""
    studio, heads, _push = studio_repo
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, lambda b: None, None)
        assert kwt.materialize_handover(conn, task, str(studio)) is False


def test_a_scratch_target_that_cannot_be_created_is_not_recovered(mounted_root, monkeypatch):
    """3d337a9df692: recovery that leaves the dir missing returns None and writes nothing (no re-admit loop)."""
    from tests.hermes_cli.test_kanban_studio_deny_ported_reroute import _ported_card
    with kb.connect_closing() as conn:
        tid, pool = _ported_card(conn)
        new = f"{pool}-from-ace-ai"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (new, tid))
            kb._append_event(conn, tid, kwt.HANDOVER_EVENT, {
                "from_host": "ace-ai", "workspace": pool, "path": new, "repo": SLUG, "mirror": "~/m.git",
                "branch": f"wt/{tid}", "head": None, "missing": ["studio-state"]})
        before = len(_handovers(conn, tid))

        def boom(*a, **k):
            raise OSError(13, "denied")
        monkeypatch.setattr(kwt.os, "makedirs", boom)
        assert kwt.recover_handover(conn, kb.get_task(conn, tid)) is None
        result = kbd.DispatchResult()
        assert kb._workspace_admission_refused(conn, tid, result, board="default", dry_run=False) is True
        assert len(_handovers(conn, tid)) == before
    assert result.stranded_by_mount_loss == [tid]


def test_a_pathless_worktree_card_uses_the_board_default_repo(kanban_home, studio_repo):
    """e1c1afcd8647 / 217145c4d8eb: workspace_path NULL -> the board's default_workdir, as at port time."""
    from hermes_cli import kanban_db_workspace as kbw
    studio, heads, push_branch = studio_repo
    kb.write_board_metadata(board=None, default_workdir=str(studio))
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, push_branch, heads[0])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=NULL WHERE id=?", (task.id,))
            kb._append_event(conn, task.id, kwt.HANDOVER_EVENT, {**_handovers(conn, task.id)[-1], "path": None})
        task = kb.get_task(conn, task.id)
        assert kwt.materialize_handover(conn, task, "", board=None) is True
        tree, branch = kbw._resolve_worktree_workspace(task)
    assert _git(tree, "rev-parse", "HEAD") == heads[0] and branch == f"wt/{task.id}"


def test_the_review_lane_starts_a_handed_over_card_at_the_clone_head(kanban_home, studio_repo, monkeypatch):
    """f17fcaeafe5b / 5d4d8720fd73: a card reviewed before any Studio run gets the handed-over head too."""
    from tests.hermes_cli.test_kanban_worker_pool import _tick
    _cfg(kanban_home, True, ["review_lane", "studio-state"])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    studio, heads, push_branch = studio_repo
    with kb.connect_closing() as conn:
        task = _handed_over_task(conn, studio, push_branch, heads[0])
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task.id,))
        conn.commit()
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
    assert task.id in [t for t, _h in spawned], (spawned, res)
    tree = studio / ".worktrees" / task.id
    assert _git(tree, "rev-parse", "HEAD") == heads[0]


# -- Prism round 2 on #319 ----------------------------------------------------------------------------------------

def test_a_scratch_recovery_overtaken_by_a_newer_hand_over_writes_nothing(mounted_root, monkeypatch):
    """3d337a9df692 (r2): a hand-over B that lands while recovery creates A's dir wins; A's document is not
    appended after B, so materialize_handover still reads B."""
    from tests.hermes_cli.test_kanban_studio_deny_ported_reroute import _ported_card
    with kb.connect_closing() as conn:
        tid, pool = _ported_card(conn)
        a, b = f"{pool}-from-ace-ai", f"{pool}-from-ace-media"
        doc = {"from_host": "ace-ai", "workspace": pool, "path": a, "repo": SLUG, "mirror": "~/m.git",
               "branch": f"wt/{tid}", "head": None, "missing": ["studio-state"]}
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (a, tid))
            kb._append_event(conn, tid, kwt.HANDOVER_EVENT, doc)
        task = kb.get_task(conn, tid)
        real = kwt._make_scratch

        def racing(path):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (b, tid))
                kb._append_event(conn, tid, kwt.HANDOVER_EVENT, {**doc, "from_host": "ace-media", "path": b})
            return real(path)
        monkeypatch.setattr(kwt, "_make_scratch", racing)
        assert kwt.recover_handover(conn, task) is None
        evs = _handovers(conn, tid)
        assert evs[-1]["path"] == b and "recovered" not in evs[-1], evs[-1]
        assert _card(conn, tid)[1] == b
