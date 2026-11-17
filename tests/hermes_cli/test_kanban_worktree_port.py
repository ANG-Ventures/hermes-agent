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

from tests.hermes_cli.test_kanban_worker_pool import _host, _plan, _tick, kanban_home  # noqa: F401

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


def test_worktree_card_without_a_mirror_row_stays_local(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        rules: dict = {}
        route, reason = kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)
        assert route == "local"
        assert reason == "not_portable:workspace_kind:derived_mode_off"
        assert rules[tid] == "workspace_kind"
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == []
        assert res.placement_waits[tid] == reason


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


def test_worktree_card_pinned_to_a_host_without_the_mirror_stays_local(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
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


def test_other_repo_does_not_port(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
    _mirrors(kanban_home, slug="ANG-Ventures/something-else")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "local"


def test_read_mirrors_per_host_override_and_old_shape(tmp_path):
    mbp = "/Users/Shared/git-mirrors/hermes-home-mirror.git"
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        SLUG: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": ["ci-box", "mbp-2026"],
               "mirrors": {"MBP-2026": mbp}},
        "ANG-Ventures/hermes-agent": {"mirror": "/srv/ci/git-mirrors/hermes-mirror.git", "hosts": ["ci-box"]},
        "ANG-Ventures/bad": {"mirror": "/srv/x.git", "hosts": ["ci-box"], "mirrors": {"mbp-2026": "rel/x.git"}},
        "ANG-Ventures/bad2": {"mirror": "/srv/x.git", "hosts": ["ci-box"], "mirrors": ["mbp-2026"]},
    }}))
    got = kwt.read_mirrors(tmp_path)
    assert got[SLUG.lower()] == ("/srv/ci/git-mirrors/hermes-home-mirror.git", ("ci-box", "mbp-2026"),
                                 {"mbp-2026": mbp})
    # Old shape: no override, every host reads ``mirror``.
    assert got["ang-ventures/hermes-agent"] == ("/srv/ci/git-mirrors/hermes-mirror.git", ("ci-box",), {})
    # A malformed override fails the row closed.
    assert "ang-ventures/bad" not in got and "ang-ventures/bad2" not in got
    port = (SLUG, *got[SLUG.lower()])
    assert kwt.mirror_for(port, "mbp-2026") == mbp
    assert kwt.mirror_for(port, "ci-box") == "/srv/ci/git-mirrors/hermes-home-mirror.git"
    assert kwt.mirror_for(port[:3], "mbp-2026") == "/srv/ci/git-mirrors/hermes-home-mirror.git"


def test_ported_clone_uses_the_placed_hosts_mirror(kanban_home, anchor, monkeypatch):
    mbp = "/Users/Shared/git-mirrors/hermes-home-mirror.git"
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        SLUG: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": ["ace-ai"],
               "mirrors": {"ace-ai": mbp}}}}))
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append((host.name, mirror)))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwt.PORTED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert calls == [("ace-ai", mbp)]
    assert json.loads(ev[0])["mirror"] == mbp


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


# ---- derived mode (t_1d2c5939): every repo, every pool host, push-then-port ----

def _origin_with_push(anchor, tmp_path):
    """Local fetch repository; tests can install a distinct pushurl to exercise it."""
    bare = tmp_path / "pushed.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "set-url", "origin", str(bare), cwd=anchor)
    return bare


def _ref(repo, ref):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def test_derived_an_unlisted_repo_ports_to_every_pool_host(kanban_home, anchor, monkeypatch):
    """No mirrors file, a repo no hand list ever named: portable, every pool host, default mirror path."""
    _git("remote", "set-url", "origin", "https://github.com/ANG-Ventures/claude-apx.git", cwd=anchor)
    kwt._origin_cache.clear()
    assert not (kanban_home / "fleet" / kwt.MIRRORS_FILE).exists()
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append((host.name, slug, mirror, branch)))
    plan = _plan(hosts=[_host("ace-ai", slots=2), _host("ace-media", slots=2)])
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), plan, worktree_out=out) == ("portable", None)
        slug, mirror, hosts, per_host, push = out[tid]
        assert slug == "ANG-Ventures/claude-apx" and set(hosts) == {"ace-ai", "ace-media"}
        assert mirror == "~/.hermes/mirrors/ANG-Ventures/claude-apx.git" and push is None
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test")
        row = _row(conn, tid)
    assert len(spawned) == 1 and spawned[0][1] in ("ace-ai", "ace-media")
    assert calls == [(spawned[0][1], "ANG-Ventures/claude-apx",
                      "~/.hermes/mirrors/ANG-Ventures/claude-apx.git", f"wt/{tid}")]
    assert row["workspace_kind"] == "scratch"


def test_derived_schema1_rows_are_mirror_paths_and_hosts_are_ignored(kanban_home, anchor):
    _mirrors(kanban_home, hosts=("ace-media",))  # the old row lists only ace-media
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor, body="host: ace-ai")
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("pin", "ace-ai")
    assert out[tid][1] == "/srv/ci/git-mirrors/hermes-home-mirror.git" and out[tid][2] == ("ace-ai",)


def test_derived_pins_and_per_host_paths(tmp_path):
    """t_20fa16c7 amendment (Ace 12:51): one `pins` list, empty by default; a row names hosts, never a default."""
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2,
        "pins": [{"repo": "acme/Secret", "hosts": ["ACE-AI"], "why": "needs the GPU"},
                 {"repo": "acme/studio", "hosts": ["mac-studio"], "why": "reads the vault"}],
        "mirror_paths": {SLUG: {"*": "/srv/m.git", "mbp-2026": "/Users/Shared/m.git"},
                         "acme/plain": "~/m/plain.git"}}))
    assert kwt.derived_route("ACME/secret", tmp_path, ("ci-box", "ace-ai"))[1] == ("ace-ai",)
    why: list = []
    assert kwt.derived_route("acme/studio", tmp_path, ("ci-box", "ace-ai"), why) is None and why == ["operator_pin"]
    assert kwt.derived_route(SLUG, tmp_path, ("ci-box", "MBP-2026")) == (
        "/srv/m.git", ("ci-box", "mbp-2026"), {"mbp-2026": "/Users/Shared/m.git"})
    assert kwt.derived_route("acme/plain", tmp_path, ("ci-box",))[0] == "~/m/plain.git"
    why = []
    assert kwt.derived_route("acme/x", tmp_path, (), why) is None and why == ["no_pool_host"]


@pytest.mark.parametrize("doc", [
    {"schema": 2, "exclude": [SLUG]},                                     # retired list
    {"schema": 2, "exclude": "acme/x"},
    {"schema": 2, "pins": [{"repo": SLUG, "hosts": ["mac-studio"]}]},   # no why
    {"schema": 2, "pins": [{"repo": SLUG, "hosts": [], "why": "x"}]},   # no hosts
    {"schema": 2, "mirror_paths": {SLUG: "rel/x.git"}},
    {"schema": 3}, "{not json",
    {"schema": 1, "repos": {SLUG: {"mirror": "/srv/m.git", "hosts": ["ace-media"]}}},  # schema-1 hosts
])
def test_a_config_fault_is_ignored_loudly_never_a_studio_pin(tmp_path, doc, caplog):
    """t_20fa16c7: an override file can no longer make a repo Studio-only by accident: every pool host, one log."""
    kwt._fault_logged.clear()
    (tmp_path / kwt.MIRRORS_FILE).write_text(doc if isinstance(doc, str) else json.dumps(doc))
    why: list = []
    with caplog.at_level("ERROR"):
        route = kwt.derived_route(SLUG, tmp_path, ("ci-box", "ace-ai"), why)
        kwt.derived_route(SLUG, tmp_path, ("ci-box", "ace-ai"))
    assert route is not None and route[1] == ("ci-box", "ace-ai") and why == []
    faults = [r for r in caplog.records if "config fault" in r.getMessage()]
    assert len(faults) == 1  # loud, once


def test_retired_exclude_row_no_longer_keeps_the_card_local(kanban_home, anchor):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "exclude": [SLUG]}))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2)) == ("portable", None)


def test_kill_switch_file_or_env_restores_the_hand_list(kanban_home, anchor, monkeypatch):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    assert kwt.derived_enabled(fleet)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "derived": False}))
    assert not kwt.derived_enabled(fleet)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "portable"
    (fleet / kwt.MIRRORS_FILE).unlink()
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")
    assert not kwt.derived_enabled(fleet)


def test_derived_unpushed_commits_are_pushed_then_ported(kanban_home, anchor, tmp_path, monkeypatch):
    """A card branch with commits origin never saw: pushed (lease: must not exist) BEFORE the clone."""
    bare = _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    _commit(anchor, "base")
    order = []
    real_push = kwt.push_card_branch
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=subprocess.run:
                        (order.append("push"), real_push(push))[1])
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None, sha=None:
                        order.append(("clone", host.name, branch)))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("branch", f"wt/{tid}", cwd=anchor)
        _git("checkout", "-q", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "studio-only")
        _git("checkout", "-q", "main", cwd=anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][4] == (str(anchor.resolve()), f"wt/{tid}", "", _ref(anchor, f"wt/{tid}"))
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (tid, "ace-ai") in spawned
    assert order == ["push", ("clone", "ace-ai", f"wt/{tid}")]
    assert _ref(bare, f"refs/heads/wt/{tid}") == _ref(anchor, f"refs/heads/wt/{tid}") != ""


@pytest.mark.parametrize("url", ["git@github.com:NousResearch/hermes-agent.git",
                                 "https://github.com/NousResearch/hermes-agent.git",
                                 "https://gitlab.com/ANG-Ventures/hermes-agent.git"])
