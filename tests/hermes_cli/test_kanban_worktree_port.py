"""Worktree cards on pool hosts via the host's git mirror (t_6d9e7d90)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401

SLUG = "ANG-Ventures/hermes-home"


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def anchor(tmp_path):
    repo = tmp_path / "anchor"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", f"https://github.com/{SLUG}.git", cwd=repo)
    kwt._origin_cache.clear()
    return repo


def _mirrors(home: Path, hosts=("ace-ai",), slug=SLUG):
    fleet = home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        slug: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": list(hosts)}}}))


def _worktree_card(conn, anchor, body=None):
    return kb.create_task(conn, title="wt", assignee="alpha", body=body,
                          workspace_kind="worktree", workspace_path=str(anchor))


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def test_worktree_card_without_a_mirror_row_stays_local(kanban_home, anchor):
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
        assert rules[tid] == "workspace_kind"


def test_worktree_card_with_a_mirror_is_portable_and_clones_on_the_host(
        kanban_home, anchor, monkeypatch):
    _mirrors(kanban_home)
    calls = []

    def fake_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append((host.name, workspace, slug, mirror, branch))

    monkeypatch.setattr(kwt, "clone_on_host", fake_clone)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][0] == SLUG
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        row = _row(conn, tid)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwt.PORTED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert calls and calls[0][0] == "ace-ai" and calls[0][2] == SLUG and calls[0][4] == f"wt/{tid}"
    assert row["workspace_kind"] == "scratch" and row["workspace_path"] == calls[0][1]
    assert row["branch_name"] == f"wt/{tid}"
    assert json.loads(ev[0])["host"] == "ace-ai"
    assert not (anchor / ".worktrees" / tid).exists()  # no Studio worktree made
    # A rerun binds to the host holding the clone (Prism 5487e612d7e8).
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        conn.commit()
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules) == ("pin", "ace-ai")
        assert rules[tid].startswith("bind_host:ace-ai")


def test_worktree_card_pinned_to_a_host_without_the_mirror_stays_local(kanban_home, anchor):
    _mirrors(kanban_home, hosts=("ace-media",))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor, body="host: ace-ai")
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] in ("local", "wait")


def test_a_prior_local_worktree_keeps_the_card_local(kanban_home, anchor):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        (anchor / ".worktrees" / tid).mkdir(parents=True)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
        assert rules[tid] == "workspace_kind"


def test_other_repo_does_not_port(kanban_home, anchor):
    _mirrors(kanban_home, slug="ANG-Ventures/something-else")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "local"


def test_malformed_mirrors_file_ports_nothing(tmp_path):
    (tmp_path / kwt.MIRRORS_FILE).write_text("{not json")
    assert kwt.read_mirrors(tmp_path) == {}
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "repos": {}}))
    assert kwt.read_mirrors(tmp_path) == {}


def test_portable_worktree_kind_needs_hosts():
    pool = kwp.PoolConfig(hosts=("ace-ai",), profiles=("alpha",))
    kw = dict(workspace_has_content=False, assignee="alpha", body=None, pool=pool)
    assert kwp.portable(workspace_kind="worktree", **kw)[0] is False
    assert kwp.portable(workspace_kind="worktree", worktree_hosts=("ace-ai",), **kw)[0] is True
    assert kwp.portable(workspace_kind="dir", worktree_hosts=("ace-ai",), **kw)[0] is False


def test_clone_script_against_a_real_local_origin(tmp_path):
    """The remote sh, run locally: clone with reference, new branch off the
    default; a second run is a no-op; a non-empty foreign dir is refused."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f").write_text("x")
    _git("add", "f", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=origin)
    mirror = tmp_path / "mirror.git"
    _git("clone", "-q", "--mirror", str(origin), str(mirror), cwd=tmp_path)
    ws = tmp_path / "ws" / "t_x"
    script = kwt.clone_script(str(ws), SLUG, str(mirror), "wt/t_x", url=str(origin))
    assert subprocess.run(["sh", "-c", script]).returncode == 0
    out = subprocess.run(["git", "-C", str(ws), "branch", "--show-current"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "wt/t_x" and (ws / "f").read_text() == "x"
    # t_69e47673: the pool workspace is never world-searchable (git clone into the empty dir keeps its mode)
    assert (ws.stat().st_mode & 0o7777) == 0o770, oct(ws.stat().st_mode)
    # --dissociate: nothing borrowed from the mirror survives the clone
    assert not (ws / ".git" / "objects" / "info" / "alternates").exists()
    assert subprocess.run(["sh", "-c", script]).returncode == 0  # idempotent
    other = tmp_path / "ws" / "busy"
    other.mkdir()
    (other / "keep").write_text("1")
    bad = kwt.clone_script(str(other), SLUG, str(mirror), "b", url=str(origin))
    assert subprocess.run(["sh", "-c", bad]).returncode == 3
    assert (other / "keep").read_text() == "1"


@pytest.mark.parametrize("url, slug", [
    ("https://github.com/ANG-Ventures/hermes-home.git", "ANG-Ventures/hermes-home"),
    ("https://github.com/ANG-Ventures/hermes-agent", "ANG-Ventures/hermes-agent"),
    ("git@github.com:ANG-Ventures/hermes-home.git", "ANG-Ventures/hermes-home"),
    ("ssh://git@github.com/acme/repo.git", "acme/repo"),
    ("https://notgithub.com/acme/repo.git", None),           # Prism afc85da13a79
    ("https://evil.example/github.com/acme/repo.git", None),
    ("https://github.com.evil/acme/repo.git", None),
])
def test_origin_slug_matches_github_only(tmp_path, url, slug):
    repo = tmp_path / "r"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", url, cwd=repo)
    kwt._origin_cache.clear()
    assert kwt.origin_slug(repo) == slug


def _origin_with_feature(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    ident = ("-c", "user.email=t@t", "-c", "user.name=t")
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f").write_text("main")
    _git("add", "f", cwd=origin)
    _git(*ident, "commit", "-qm", "m", cwd=origin)
    _git("checkout", "-q", "-b", "feature", cwd=origin)
    (origin / "f").write_text("feature")
    _git(*ident, "commit", "-qam", "f", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    mirror = tmp_path / "mirror.git"
    _git("clone", "-q", "--mirror", str(origin), str(mirror), cwd=tmp_path)
    return origin, mirror


def test_existing_remote_branch_is_the_baseline(tmp_path):
    """Prism 9b397aa5183d: the baseline is the clone's own origin/<branch>."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    assert (ws / "f").read_text() == "feature"


def test_rerun_after_an_interrupted_checkout_finishes_it(tmp_path):
    """Prism f43e48b43fc3: a clone left on main by a failed checkout is
    finished on the retry, never reported done on the wrong branch."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    _git("clone", "-q", str(origin), str(ws), cwd=tmp_path)   # clone ok, checkout "failed"
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    out = subprocess.run(["git", "-C", str(ws), "branch", "--show-current"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "feature" and (ws / "f").read_text() == "feature"


def test_a_checkout_of_another_origin_is_refused(tmp_path):
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    _git("clone", "-q", str(origin), str(ws), cwd=tmp_path)
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url="https://github.com/x/y.git")
    assert subprocess.run(["sh", "-c", sc]).returncode == 3


def _commit(repo, name):
    (repo / name).write_text(name)
    _git("add", name, cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name, cwd=repo)


def test_a_linked_worktree_path_is_not_an_anchor(kanban_home, anchor):
    """Prism 4a24c6d61776: a rerun whose persisted path is the Studio's
    .worktrees/<id> keeps its tree; it is never re-cloned on a pool host."""
    _mirrors(kanban_home)
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
        conn.commit()
        assert kwt.anchor_repo(tid, str(wt), None) is None
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "local"


def test_a_local_branch_ahead_of_origin_keeps_the_card_local(kanban_home, anchor):
    """Prism 044612a3554e: the card's branch has commits only the Studio has."""
    _mirrors(kanban_home)
    _commit(anchor, "base")
    _git("update-ref", "refs/remotes/origin/feature", "HEAD", cwd=anchor)
    _git("checkout", "-q", "-b", "feature", cwd=anchor)
    _commit(anchor, "local-only")
    _git("checkout", "-q", "main", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") is None
    # in step with origin: portable
    _git("update-ref", "refs/remotes/origin/feature", "feature", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") == anchor.resolve()
    # a local branch origin never saw: local
    _git("branch", "only-here", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "only-here") is None
    # no local branch at all: the clone makes it, portable
    assert kwt.anchor_repo("t_x", str(anchor), None, "wt/t_x") == anchor.resolve()