def test_push_to_foreign_origin_is_refused_before_any_push(anchor, url, no_ambient_git_config):
    """t_eac2768a: origin is an upstream we do not own: no push is attempted, the refusal names the URL."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", url, cwd=anchor)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if "push" in argv:  # never executed: no network push from a test
            return subprocess.CompletedProcess(argv, 1, "", "runner: push not executed")
        return subprocess.run(argv, **kw)
    with pytest.raises(kwt.ForeignOriginPush) as exc:
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=runner)
    assert url in str(exc.value) and "checkout whose origin is ours" in str(exc.value)
    assert not [c for c in calls if "push" in c]
    assert kb._unusable_workspace_reason(exc.value) == str(exc.value)  # permanent: blocks on failure #1


@pytest.mark.parametrize("url", ["git@github.com:ANG-Ventures/hermes-agent.git",
                                 "https://github.com/kyzcreig/demo.git"])
def test_push_to_own_origin_runs_the_push(anchor, url, no_ambient_git_config):
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", url, cwd=anchor)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if "push" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, **kw)
    kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=runner)
    pushes = [c for c in calls if "push" in c]
    assert len(pushes) == 1 and f"remote.kanban-port-destination.url={url}" in pushes[0]


@pytest.fixture
def no_ambient_git_config(monkeypatch):
    """The push-destination tests read url.*.insteadOf: keep a host's global/system git config out of them
    (ci-box has a global url.https://github.com/.insteadOf git@github.com:)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _no_push_runner(calls):
    def runner(argv, **kw):
        calls.append(argv)
        if "push" in argv:  # never executed: no network push from a test
            return subprocess.CompletedProcess(argv, 1, "", "runner: push not executed")
        return subprocess.run(argv, **kw)
    return runner


def test_push_refused_when_insteadof_rewrites_own_origin_to_a_foreign_one(anchor, no_ambient_git_config):
    """Prism 51498b6e31ba: `remote get-url` applies insteadOf once (alias -> ANG-Ventures), and the push applies
    it again to that URL (ANG-Ventures -> NousResearch). The URL the push will use is the one checked."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "git@github.com:Kyzcreig/hermes-agent.git", cwd=anchor)
    _git("config", "url.git@github.com:ANG-Ventures/.insteadOf", "git@github.com:Kyzcreig/", cwd=anchor)
    _git("config", "url.git@github.com:NousResearch/.insteadOf", "git@github.com:ANG-Ventures/", cwd=anchor)
    got = subprocess.run(["git", "remote", "get-url", "origin"], cwd=anchor, check=True, capture_output=True, text=True)
    assert got.stdout.strip() == "git@github.com:ANG-Ventures/hermes-agent.git"
    calls = []
    with pytest.raises(kwt.ForeignOriginPush) as exc:
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=_no_push_runner(calls))
    assert "git@github.com:NousResearch/hermes-agent.git" in str(exc.value)
    assert not [c for c in calls if "push" in c]


def test_push_refused_when_a_configured_pushurl_of_the_push_remote_is_foreign(anchor, no_ambient_git_config):
    """Prism 51498b6e31ba: pushurl is multivalued, so `-c ...pushurl=` APPENDS to a checkout-configured one and
    the push would go to both. Every effective push URL is checked."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "https://github.com/ANG-Ventures/hermes-agent.git", cwd=anchor)
    _git("config", "remote.kanban-port-destination.pushurl", "https://github.com/NousResearch/hermes-agent.git",
         cwd=anchor)
    calls = []
    with pytest.raises(kwt.ForeignOriginPush) as exc:
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=_no_push_runner(calls))
    assert "https://github.com/NousResearch/hermes-agent.git" in str(exc.value)
    assert not [c for c in calls if "push" in c]


def test_foreign_origin_refusal_redacts_url_credentials(anchor, no_ambient_git_config):
    """Prism fa431e49e08b: the refusal is stored on the card, so URL userinfo never reaches it."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "https://x-access-token:SECRET123@github.com/NousResearch/hermes-agent.git",
         cwd=anchor)
    calls = []
    with pytest.raises(kwt.ForeignOriginPush) as exc:
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=_no_push_runner(calls))
    assert "SECRET123" not in str(exc.value) and "x-access-token" not in str(exc.value)
    assert "https://github.com/NousResearch/hermes-agent.git" in str(exc.value)
    assert not [c for c in calls if "push" in c]


def test_refusal_redacts_credentials_in_the_rewritten_url(anchor, no_ambient_git_config):
    """Prism bdade6e7f897: a credential in the insteadOf-REWRITTEN URL is redacted too, not only the origin's.
    `remote get-url` rewrites once (kz: -> Kyzcreig, owned); the push rewrites that again (-> token URL)."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "kz:hermes-agent.git", cwd=anchor)
    _git("config", "url.git@github.com:Kyzcreig/.insteadOf", "kz:", cwd=anchor)
    _git("config", "url.https://x-access-token:SECRET123@github.com/NousResearch/.insteadOf",
         "git@github.com:Kyzcreig/", cwd=anchor)
    calls = []
    with pytest.raises(kwt.ForeignOriginPush) as exc:
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "HEAD")), runner=_no_push_runner(calls))
    assert "SECRET123" not in str(exc.value) and "x-access-token" not in str(exc.value)
    assert "https://github.com/NousResearch/hermes-agent.git" in str(exc.value)
    assert not [c for c in calls if "push" in c]


def test_derived_unpushed_branch_on_upstream_origin_stays_local_named_without_push(
        kanban_home, anchor, monkeypatch, no_ambient_git_config):
    """The Studio case (t_6dde6d1e): checkout origin = NousResearch, card branch has an unpushed commit. Named at
    routing (t_20fa16c7: origin_not_ours, counted, pages nobody), never a push, never a blocked card."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "git@github.com:NousResearch/hermes-agent.git", cwd=anchor)
    pushes, clones = [], []
    real_push = kwt.push_card_branch

    def spy(argv, **kw):
        if "push" in argv:
            pushes.append(argv)  # recorded, never executed: no network push from a test
            return subprocess.CompletedProcess(argv, 1, "", "spy: push not executed")
        return subprocess.run(argv, **kw)
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=None: real_push(push, runner=spy))
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: clones.append(a))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("branch", f"wt/{tid}", cwd=anchor)
        _git("checkout", "-q", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "studio-only")
        _git("checkout", "-q", "main", cwd=anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2)) == (
            "local", "not_portable:workspace_kind:origin_not_ours")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        row = _row(conn, tid)
    assert not pushes and not clones and not spawned
    assert tid not in res.auto_blocked and row["status"] == "ready"


def test_push_lease_refuses_a_branch_moved_on_origin(anchor, tmp_path):
    bare = _origin_with_push(anchor, tmp_path)
    _commit(anchor, "base")
    _git("push", "-q", "origin", "main:refs/heads/feature", cwd=anchor)  # someone else made it
    theirs = _ref(bare, "refs/heads/feature")
    _git("branch", "feature", cwd=anchor)
    _commit(anchor, "ours")
    _git("branch", "-f", "feature", "HEAD", cwd=anchor)
    # the routing saw no origin ref (""): origin's feature appeared since, so the lease refuses
    with pytest.raises(RuntimeError, match="before the port failed"):
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "feature")))
    assert _ref(bare, "refs/heads/feature") == theirs


def test_derived_dirty_own_tree_stays_local_named(kanban_home, anchor, monkeypatch):
    """With the dirty-tree carry switched off (t_894be273 kill switch), the card's own Studio tree with
    uncommitted work stays local, as before."""
    _commit(anchor, "base")
    monkeypatch.setenv(kwt.DIRTY_CARRY_ENV, "0")
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "pins": []}))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        (wt / "scratch.txt").write_text("uncommitted")
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules) == (
            "local", "not_portable:workspace_kind:unpushed_dirty_tree")
        assert rules[tid] == "workspace_kind"
        # committed (still unpushed): portable, with a plain push
        _git("add", "scratch.txt", cwd=wt)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=wt)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][4][1:] == (f"wt/{tid}", "", _ref(wt, "HEAD"))


# ---- t_20fa16c7: a dirty or prior-run Studio tree is CARRIED, never a Studio pin ----

def _snapshot(tree: Path) -> dict:
    """Every file under the worktree (bytes) plus its git admin state: index, HEAD, the card branch ref."""
    files = {str(p.relative_to(tree)): p.read_bytes() for p in sorted(tree.rglob("*"))
             if p.is_file() and ".git" not in p.relative_to(tree).parts}
    gitdir = Path(subprocess.run(["git", "-C", str(tree), "rev-parse", "--absolute-git-dir"],
                                 check=True, capture_output=True, text=True).stdout.strip())
    admin = {n: (gitdir / n).read_bytes() for n in ("index", "HEAD") if (gitdir / n).exists()}
    refs = subprocess.run(["git", "-C", str(tree), "for-each-ref", "--format=%(refname) %(objectname)"],
                          check=True, capture_output=True, text=True).stdout
    return {"files": files, "admin": admin, "refs": refs}


@pytest.fixture
def studio_card(kanban_home, anchor, tmp_path, monkeypatch):
    """A card whose Studio ``.worktrees/<id>`` was made by a prior run; origin is a local bare repo we own. The
    clone runs for real (its ssh half in a local sh, :func:`_clone_locally`)."""
    bare = _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    _commit(anchor, "base")
    calls: list = []
    _clone_locally(monkeypatch, tmp_path, bare, calls)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
    wt = anchor / ".worktrees" / tid
    _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
    return tid, wt, bare, calls


@pytest.mark.parametrize("persisted", ["anchor", "own_tree"])
@pytest.mark.parametrize("state", ["dirty", "untracked_only", "committed_and_dirty", "off_branch"])
def test_dirty_or_prior_run_studio_tree_ports_and_its_work_reaches_the_clone(studio_card, tmp_path, persisted,
                                                                             state):
    """Acceptance 1+2 (t_20fa16c7, on #219's carry): a prior run's own tree, dirty, holding unpushed commits, or
    off its branch, ports. Its committed work is pushed as the card branch tip; its uncommitted files reach the
    pool clone byte-equal and are pushed nowhere; the Studio tree is byte-unchanged (files, index, HEAD, refs)."""
    tid, wt, bare, calls = studio_card
    (wt / "base").write_text("edited in the Studio tree")
    (wt / "new dir").mkdir()
    (wt / "new dir" / "notes.md").write_text("untracked work")
    if state == "untracked_only":
        _git("checkout", "--", "base", cwd=wt)
    if state == "off_branch":  # detached, then committed on: the card branch lags the tree's HEAD
        _git("checkout", "-q", "--detach", cwd=wt)
    if state in ("committed_and_dirty", "off_branch"):
        _git("stash", "-q", "-u", cwd=wt)
        _commit(wt, "committed-only-here")
        _git("stash", "pop", "-q", cwd=wt)
    head = _ref(wt, "HEAD")
    with kb.connect_closing() as conn:
        if persisted == "own_tree":  # a prior run persisted the concrete path
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
            conn.commit()
        before, studio = _snapshot(wt), _tree_state(wt)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
    port = out[tid]
    assert port[5][:2] == (str(wt), head)  # a carry, pinned to the tree's HEAD
    ws = tmp_path / "pool" / tid
    _key, job, _lock = kbd._pool_clone_job(tid, f"wt/{tid}", _host("ace-ai"), port, ws)
    job()
    assert len(calls) == 1 and _ref(ws, "HEAD") == head
    assert _tree_state(ws) == studio                           # the work reached the clone, byte-equal
    on_origin = _ref(bare, f"refs/heads/wt/{tid}")
    assert on_origin == head  # the card branch tip on origin is the tree's HEAD: its commits, nothing else
    history = subprocess.run(["git", "-C", str(bare), "log", "--all", "-p"], capture_output=True, text=True).stdout
    assert "untracked work" not in history and "edited in the Studio tree" not in history  # nothing uncommitted
    assert _snapshot(wt) == before                             # the Studio tree is left byte-unchanged


def test_a_clean_pushed_prior_run_tree_ports_without_a_carry(studio_card):
    tid, wt, bare, calls = studio_card
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    _git("fetch", "-q", "origin", cwd=wt)
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
    assert out[tid][4] is None and len(out[tid]) == 5  # nothing to push, nothing to carry


def _drop_index(wt: Path) -> None:
    gitdir = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                                 check=True, capture_output=True, text=True).stdout.strip())
    (gitdir / "index").unlink()


def test_an_unfinished_worktree_add_with_no_files_is_not_work_and_ports(studio_card):
    """t_20fa16c7's own first spawn: `git worktree add` timed out before writing anything but ``.git``: no index,
    no files. Nothing was ever worked in it, so it ports and is left as it is."""
    tid, wt, bare, calls = studio_card
    _drop_index(wt)
    for p in wt.iterdir():
        if p.name != ".git":
            p.unlink()
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
    assert len(out[tid]) == 5  # no carry of "deleted" files
    assert (wt / ".git").exists()


@pytest.mark.parametrize("how", ["no_checkout", "index_lost"])
def test_a_tree_with_no_index_that_holds_files_stays_local_named(studio_card, anchor, how):
    """Prism 9afe97134805: a tree with no index cannot be carried (git status reads every file as deleted, an
    untracked file is not seen), so one holding any file besides ``.git`` fails closed, never ports without it."""
    tid, wt, bare, calls = studio_card
    if how == "no_checkout":
        _git("worktree", "remove", "--force", str(wt), cwd=anchor)
        _git("worktree", "add", "-q", "--no-checkout", str(wt), f"wt/{tid}", cwd=anchor)
        (wt / "notes.txt").write_text("work done in a tree git never indexed")
    else:
        _drop_index(wt)
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == (
            "local", "not_portable:workspace_kind:own_tree_not_a_checkout")
    assert tid not in out


def test_a_failed_index_probe_refuses_the_port_named(studio_card, monkeypatch):
    """Prism b7868d8b43cd: a probe that fails (git error, timeout) is not "uninitialized": the port is refused
    with a named reason, never read as a tree nobody worked in."""
    tid, wt, bare, calls = studio_card
    (wt / "notes.txt").write_text("untracked work")
    real = kwt._tree_git

    def failing(tree, runner, *args):
        return None if "--git-path" in args else real(tree, runner, *args)

    monkeypatch.setattr(kwt, "_tree_git", failing)
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == (
            "local", "not_portable:workspace_kind:own_tree_probe_failed")
    assert "own_tree_probe_failed" in kwt.LOCAL_ONLY_REASONS


def test_an_off_branch_tree_that_drops_branch_commits_stays_local_named(studio_card):
    """Putting the card branch at the tree's HEAD would drop the branch's own commits: an enumerated reason."""
    tid, wt, bare, calls = studio_card
    _commit(wt, "on-the-branch-only")
    _git("checkout", "-q", "--detach", "HEAD~1", cwd=wt)
    (wt / "dirty").write_text("x")
    with kb.connect_closing() as conn:
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2)) == (
            "local", "not_portable:workspace_kind:own_tree_off_branch")
    assert "own_tree_off_branch" in kwt.LOCAL_ONLY_REASONS


def test_an_off_branch_carry_stays_portable_after_its_own_prefetch_push(studio_card, clone_prefetch,
                                                                       no_ambient_git_config):
    """Prism 97743f997e44: local and tracked ``wt/<id>`` at B; the own tree detached at descendant L with a
    committed file and an untracked note; card persisted at the anchor. A deferred tick pushes L and clones +
    carries. The next tick, without a fetch (the tracking ref still says B, origin says L), must stay portable and
    collect THAT clone: never route local, never check out B. The Studio's refs are not changed."""
    tid, wt, bare, calls = studio_card
    anchor = wt.parent.parent
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    _git("fetch", "-q", "origin", cwd=anchor)
    b = _ref(anchor, f"refs/heads/wt/{tid}")
    _git("checkout", "-q", "--detach", cwd=wt)
    _commit(wt, "committed-off-branch")
    (wt / "note.txt").write_text("untracked note")
    l_sha = _ref(wt, "HEAD")
    studio_refs = _refs(anchor)
    with kb.connect_closing() as conn:
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[tid].startswith(kwt.CLONE_PENDING), res.placement_waits
        _wait_prefetch_done()
        assert _ref(bare, f"refs/heads/wt/{tid}") == l_sha               # the prefetch pushed L
        assert _ref(anchor, f"refs/remotes/origin/{f'wt/{tid}'}") == b   # the tracking ref still says B
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=1), worktree_out=out) == ("portable", None)
        res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        row = _row(conn, tid)
    assert (tid, "ace-ai") in spawned2, (res2.placement_waits, getattr(res2, "spawn_failed", None))
    assert len(calls) == 1                                               # collected, never cloned again
    ws = Path(row["workspace_path"])
    assert row["workspace_kind"] == "scratch" and _ref(ws, "HEAD") == l_sha   # L, never B
    assert (ws / "note.txt").read_text() == "untracked note"
    assert _refs(anchor) == studio_refs                                  # the Studio's refs are unchanged
    assert _ref(wt, "HEAD") == l_sha


def test_derived_diverged_branch_stays_local_and_ahead_branch_pushes(anchor):
    _commit(anchor, "base")
    base = _ref(anchor, "HEAD")
    _git("branch", "feature", cwd=anchor)
    _git("update-ref", "refs/remotes/origin/feature", "HEAD", cwd=anchor)
    _git("checkout", "-q", "feature", cwd=anchor)
    _commit(anchor, "studio-only")
    _git("checkout", "-q", "main", cwd=anchor)
    # ahead only (fast-forward): pushed with the lease pinned to origin's sha
    assert kwt.assess("t_x", str(anchor), None, "feature", push_ok=True) == (
        anchor.resolve(), (str(anchor.resolve()), "feature", base, _ref(anchor, "feature")))
    # the hand-list semantics are unchanged: local
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") is None
    # origin moved to a commit the Studio lacks: a push would drop it, so local, named
    _commit(anchor, "origin-only")
    _git("update-ref", "refs/remotes/origin/feature", "main", cwd=anchor)
    why: list = []
    assert kwt.assess("t_x", str(anchor), None, "feature", push_ok=True, why=why) == (None, None)
    assert why == ["diverged_from_origin"]


def test_push_uses_assessed_sha_despite_branch_reset_and_ignores_pushurl(anchor, tmp_path):
    bare = _origin_with_push(anchor, tmp_path)
    _commit(anchor, "parent")
    parent = _ref(anchor, "HEAD")
    _commit(anchor, "remote")
    _git("push", "-q", "origin", "main:refs/heads/feature", cwd=anchor)
    remote = _ref(anchor, "HEAD")
    _git("checkout", "-q", "-b", "feature", cwd=anchor)
    _commit(anchor, "assessed")
    assessed = _ref(anchor, "HEAD")
    _, push = kwt.assess("t_x", str(anchor), None, "feature", push_ok=True)
    other = tmp_path / "wrong.git"
    _git("init", "-q", "--bare", str(other), cwd=tmp_path)
    _git("remote", "set-url", "--push", "origin", str(other), cwd=anchor)
    _git("config", f"url.{other}.pushInsteadOf", str(bare), cwd=anchor)
    _git("checkout", "-q", "main", cwd=anchor)
    _git("branch", "-f", "feature", parent, cwd=anchor)
    kwt.push_card_branch(push)
    assert _ref(bare, "refs/heads/feature") == assessed != remote
    assert not _ref(other, "refs/heads/feature")


def test_assessment_checks_immutable_shas_when_branch_moves(anchor):
    _commit(anchor, "base")
    _git("branch", "feature", cwd=anchor)
    _commit(anchor, "remote")
    remote = _ref(anchor, "HEAD")
    _git("update-ref", "refs/remotes/origin/feature", remote, cwd=anchor)
    _git("checkout", "-q", "feature", cwd=anchor)
    _commit(anchor, "divergent")
    divergent = _ref(anchor, "HEAD")
    def racing_runner(argv, **kwargs):
        if "rev-list" in argv:
            _git("update-ref", "refs/heads/feature", remote, cwd=anchor)
        return subprocess.run(argv, **kwargs)
    why = []
    assert kwt.assess("t_x", str(anchor), None, "feature", runner=racing_runner,
                      push_ok=True, why=why) == (None, None)
    assert why == ["diverged_from_origin"]
    assert divergent != remote


def test_clone_script_expands_a_home_relative_mirror(tmp_path):
    origin, mirror = _origin_with_feature(tmp_path)
    home = tmp_path / "home"
    (home / ".hermes" / "mirrors" / "acme").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(origin),
                    str(home / ".hermes" / "mirrors" / "acme" / "r.git")], check=True)
    ws = tmp_path / "ws"
    sc = kwt.clone_script(str(ws), "acme/r", "~/.hermes/mirrors/acme/r.git", "feature", url=str(origin))
    import os
    env = dict(os.environ, HOME=str(home))
    assert subprocess.run(["sh", "-c", sc], env=env).returncode == 0
    assert (ws / "f").read_text() == "feature"
    # absent mirror: --reference-if-able, a plain clone, never a refusal
    ws2 = tmp_path / "ws2"
    sc2 = kwt.clone_script(str(ws2), "acme/r", "~/.hermes/mirrors/acme/none.git", "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc2], env=env).returncode == 0


# -- pool-host clone off the dispatcher tick (t_915b8d2d) ------------------------------------------------------

@pytest.fixture
def clone_prefetch():
    kwt._prefetch.clear()
    yield kwt._prefetch
    for fut, _t0 in list(kwt._prefetch.values()):
        fut.cancel()
    kwt._prefetch.clear()


def _wait_prefetch_done(timeout=10.0):
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if kwt._prefetch and all(f.done() for f, _ in kwt._prefetch.values()):
            return
        _t.sleep(0.02)
    raise AssertionError("background clone never finished")


def test_gateway_tick_does_not_wait_on_a_slow_pool_host_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """A clone that takes as long as it likes never holds up the tick: the card waits clone_pending, its slot
    goes back to the next portable card in the SAME tick, and the clone's card launches on a later tick."""
    import threading
    import time as _t
    _mirrors(kanban_home)
    gate, calls = threading.Event(), []

    def slow_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append((host.name, workspace, branch))
        assert gate.wait(30), "test released the clone"

    monkeypatch.setattr(kwt, "clone_on_host", slow_clone)
    with kb.connect_closing() as conn:
        wt = kb.create_task(conn, title="wt", assignee="alpha", priority=9,
                            workspace_kind="worktree", workspace_path=str(anchor))
        other = kb.create_task(conn, title="plain", assignee="alpha", priority=1)
        t0 = _t.monotonic()
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        wall = _t.monotonic() - t0
        assert wall < 10, f"tick waited on the clone ({wall:.1f}s)"
        assert res.placement_waits[wt] == f"{kwt.CLONE_PENDING}:ace-ai"
        assert (other, "ace-ai") in spawned          # the slot went to the next portable card this tick
        assert all(tid != wt for tid, _ in spawned)
        assert _row(conn, wt)["status"] == "ready"   # never claimed while the clone runs
        assert res.clone_ms < 5000
        gate.set()
        _wait_prefetch_done()
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (other,))
        conn.commit()
        res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        row = _row(conn, wt)
    assert (wt, "ace-ai") in spawned2
    assert len(calls) == 1                           # collected, not cloned a second time
    assert row["workspace_kind"] == "scratch" and row["workspace_path"] == calls[0][1]
    assert calls[0][2] == f"wt/{wt}"
    assert not kwt._prefetch


def test_failed_background_clone_takes_the_existing_failure_path(kanban_home, anchor, monkeypatch,
                                                                 clone_prefetch):
    _mirrors(kanban_home)

    def bad_clone(host, workspace, slug, mirror, branch, runner=None):
        raise RuntimeError(f"worktree clone of {slug} on {host.name} failed rc=128: boom")

    monkeypatch.setattr(kwt, "clone_on_host", bad_clone)
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        plan = _plan(free=1)
        res2, spawned2 = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        row = _row(conn, wt)
        run = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (wt,)).fetchone()
        # The refunded slot is free again: a fresh portable card takes it in the next tick.
        other = kb.create_task(conn, title="plain", assignee="alpha")
        _res3, spawned3 = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test")
    assert spawned2 == [] and wt in res2.spawn_failed
    assert row["status"] in ("ready", "blocked") and row["claim_lock"] is None
    assert run["outcome"] == "spawn_failed" and "boom" in (run["error"] or "")
    assert (other, "ace-ai") in spawned3


def test_cli_tick_keeps_the_synchronous_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """Without defer_pool_clone (CLI, admit, dashboard) the clone runs in the tick as before: a one-shot
    process must not exit with a clone thread still running."""
    _mirrors(kanban_home)
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append(host.name))
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        _res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned and calls == ["ace-ai"] and not kwt._prefetch


# -- Prism 3e2f8506c8ff / d4847ef87c43: the pending clone is keyed on the tip and visible to every dispatcher ---

def test_tip_moved_after_prefetch_never_launches_the_stale_clone(kanban_home, anchor, tmp_path, monkeypatch,
                                                                 clone_prefetch):
    """Prism 3e2f8506c8ff: prefetch at A, commit B, dispatch -> the card launches on B, never on A's clone."""
    _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    _commit(anchor, "base")
    pushes, clones = [], []
    real_push = kwt.push_card_branch
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=subprocess.run:
                        (pushes.append(push[3]), real_push(push))[1])
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None, sha=None:
                        clones.append(sha))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("checkout", "-q", "-b", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "A")
        sha_a = _ref(anchor, f"wt/{tid}")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[tid].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        _git("fetch", "-q", "origin", cwd=anchor)       # the Studio sees its own push
        _commit(anchor, "B")                             # the tip moves before the clone is collected
        sha_b = _ref(anchor, f"wt/{tid}")
        _git("checkout", "-q", "main", cwd=anchor)
        res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        assert spawned2 == [], "launched on the clone of the old tip"
        assert res2.placement_waits[tid].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        _res3, spawned3 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                                defer_pool_clone=True)
    assert (tid, "ace-ai") in spawned3
    assert pushes == [sha_a, sha_b]
    assert clones == [sha_a, sha_b]                      # the launched workspace is pinned to B
    assert not kwt._prefetch


def test_deferred_then_sync_tick_never_starts_a_second_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """Prism d4847ef87c43: a gateway (deferred) tick starts the clone; a CLI/admit (synchronous) tick before it
    finishes must skip the card, never run a second writer into the same workspace."""
    import threading
    _mirrors(kanban_home)
    gate, calls = threading.Event(), []

    def slow_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append(workspace)
        assert gate.wait(30), "test released the clone"

    monkeypatch.setattr(kwt, "clone_on_host", slow_clone)
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test", defer_pool_clone=True)
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
        assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING)
        assert _row(conn, wt)["status"] == "ready"
        assert len(calls) == 1, "a second clone started while the first was running"
        gate.set()
        _wait_prefetch_done()
        _res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned2 and len(calls) == 1   # the sync tick collected the finished clone


def test_a_clone_held_by_another_dispatcher_process_is_skipped_by_every_tick(kanban_home, anchor, monkeypatch,
                                                                            clone_prefetch):
    """The pending marker is a per-workspace lock any dispatcher process sees (the in-process cache is not):
    while another process holds it, deferred and synchronous ticks both skip the card; it dies with that
    process, so a crashed dispatcher never strands the card."""
    import sys
    _mirrors(kanban_home)
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append(ws))
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        out: dict = {}
        kbd._route_row(conn, _row(conn, wt), _plan(free=1), worktree_out=out)
        _key, _job, lock = kbd._pool_clone_job(wt, None, _host(), out[wt], kb.workspaces_root() / wt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
                                   "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
                                   str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            for defer in (True, False):
                res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                                     defer_pool_clone=defer)
                assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING), defer
            assert calls == [] and not kwt._prefetch
        finally:
            holder.kill()
            holder.wait()
        _res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned and len(calls) == 1


def test_clone_script_pins_a_reused_workspace_to_the_tip(tmp_path):
    """A workspace an earlier (stale) clone left at A is moved to the pushed tip B, and proved there (rc 5)."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    sha_a = _ref(origin, "feature")
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha=sha_a)
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    _git("checkout", "-q", "feature", cwd=origin)
    _commit(origin, "b")
    _git("checkout", "-q", "main", cwd=origin)
    sha_b = _ref(origin, "feature")
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha=sha_b)
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    assert _ref(ws, "HEAD") == sha_b
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha="0" * 40)
    assert subprocess.run(["sh", "-c", sc], capture_output=True).returncode == 5


# ---- dirty own tree carried into the pool clone, never pushed (t_894be273) ----

def _local_ssh(calls):
    """A runner that runs the remote half of an ``ssh ... <target> <cmd>`` argv in a local ``sh``."""
    def run(argv, **kw):
        if argv and argv[0] == "ssh":
            calls.append(argv[-1])
            argv = ["sh", "-c", argv[-1]]
        return subprocess.run(argv, **kw)
    return run


def _clone_locally(monkeypatch, tmp_path, bare, calls):
    """The real clone_on_host (clone + carry in one ssh call), with the clone pointed at the local origin and the
    ssh half run in a local sh."""
    real_script = kwt.clone_script
    monkeypatch.setattr(kwt, "clone_script", lambda w, slug, mirror, branch, url=None, sha=None, **kw: real_script(
        w, slug, str(tmp_path / "no-mirror.git"), branch, url=str(bare), sha=sha, **kw))
    real = kwt.clone_on_host
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, w, slug, mirror, branch, runner=None, sha=None,
                        carry=None: real(host, w, slug, mirror, branch, runner=_local_ssh(calls), sha=sha,
                                         carry=carry))


def _carry(ws, wt, head, calls, bare, tmp_path, runner=None):
    """Clone (or re-run on a reused clone) at ``head`` and carry ``wt``'s dirty state, one local 'ssh' call."""
    real_script = kwt.clone_script
    sc = lambda w, slug, mirror, branch, url=None, sha=None, **kw: real_script(  # noqa: E731
        w, slug, str(tmp_path / "no-mirror.git"), branch, url=str(bare), sha=sha, **kw)
    orig, kwt.clone_script = kwt.clone_script, sc
    try:
        census = kwt.dirty_census(wt)
        kwt.clone_on_host(_host("ace-ai"), str(ws), SLUG, "unused", _branch(wt),
                          runner=runner or _local_ssh(calls), sha=head,
                          carry=(str(wt), head, kwt.carry_digest(wt, census)))
    finally:
        kwt.clone_script = orig


def _branch(tree):
    return subprocess.run(["git", "-C", str(tree), "branch", "--show-current"], capture_output=True,
                          text=True).stdout.strip()


def _dirty_tree(conn, anchor, tmp_path, monkeypatch):
    bare = _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    _commit(anchor, "base")
    _commit(anchor, "doomed")
    tid = _worktree_card(conn, anchor)
    wt = anchor / ".worktrees" / tid
    _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    _git("fetch", "-q", "origin", cwd=anchor)
    (wt / "scratch.txt").write_text("uncommitted")
    (wt / "base").write_text("edited")
    (wt / "doomed").unlink()
    return bare, tid, wt


def _port_of(tid, wt, fleet):
    why: list = []
    return kwt.port_for(tid, str(wt), None, fleet, allow_studio_tree=True, pool_hosts=("ace-ai",), why=why), why


def _tree_state(tree):
    """Every non-ignored path with its bytes, as the working tree holds it."""
    out = subprocess.run(["git", "-C", str(tree), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True).stdout
    names = [f.decode() for f in out.split(b"\0") if f]
    return {n: (tree / n).read_bytes() for n in names if (tree / n).is_file()}


def _refs(bare):
    return subprocess.run(["git", "-C", str(bare), "for-each-ref"], capture_output=True, text=True).stdout


def test_dirty_own_tree_is_carried_into_the_clone_and_nothing_is_pushed(kanban_home, anchor, tmp_path,
                                                                        monkeypatch):
    """Red-first (Apollo 18:0x F1): a dirty own tree used to stay local as unpushed_dirty_tree. Now port_for
    returns a route, the clone job clones the card branch pinned to the tree's HEAD, and the changed files and the
    deletion are copied straight into that clone over ssh. Nothing reaches the remote: its refs are unchanged."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    head, refs_before, studio = _ref(wt, "HEAD"), _refs(bare), _tree_state(wt)
    port, why = _port_of(tid, wt, kanban_home / "fleet")
    assert port is not None and why == [], why
    assert port[4] is None and port[5][:2] == (str(wt), head) and len(port[5][2]) == 64
    ws = tmp_path / "pool" / tid
    calls: list = []
    _clone_locally(monkeypatch, tmp_path, bare, calls)
    _key, job, _lock = kbd._pool_clone_job(tid, f"wt/{tid}", _host("ace-ai"), port, ws)
    job()
    assert len(calls) == 1                                  # clone + carry: ONE ssh call (one admitted site)
    assert _ref(ws, "HEAD") == head                         # the clone is at the Studio head, no extra commit
    assert _tree_state(ws) == studio                        # byte-equal, deletion applied
    assert not (ws / "doomed").exists() and (ws / "scratch.txt").read_text() == "uncommitted"
    assert _refs(bare) == refs_before                       # nothing pushed: no new ref, card branch unmoved
    assert _tree_state(wt) == studio                        # the Studio tree is untouched


def test_carry_runs_on_every_port_and_a_reused_clone_ends_byte_equal(kanban_home, anchor, tmp_path, monkeypatch):
    """Apollo F4 / Prism 6d57e664864b: a reused clone already dirty from an earlier carry is brought to the
    Studio tree's CURRENT state, including a file the Studio has since removed."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    head = _ref(wt, "HEAD")
    ws = tmp_path / "pool" / tid
    calls: list = []
    _carry(ws, wt, head, calls, bare, tmp_path)
    assert _tree_state(ws) == _tree_state(wt)
    # a later round of Studio work: one untracked file gone, a new one, the edit and the deletion reverted
    (wt / "scratch.txt").unlink()
    (wt / "second.txt").write_text("second round")
    _git("checkout", "--", "base", "doomed", cwd=wt)
    _carry(ws, wt, head, calls, bare, tmp_path)                       # the reused clone, still dirty
    assert _tree_state(ws) == _tree_state(wt)
    assert not (ws / "scratch.txt").exists() and (ws / "second.txt").read_text() == "second round"
    assert (ws / "doomed").read_text() == "doomed" and (ws / "base").read_text() == "base"
    assert len(calls) == 2


def test_exact_and_carried_combine_on_a_reused_clone_behind_the_tip(kanban_home, anchor, tmp_path, monkeypatch):
    """Merge of #219 (carried) and #216 (exact), operator F1 22:1x: a reused clone that sits at the branch tip,
    with a carry attached and an exact sha BEHIND that tip, ends on the exact commit with the carried files in
    place. Without ``exact`` the fast-forward path leaves HEAD on the tip and the call fails rc 5."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    head = _ref(wt, "HEAD")
    # origin's card branch moves past the Studio head (another pusher)
    other = tmp_path / "other"
    _git("clone", "-q", "-b", f"wt/{tid}", str(bare), str(other), cwd=tmp_path)
    _commit(other, "ahead")
    _git("push", "-q", "origin", f"wt/{tid}", cwd=other)
    tip = _ref(other, "HEAD")
    assert tip != head
    ws = tmp_path / "pool" / tid
    real_script = kwt.clone_script
    monkeypatch.setattr(kwt, "clone_script", lambda w, slug, mirror, branch, url=None, sha=None, **kw: real_script(
        w, slug, str(tmp_path / "no-mirror.git"), branch, url=str(bare), sha=sha, **kw))
    # the reused clone: an earlier run left it at origin's tip
    assert subprocess.run(["sh", "-c", kwt.clone_script(str(ws), SLUG, "m", f"wt/{tid}", sha=tip)]).returncode == 0
    assert _ref(ws, "HEAD") == tip
    calls: list = []
    carry = (str(wt), head, kwt.carry_digest(wt, kwt.dirty_census(wt)))
    with pytest.raises(RuntimeError, match="rc=5"):
        kwt.clone_on_host(_host("ace-ai"), str(ws), SLUG, "m", f"wt/{tid}", runner=_local_ssh(calls), sha=head,
                          carry=carry)
    kwt.clone_on_host(_host("ace-ai"), str(ws), SLUG, "m", f"wt/{tid}", runner=_local_ssh(calls), sha=head,
                      carry=carry, exact=True)
    assert _ref(ws, "HEAD") == head                          # the exact commit, behind origin's tip
    assert _tree_state(ws) == _tree_state(wt)                # the carried files in place, deletion applied
    assert (ws / "scratch.txt").read_text() == "uncommitted" and not (ws / "doomed").exists()
    assert not (ws / "ahead").exists()


def test_dirty_submodule_or_nested_repo_keeps_the_card_local(kanban_home, anchor, tmp_path, monkeypatch):
    """Apollo F3 / Prism 136fb02e9f5e, 571037abe277: status cannot see inside an embedded repository or a
    submodule, so a tree holding either stays local with a named reason."""
    import shutil
    with kb.connect_closing() as conn:
        _bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    nested = wt / "vendor" / "inner"
    nested.mkdir(parents=True)
    _git("init", "-q", cwd=nested)
    (nested / "work.txt").write_text("inner work")
    assert _port_of(tid, wt, kanban_home / "fleet") == (None, [kwt.WHY_NESTED_REPO])
    # a registered submodule with its own uncommitted work
    shutil.rmtree(wt / "vendor")
    sub_origin = tmp_path / "sub"
    sub_origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=sub_origin)
    _commit(sub_origin, "s")
    _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "sub", cwd=wt)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub", cwd=wt)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    _git("fetch", "-q", "origin", cwd=anchor)
    (wt / "sub" / "s").write_text("modified inside the submodule")
    assert kwt.dirty_census(wt) == kwt.WHY_NESTED_REPO
    assert _port_of(tid, wt, kanban_home / "fleet") == (None, [kwt.WHY_NESTED_REPO])


def test_carry_refusals_and_kill_switch_keep_the_card_local(kanban_home, anchor, tmp_path, monkeypatch):
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    fleet = kanban_home / "fleet"
    # kill switch: env, then the overrides file
    monkeypatch.setenv(kwt.DIRTY_CARRY_ENV, "0")
    assert _port_of(tid, wt, fleet) == (None, [kwt.WHY_DIRTY])
    monkeypatch.delenv(kwt.DIRTY_CARRY_ENV)
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "dirty_carry": False}))
    assert _port_of(tid, wt, fleet) == (None, [kwt.WHY_DIRTY])
    (fleet / kwt.MIRRORS_FILE).unlink()
    # size cap (the survivor-capture cap)
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    (wt / "big.bin").write_bytes(b"x" * 4096)
    assert _port_of(tid, wt, fleet) == (None, [kwt.WHY_CARRY_SIZE])
    (wt / "big.bin").unlink()
    # origin's card branch moved past the Studio head from elsewhere (stale tracking ref): local, no clone
    other = tmp_path / "elsewhere"
    _git("clone", "-q", "-b", f"wt/{tid}", str(bare), str(other), cwd=tmp_path)
    _commit(other, "operator-fold")
    _git("push", "-q", "origin", f"wt/{tid}", cwd=other)
    assert _port_of(tid, wt, fleet) == (None, [kwt.WHY_BASE_MOVED])
    # the carry itself refuses a tree that moved off the routed HEAD, before anything is sent
    calls: list = []
    with pytest.raises(RuntimeError, match="HEAD moved"):
        kwt.clone_on_host(_host("ace-ai"), str(tmp_path / "x"), SLUG, "m", f"wt/{tid}", runner=_local_ssh(calls),
                          sha="0" * 40, carry=(str(wt), "0" * 40, "d"))
    assert calls == []


def test_type_change_dir_to_file_and_file_to_dir_ends_byte_equal(kanban_home, anchor, tmp_path, monkeypatch):
    """Apollo 19:13: a tracked directory replaced by a file of the same name, and a tracked file replaced by a
    directory. After the reset, rm -f leaves the old directory behind; the carry removes the emptied parents, so
    tar -xf lands the file and the clone ends byte-equal (both ways)."""
    import shutil
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    (wt / "pkg" / "sub").mkdir(parents=True)
    (wt / "pkg" / "sub" / "a.txt").write_text("a")
    (wt / "pkg" / "b.txt").write_text("b")
    _git("add", "pkg", cwd=wt)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pkg", cwd=wt)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    head = _ref(wt, "HEAD")
    shutil.rmtree(wt / "pkg")
    (wt / "pkg").write_text("pkg is a file now")                     # dir -> file
    (wt / "base").unlink()
    (wt / "base").mkdir()
    (wt / "base" / "inner.txt").write_text("base is a dir now")      # file -> dir
    census = kwt.dirty_census(wt)
    assert not isinstance(census, str), census
    ws = tmp_path / "pool" / tid
    calls: list = []
    _carry(ws, wt, head, calls, bare, tmp_path)
    assert (ws / "pkg").is_file() and (ws / "pkg").read_text() == "pkg is a file now"
    assert (ws / "base" / "inner.txt").read_text() == "base is a dir now"
    assert _tree_state(ws) == _tree_state(wt)
    # and the reverse on the same reused clone: the Studio puts both back as HEAD has them
    (wt / "pkg").unlink()
    shutil.rmtree(wt / "base")
    _git("checkout", "--", "pkg", "base", cwd=wt)
    _carry(ws, wt, head, calls, bare, tmp_path)
    assert (ws / "pkg" / "sub" / "a.txt").read_text() == "a" and (ws / "base").read_text() == "base"
    assert _tree_state(ws) == _tree_state(wt)


def test_a_dir_replaced_by_a_symlink_out_of_the_tree_is_refused(kanban_home, anchor, tmp_path, monkeypatch):
    """Prism 9e03f1e94c2d red-first: a tracked dir replaced by a symlink to a directory OUTSIDE the tree holding a
    same-named file. git reports the old child as deleted; lstat through the linked parent found the outside
    file and the tar carried it. Now the card stays local with a named reason, and no tar ever holds it."""
    import tarfile
    import io
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    (wt / "conf").mkdir()
    (wt / "conf" / "settings").write_text("tracked")
    _git("add", "conf", cwd=wt)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "conf", cwd=wt)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    _git("fetch", "-q", "origin", cwd=anchor)
    head = _ref(wt, "HEAD")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings").write_text("OUTSIDE-SECRET")
    import shutil
    shutil.rmtree(wt / "conf")
    (wt / "conf").symlink_to(outside)
    assert kwt.dirty_census(wt) == kwt.WHY_SYMLINK_PARENT
    assert _port_of(tid, wt, kanban_home / "fleet") == (None, [kwt.WHY_SYMLINK_PARENT])
    calls: list = []
    with pytest.raises(RuntimeError, match=kwt.WHY_SYMLINK_PARENT):
        kwt.clone_on_host(_host("ace-ai"), str(tmp_path / "ws"), SLUG, "m", f"wt/{tid}",
                          runner=_local_ssh(calls), sha=head, carry=(str(wt), head, "d"))
    assert calls == []
    # the tar builder itself never follows a symlinked parent either
    with pytest.raises(OSError):
        kwt._carry_tar(wt, ("conf/settings",))
    with tarfile.open(fileobj=io.BytesIO(kwt._carry_tar(wt, ("scratch.txt",)))) as tar:
        assert all(b"OUTSIDE-SECRET" not in tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())


def test_snapshot_race_a_commit_while_the_tar_is_built_raises(kanban_home, anchor, tmp_path, monkeypatch):
    """Prism d9d8d2d38b45 (snapshot race): HEAD is read again after the tar is built. A commit in between (the
    census would now be empty) raises instead of sending an empty carry against a clone pinned to the old HEAD.
    The same holds for a carried file edited while the tar is built."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    head = _ref(wt, "HEAD")
    real_tar = kwt._carry_tar

    def tar_then_commit(tree, copy):
        out = real_tar(tree, copy)
        _git("add", "-A", cwd=wt)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "raced", cwd=wt)
        return out

    calls: list = []
    monkeypatch.setattr(kwt, "_carry_tar", tar_then_commit)
    with pytest.raises(RuntimeError, match="HEAD moved"):
        kwt.clone_on_host(_host("ace-ai"), str(tmp_path / "ws"), SLUG, "m", f"wt/{tid}",
                          runner=_local_ssh(calls), sha=head, carry=(str(wt), head, "d"))
    assert calls == []
    _git("reset", "-q", "--soft", head, cwd=wt)
    _git("reset", "-q", cwd=wt)

    def tar_then_edit(tree, copy):
        out = real_tar(tree, copy)
        (wt / "scratch.txt").write_text("edited during the build, a different size")
        return out

    monkeypatch.setattr(kwt, "_carry_tar", tar_then_edit)
    with pytest.raises(RuntimeError, match="changed while the carry was built"):
        kwt.clone_on_host(_host("ace-ai"), str(tmp_path / "ws"), SLUG, "m", f"wt/{tid}",
                          runner=_local_ssh(calls), sha=head, carry=(str(wt), head, "d"))
    assert calls == []


def test_prefetched_clone_is_not_collected_after_the_dirty_files_change(kanban_home, anchor, tmp_path,
                                                                        monkeypatch):
    """Prism b4ca10cadc5b: the census digest is in the clone key. Prefetch with content A, change the file to B
    without committing, dispatch: the A clone is a different key, never collected, and the clone job that runs
    carries B."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    fleet = kanban_home / "fleet"
    ws = tmp_path / "pool" / tid
    calls: list = []
    _clone_locally(monkeypatch, tmp_path, bare, calls)
    port_a, why = _port_of(tid, wt, fleet)
    assert port_a is not None, why
    key_a, job_a, _lock = kbd._pool_clone_job(tid, f"wt/{tid}", _host("ace-ai"), port_a, ws)
    job_a()                                                           # the prefetch, content A
    assert (ws / "scratch.txt").read_text() == "uncommitted"
    (wt / "scratch.txt").write_text("content B, not committed")
    port_b, why = _port_of(tid, wt, fleet)
    key_b, job_b, _lock = kbd._pool_clone_job(tid, f"wt/{tid}", _host("ace-ai"), port_b, ws)
    assert key_a != key_b and key_a[:7] == key_b[:7]                  # same clone, different carry digest
    job_b()
    assert (ws / "scratch.txt").read_text() == "content B, not committed"
    assert _tree_state(ws) == _tree_state(wt)


def test_a_carried_file_head_ignores_does_not_survive_the_next_carry(kanban_home, anchor, tmp_path, monkeypatch):
    """Prism 23f7819e68e3: carry 1 brings an edited .gitignore that un-ignores secret.txt and secret.txt itself.
    Carry 2 has neither. The reset puts HEAD's .gitignore back, which ignores secret.txt, so clean -fd would keep
    it and a later carried .gitignore would un-ignore it again. The carry manifest removes exactly what carry 1
    wrote; a worker's own ignored build output (never carried) is kept."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    (wt / ".gitignore").write_text("secret.txt\nbuild/\n")
    _git("add", ".gitignore", cwd=wt)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ignore", cwd=wt)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    head = _ref(wt, "HEAD")
    ws = tmp_path / "pool" / tid
    calls: list = []
    (wt / ".gitignore").write_text("build/\n")                       # un-ignores secret.txt
    (wt / "secret.txt").write_text("carried once")
    _carry(ws, wt, head, calls, bare, tmp_path)
    assert (ws / "secret.txt").read_text() == "carried once"
    (ws / "build").mkdir()
    (ws / "build" / "out.o").write_text("worker build output")       # ignored in every version
    # round 2: the Studio reverted .gitignore and dropped secret.txt
    _git("checkout", "--", ".gitignore", cwd=wt)
    (wt / "secret.txt").unlink()
    _carry(ws, wt, head, calls, bare, tmp_path)
    assert not (ws / "secret.txt").exists()
    assert (ws / "build" / "out.o").read_text() == "worker build output"
    assert _tree_state(ws) == _tree_state(wt)


def test_a_reused_clone_is_cleaned_before_the_fast_forward_to_a_new_head(kanban_home, anchor, tmp_path,
                                                                          monkeypatch):
    """Apollo 20:4x F1: carry 1 at H1 leaves the clone dirty. The Studio then commits the carried files (H2) and
    edits one again. The reused clone's fast-forward H1 -> H2 touches those same files, so git refused it over the
    first carry's local changes (rc 5 on every retry). The previous carry is now undone BEFORE the fast-forward;
    the clone ends at H2, byte-equal to the Studio tree."""
    with kb.connect_closing() as conn:
        bare, tid, wt = _dirty_tree(conn, anchor, tmp_path, monkeypatch)
    h1 = _ref(wt, "HEAD")
    ws = tmp_path / "pool" / tid
    calls: list = []
    _carry(ws, wt, h1, calls, bare, tmp_path)
    assert (ws / "base").read_text() == "edited" and (ws / "scratch.txt").exists()
    _git("add", "-A", cwd=wt)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "carried work committed", cwd=wt)
    _git("push", "-q", "origin", f"wt/{tid}", cwd=wt)
    h2 = _ref(wt, "HEAD")
    (wt / "base").write_text("edited again after H2")
    _carry(ws, wt, h2, calls, bare, tmp_path)
    assert _ref(ws, "HEAD") == h2
    assert _tree_state(ws) == _tree_state(wt)
    assert (ws / "base").read_text() == "edited again after H2" and not (ws / "doomed").exists()


# ---- t_fbe8a067: an operator pin is a hard host set; a pinned card waits, never falls back to the Studio ----

def _pin(home: Path, hosts, slug=SLUG):
    fleet = home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "pins": [
        {"repo": slug, "hosts": list(hosts), "why": "t_fbe8a067 test"}]}))


def _pinned_plan():
    """ace-ai full, ci-box free, the Studio with allowance and the pool-refused last resort ON (default_remote)."""
    p = _plan(hosts=[_host("ace-ai", slots=0), _host("ci-box", slots=2)], band="admitting")
    p.default_remote = True
    return p


def test_a_pinned_card_whose_pin_host_is_full_waits_named_never_on_the_studio(kanban_home, anchor, monkeypatch):
    """Prism 7a74ce926bd4 (finding input): pins [{repo, hosts:[ace-ai]}], an otherwise portable worktree card,
    ace-ai at capacity, a free Studio allowance. It waits, named; never the Studio, never ci-box."""
    _pin(kanban_home, ["ace-ai"])
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _pinned_plan(), worktree_out=out) == ("portable", None)
        assert out[tid][2] == ("ace-ai",)
        res, spawned = _tick(conn, spillover=_pinned_plan(), spawn_limit=4)
    assert spawned == [], spawned
    assert res.placement_waits[tid].startswith("worktree_pin (ace-ai"), res.placement_waits[tid]


def test_a_pinned_card_whose_pin_names_no_pool_host_waits_named(kanban_home, anchor, monkeypatch):
    """Second case: the pin names a host outside the pool. It waits, named (was a local route, then the Studio)."""
    _pin(kanban_home, ["nas-fleet"])
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        rules: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _pinned_plan(), rules_out=rules, studio_on=False)
        assert route == ("wait", "worktree_pin (nas-fleet: no pool host in the pin)"), route
        assert rules[tid] == "workspace_kind"
        res, spawned = _tick(conn, spillover=_pinned_plan(), spawn_limit=4)
    assert spawned == [] and res.placement_waits[tid].startswith("worktree_pin (nas-fleet")


def test_a_pin_naming_the_studio_still_runs_on_the_studio(kanban_home, anchor):
    """The Studio is allowed only when the pin names it."""
    _pin(kanban_home, ["mac-studio"])
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=_pinned_plan(), spawn_limit=4)
    assert spawned == [(tid, None)], spawned


def test_a_pin_naming_the_studio_and_a_full_pool_host_keeps_the_studio_last_resort(kanban_home, anchor, monkeypatch):
    _pin(kanban_home, ["ace-ai", "mac-studio"])
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=_pinned_plan(), spawn_limit=4)
    assert spawned == [(tid, None)], spawned


def test_operator_pin_naming_the_studio_is_structural_under_studio_capability_only(kanban_home, anchor):
    """Prism 8dacc17e0687 (finding input): studio_on=True, pins [{repo, hosts:[mac-studio]}], pool (ci-box,),
    a clean worktree card with no requires: line -> local, not wait."""
    _pin(kanban_home, ["mac-studio"])
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        studio: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _plan(hosts=[_host("ci-box")]), studio_on=True,
                               studio_out=studio)
    assert route == ("local", "not_portable:workspace_kind:operator_pin"), route
    assert studio[tid]["capability"] == "workspace_kind:operator_pin" and studio[tid]["kind"] == "structural"


def test_origin_not_ours_is_structural_under_studio_capability_only(kanban_home, anchor, no_ambient_git_config):
    """Prism 8dacc17e0687: origin_not_ours runs on the Studio, where the unpushed work is; never a wait."""
    _commit(anchor, "base")
    _git("remote", "set-url", "origin", "git@github.com:NousResearch/hermes-agent.git", cwd=anchor)
    kwt._origin_cache.clear()
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("branch", f"wt/{tid}", cwd=anchor)
        _git("checkout", "-q", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "studio-only")
        _git("checkout", "-q", "main", cwd=anchor)
        studio: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), studio_on=True, studio_out=studio)
    assert route == ("local", "not_portable:workspace_kind:origin_not_ours"), route
    assert studio[tid]["kind"] == "structural"


def test_wt_structural_lists_no_retired_reason():
    """Prism 8dacc17e0687: every structural name is a reason the port can still give."""
    assert kbd._WT_STRUCTURAL <= set(kwt.LOCAL_ONLY_REASONS)
    assert {"operator_pin", "origin_not_ours"} <= kbd._WT_STRUCTURAL


def test_an_uninitialized_own_tree_whose_head_is_ahead_of_the_card_branch_stays_local(studio_card, anchor):
    """Prism 34f9c3bc9c4c (finding input): `git worktree add --no-checkout --detach <path> B`, B ahead of the card
    branch A: no index, nothing but `.git`. Its HEAD is read before the tree is dropped: own_tree_off_branch."""
    tid, wt, bare, calls = studio_card
    _git("checkout", "-q", "--detach", cwd=wt)
    _commit(wt, "b-only")
    b = _ref(wt, "HEAD")
    assert b != _ref(anchor, f"refs/heads/wt/{tid}")
    _git("worktree", "remove", "--force", str(wt), cwd=anchor)
    _git("worktree", "add", "-q", "--no-checkout", "--detach", str(wt), b, cwd=anchor)
    assert [p.name for p in wt.iterdir()] == [".git"] and _ref(wt, "HEAD") == b
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == (
            "local", "not_portable:workspace_kind:own_tree_off_branch")
    assert tid not in out and not calls


def test_an_uninitialized_own_tree_at_an_ancestor_of_the_card_branch_still_ports(studio_card, anchor):
    """The same no-index tree at the card branch tip's parent holds nothing the branch lacks: ports as before."""
    tid, wt, bare, calls = studio_card
    base = _ref(anchor, f"refs/heads/wt/{tid}")
    _commit(wt, "on-the-branch")
    _git("worktree", "remove", "--force", str(wt), cwd=anchor)
    _git("worktree", "add", "-q", "--no-checkout", "--detach", str(wt), base, cwd=anchor)
    with kb.connect_closing() as conn:
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out)[0] == "portable"


def test_a_pin_survives_an_assessment_early_return(studio_card, kanban_home, anchor):
    """Prism 345896e571e6 (finding input): pin [ace-ai]; the card's own tree made with `worktree add --no-checkout
    --detach` at a commit ahead of the card branch, so the assessment returns early (own_tree_off_branch) before
    the repo is ported. studio_on False: the pin is still read from the workspace and the card waits, not local."""
    tid, wt, bare, calls = studio_card
    _pin(kanban_home, ["ace-ai"])
    _git("checkout", "-q", "--detach", cwd=wt)
    _commit(wt, "b-only")
    b = _ref(wt, "HEAD")
    _git("worktree", "remove", "--force", str(wt), cwd=anchor)
    _git("worktree", "add", "-q", "--no-checkout", "--detach", str(wt), b, cwd=anchor)
    with kb.connect_closing() as conn:
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), studio_on=False)
    assert route[0] == "wait", route
    assert route[1].startswith("worktree_pin (ace-ai: ") and "own_tree_off_branch" in route[1], route
    assert not calls


def test_a_pinned_card_on_a_tick_with_no_pool_plan_waits_never_on_the_studio(kanban_home, anchor):
    """Prism 1a0b618f832f / a427dd03f8a6 (finding input): pin [ace-ai], a clean worktree card, a dispatch tick with
    no pool plan. The card does not spawn on the Studio: it waits, worktree_pin, no plan."""
    _pin(kanban_home, ["ace-ai"])
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert spawned == [], spawned
    assert res.placement_waits.get(tid, "").startswith("worktree_pin (ace-ai: no pool plan"), res.placement_waits


def test_an_unpinned_worktree_card_on_a_tick_with_no_pool_plan_still_runs_locally(kanban_home, anchor):
    """No pin row (today's default): no change, the card runs where it did."""
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert spawned == [(tid, None)], spawned


def _own_tree_with_local_url_rewrite(conn, anchor):
    """The card's own clean linked ``.worktrees/<id>``, persisted, whose worktree-local config rewrites origin away
    from GitHub (``extensions.worktreeConfig``): its own origin reads as no GitHub repo, the anchor's as SLUG."""
    _commit(anchor, "base")
    tid = _worktree_card(conn, anchor)
    wt = anchor / ".worktrees" / tid
    _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
    _git("config", "extensions.worktreeConfig", "true", cwd=anchor)
    _git("config", "--worktree", "url.https://mirror.example/.insteadOf", "https://github.com/", cwd=wt)
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
    conn.commit()
    kwt._origin_cache.clear()
    assert kwt.origin_slug(wt) is None and kwt.origin_slug(anchor) == SLUG
    return tid, wt


def test_a_pin_is_read_from_the_repo_the_port_reads_not_the_worktree_config(kanban_home, anchor, monkeypatch):
    """Prism 881a62c60cff (finding input): anchor repo pinned to [ace-ai], a clean own linked worktree with a
    worktree-local `url.<x>.insteadOf` rewrite, pool plan present. The port reads the anchor's origin; so does the
    pin, so the card is limited to ace-ai and never reaches the Studio."""
    _pin(kanban_home, ["ace-ai"])
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    with kb.connect_closing() as conn:
        tid, wt = _own_tree_with_local_url_rewrite(conn, anchor)
        out: dict = {}
        caps: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _pinned_plan(), worktree_out=out, caps_out=caps,
                               studio_on=False)
    assert route[0] != "local", route
    if route[0] == "portable":
        assert out[tid][0] == SLUG and out[tid][2] == ("ace-ai",)
        assert caps["only"][tid] == ("ace-ai",), caps
    else:
        assert route[0] == "wait" and route[1].startswith("worktree_pin (ace-ai"), route


def test_the_port_and_the_pin_resolve_one_repo(kanban_home, anchor, monkeypatch):
    """One resolver answers "which repo is this card's workspace in" for the port and the pin: the anchor path,
    an existing own linked worktree (even with a worktree-local url rewrite), and a not-yet-made .worktrees/<id>."""
    _pin(kanban_home, ["ace-ai"])
    with kb.connect_closing() as conn:
        tid, wt = _own_tree_with_local_url_rewrite(conn, anchor)
    repo = anchor.resolve()
    for path in (str(anchor), str(wt), str(anchor / ".worktrees" / "t_notmade0")):
        tid_for = "t_notmade0" if path.endswith("t_notmade0") else tid
        assert kwt.workspace_repo(tid_for, path, None, allow_studio_tree=True)[0] == repo, path
        assert kwt.workspace_repo(tid_for, path, None, allow_studio_tree=False)[0] == repo, path
        assert kwt.repo_pin(tid_for, path, None, kanban_home / "fleet") == ("ace-ai",), path
    assert kwt.workspace_repo(tid, str(wt), None, allow_studio_tree=True)[1] == wt.resolve()
    ported, _push = kwt.assess(tid, str(wt), None, f"wt/{tid}", allow_studio_tree=True, push_ok=True, carry_out=[])
    assert ported == repo


# ---- t_7bb031b1 (Prism cf6083fccb97): an own worktree and its anchor must share one git common dir ----

def test_a_worktree_of_repo_a_inside_repo_b_reads_a_s_pin_and_does_not_port(kanban_home, anchor, tmp_path,
                                                                           monkeypatch):
    """Finding input: repos A and B, A's worktree at B/.worktrees/<id>, A pinned to [ace-ai]. The pin is A's (the
    worktree's real repo, its common dir's main tree), and the card does not port B's contents."""
    _pin(kanban_home, ["ace-ai"])
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    _commit(anchor, "base")
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("remote", "add", "origin", "https://github.com/ANG-Ventures/other-repo.git", cwd=other)
    _commit(other, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = other / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
        conn.commit()
        kwt._origin_cache.clear()
        repo, own, why = kwt.workspace_repo(tid, str(wt), None, allow_studio_tree=True)
        assert repo == anchor.resolve() and own is None and why == "own_tree_repo_mismatch", (repo, own, why)
        assert kwt.repo_pin(tid, str(wt), None, kanban_home / "fleet") == ("ace-ai",)
        out: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _pinned_plan(), worktree_out=out, studio_on=False)
    assert route[0] != "portable" and tid not in out, (route, out)
    assert route[0] == "wait" and route[1].startswith("worktree_pin (ace-ai"), route


def test_an_unpinned_worktree_of_repo_a_inside_repo_b_stays_local_with_the_studio_capability_on(
        kanban_home, anchor, tmp_path, monkeypatch):
    """No pin, studio_on (production): the mismatched tree's work exists only on the Studio, so the card stays
    local, named own_tree_repo_mismatch; it neither ports B nor waits forever."""
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    _commit(anchor, "base")
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("remote", "add", "origin", "https://github.com/ANG-Ventures/other-repo.git", cwd=other)
    _commit(other, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = other / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
        conn.commit()
        kwt._origin_cache.clear()
        out: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out, studio_on=True)
    assert tid not in out, out
    assert route == ("local", "not_portable:workspace_kind:own_tree_repo_mismatch"), route


@pytest.mark.parametrize("form", ["worktree", "anchor"])
def test_a_clean_worktree_of_repo_a_inside_repo_b_never_ports_b_in_either_persisted_form(
        kanban_home, anchor, tmp_path, monkeypatch, form):
    """Prism b7d84f5e8135: A's clean wt/<id> worktree at B/.worktrees/<id>, B without that branch. Whether
    workspace_path names (a) the worktree or (b) B, the card is not portable, named own_tree_repo_mismatch, and the
    pin is read from A (one check, :func:`_own_tree_of`, decides for workspace_repo and assess's discovery)."""
    _pin(kanban_home, ["ace-ai"])
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    _commit(anchor, "base")
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("remote", "add", "origin", "https://github.com/ANG-Ventures/other-repo.git", cwd=other)
    _commit(other, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
    wt = other / ".worktrees" / tid
    _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
    path = str(wt if form == "worktree" else other)
    kwt._origin_cache.clear()
    repo, own, refusal = kwt.workspace_repo(tid, path, None, allow_studio_tree=True)
    assert (repo, own, refusal) == (anchor.resolve(), None, "own_tree_repo_mismatch")
    why: list = []
    assert kwt.assess(tid, path, None, f"wt/{tid}", allow_studio_tree=True, push_ok=True, why=why,
                      carry_out=[]) == (None, None)
    assert why == ["own_tree_repo_mismatch"], why
    assert kwt.repo_pin(tid, path, None, kanban_home / "fleet") == ("ace-ai",)


# -- worktree cache (scope port_clone) and the never-prune mirror (t_969164a0) ---------------------------------


def _alternates(ws: Path) -> str:
    p = ws / ".git" / "objects" / "info" / "alternates"
    return p.read_text().strip() if p.exists() else ""


def test_clone_script_keeps_alternates_to_a_never_prune_mirror(tmp_path):
    """--dissociate copied every borrowed object: on ci-box 2 of the 3 clones born in 48 h with no
    alternates were port clones (t_d25e5125, t_1678415e: 450-580 MB .git each). A mirror that
    cannot prune (preciousObjects) is borrowed from instead."""
    origin, mirror = _origin_with_feature(tmp_path)
    _git("config", "core.repositoryformatversion", "1", cwd=mirror)
    _git("config", "extensions.preciousObjects", "true", cwd=mirror)
    ws = tmp_path / "ws"
    assert subprocess.run(["sh", "-c", kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))]
                          ).returncode == 0
    assert (ws / "f").read_text() == "feature"
    assert _alternates(ws) == str(mirror / "objects")
    # a mirror that can prune is still dissociated (Prism 02dfd9094b97)
    _git("config", "--unset", "extensions.preciousObjects", cwd=mirror)
    ws2 = tmp_path / "ws2"
    assert subprocess.run(["sh", "-c", kwt.clone_script(str(ws2), SLUG, str(mirror), "feature", url=str(origin))]
                          ).returncode == 0
    assert _alternates(ws2) == ""


def _fake_hermes(home: Path, active: bool) -> Path:
    """$HOME/.local/bin/hermes that logs its argv; `clone` runs a plain git clone."""
    b = home / ".local" / "bin"
    b.mkdir(parents=True)
    v = b / "hermes"
    v.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$HOME/calls"\n'
        f'if [ "$2" = active ]; then exit {0 if active else 1}; fi\n'
        'while [ $# -gt 0 ]; do case "$1" in --url) u=$2; shift;; --dest) d=$2; shift;; esac; shift; done\n'
        'exec git clone -q "$u" "$d"\n')
    v.chmod(0o755)
    return home / "calls"


@pytest.mark.parametrize("active", [True, False])
def test_clone_script_goes_through_the_cache_only_when_the_host_says_on(tmp_path, monkeypatch, active):
    origin, mirror = _origin_with_feature(tmp_path)
    home = tmp_path / "home"
    calls = _fake_hermes(home, active)
    monkeypatch.delenv("HERMES_WORKTREE_CACHE", raising=False)
    import os
    env = dict(os.environ, HOME=str(home))
    ws = tmp_path / "ws"
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc], env=env).returncode == 0
    lines = calls.read_text().splitlines()
    assert lines[0] == "worktree-cache active --caller port_clone"
    if active:
        assert lines[1:] == [f"worktree-cache clone --url {origin} --dest {ws} --caller port_clone"]
    else:
        assert lines[1:] == []                       # plain git, exactly as before
    assert (ws / "f").read_text() == "feature"
    assert (ws.stat().st_mode & 0o7777) == 0o770
    # the per-process switch: the CLI is never started
    calls.unlink()
    ws2 = tmp_path / "ws2"
    env["HERMES_WORKTREE_CACHE"] = "0"
    assert subprocess.run(["sh", "-c", kwt.clone_script(str(ws2), SLUG, str(mirror), "feature", url=str(origin))],
                          env=env).returncode == 0
    assert not calls.exists()
