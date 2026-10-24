"""t_b15005c7: a SHALLOW workspace clone must never surface a bare `git bundle failed (rc=128)`.

`_snapshot(base=None)` cuts the bundle in a throwaway `recovery.git` that
borrows the workspace's objects through GIT_ALTERNATE_OBJECT_DIRECTORIES. The
borrowed store does not carry the source's `shallow` grafts, so `bundle create
--all` walked into parents the clone never fetched and died rc=128, which no
classifier explained. Two shapes are pinned here:

* clean HEAD that the remote has since moved past: HEAD is still an ancestor of
  the live branch, so it is PUBLISHED and the card completes with a `ref`
  survivor (no bundle is needed at all);
* unpublished local work on a shallow clone: the bundle is cut against the
  shallow boundary and the card completes with a bundle that verifies in a
  shallow clone of the same remote.
"""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _identity(repo):
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")


def shallow_workspace(conn, tmp_path):
    """A card whose workspace is a `--depth 1` clone; the remote then advances.

    Returns (task id, workspace, remote, upstream). `bases` stays empty: the
    workspace path is set before the clone exists, so nothing is recorded.
    """
    tid = kb.create_task(conn, title="shallow clone")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kbw.set_workspace_path(conn, tid, ws)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    upstream = tmp_path / "upstream"
    git(tmp_path, "init", "-b", "main", str(upstream))
    _identity(upstream)
    for i in range(4):
        (upstream / f"f{i}.txt").write_text(f"{i}\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", f"c{i}")
    git(upstream, "remote", "add", "origin", str(remote))
    git(upstream, "push", "origin", "main")
    clone = ws / "repo"
    git(ws, "clone", "-q", "--depth", "1", f"file://{remote}", str(clone))
    _identity(clone)
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    # The remote moves on: its tip is now a commit the clone never fetched.
    (upstream / "later.txt").write_text("later\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "remote advanced")
    git(upstream, "push", "origin", "main")
    return tid, ws, remote, clone


def test_clean_shallow_head_behind_an_advanced_remote_is_published(board, tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    # A `file://` remote doubles as a canonical local tree; a real fleet remote
    # (https/ssh) never does. Disable that path so the remote is judged only by
    # what it advertises, as it is in production.
    monkeypatch.setattr(survivor, "_canonical_survivor", lambda *a, **k: None)
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    head = git(clone, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref", survivor
    assert survivor["refs"][0]["head"] == head
    assert survivor["refs"][0]["matched_by"] == "live-ancestor"
    assert not kb.list_attachments(board, tid)
    assert not ws.exists()


def test_unpublished_work_on_a_shallow_clone_is_bundled(board, tmp_path):
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    # Make the published tip unreachable from this clone's view: commit local
    # work on top of the shallow HEAD, and drop the remote's new tip from the
    # advertised set by rewinding nothing -- the new tip is simply absent.
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    work = git(clone, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"bundle", "patch"}, survivor
    attachments = kb.list_attachments(board, tid)
    stored = [Path(a.stored_path) for a in attachments
              if a.stored_path.endswith((".bundle", ".patch"))]
    assert stored, attachments
    artifact = stored[0]
    # Restore from the remote + the artifact alone.
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "-q", f"file://{remote}", str(restored))
    _identity(restored)
    if artifact.suffix == ".bundle":
        git(restored, "fetch", "-q", str(artifact), "refs/heads/implementation:refs/heads/recovered")
        git(restored, "checkout", "-q", "recovered")
        assert git(restored, "rev-parse", "HEAD^") == work
    else:
        git(restored, "apply", "-p2", str(artifact))
    assert (restored / "work.txt").read_text() == "unpublished\n"


def test_a_shallow_bundle_declares_its_boundary_as_prerequisites(board, tmp_path):
    """A shallow bundle omits everything behind the boundary; it must say so.

    Without declared prerequisites `bundle verify` calls it complete in an
    EMPTY repo, yet `clone`/`fetch` there die "Failed to traverse parents".
    Declared, an empty repo refuses it up front, and the remote's history
    plus the bundle restore the work.
    """
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    work = git(clone, "rev-parse", "HEAD")
    boundary = set((clone / ".git" / "shallow").read_text().split())
    assert kb.complete_task(board, tid)
    stored = [Path(a.stored_path) for a in kb.list_attachments(board, tid)
              if a.stored_path.endswith(".bundle")]
    assert stored, kb.list_attachments(board, tid)
    artifact = stored[0]
    listed = subprocess.run(["git", "bundle", "list-heads", str(artifact)],
                            capture_output=True, check=True).stdout.decode()
    assert "refs/heads/implementation" in listed
    header = artifact.read_bytes().split(b"\n\n", 1)[0].decode()
    prereqs = {line[1:].split()[0] for line in header.splitlines() if line.startswith("-")}
    assert prereqs == boundary, header
    # An empty repo cannot satisfy the boundary: verify says so instead of "okay".
    empty = tmp_path / "empty"
    git(tmp_path, "init", "-q", str(empty))
    verify = subprocess.run(["git", "-C", str(empty), "bundle", "verify", str(artifact)],
                            capture_output=True)
    assert verify.returncode != 0
    assert b"lacks these prerequisite commits" in verify.stderr, verify.stderr
    # Fetch the remote's history into that same empty repo, then the bundle restores.
    git(empty, "fetch", "-q", f"file://{remote}", "+refs/heads/*:refs/remotes/origin/*")
    git(empty, "bundle", "verify", str(artifact))
    git(empty, "fetch", "-q", str(artifact), "refs/heads/implementation:refs/heads/recovered")
    git(empty, "checkout", "-q", "recovered")
    assert git(empty, "rev-parse", "HEAD^") == work
    assert (empty / "work.txt").read_text() == "unpublished\n"


def test_a_stale_shallow_entry_whose_object_is_absent_still_bundles(board, tmp_path):
    """A `shallow` line naming an object the store lacks is not a boundary.

    Passed through as `^<sha>`, `bundle create` dies rc=128 "bad object"; it is
    dropped, and the real boundary is still declared.
    """
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    shallow = clone / ".git" / "shallow"
    boundary = set(shallow.read_text().split())
    with shallow.open("a") as fh:
        fh.write("1" * 40 + "\n")
    assert kb.complete_task(board, tid)
    stored = [Path(a.stored_path) for a in kb.list_attachments(board, tid)
              if a.stored_path.endswith(".bundle")]
    assert stored, kb.list_attachments(board, tid)
    header = stored[0].read_bytes().split(b"\n\n", 1)[0].decode()
    prereqs = {line[1:].split()[0] for line in header.splitlines() if line.startswith("-")}
    assert prereqs == boundary, header


def test_a_shallow_capture_that_still_fails_names_the_remedy(board, tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    # Simulate any future shallow-traversal failure: without the grafts the
    # bundle walks past the boundary exactly as it did before the fix.
    monkeypatch.setattr(survivor, "_borrow_shallow", lambda *a, **k: None)
    # Without grafts the live-boundary probe also fails; skip it so this pins
    # the bundle-traversal failure, not the boundary refusal.
    monkeypatch.setattr(survivor, "_require_live_boundary", lambda *a, **k: None)
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    message = str(raised.value)
    assert "shallow clone ./repo" in message
    assert "fetch --unshallow" in message
    assert ws.exists()


def _one_commit_shallow_workspace(conn, tmp_path):
    """Depth-1 clone of a one-commit upstream (Prism d722f02857e3 repro)."""
    tid = kb.create_task(conn, title="shallow, upstream gone")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kbw.set_workspace_path(conn, tid, ws)
    upstream = tmp_path / "upstream"
    git(tmp_path, "init", "-q", "-b", "main", str(upstream))
    _identity(upstream)
    (upstream / "base.txt").write_text("base\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "only")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(remote))
    clone = ws / "repo"
    git(ws, "clone", "-q", "--depth", "1", f"file://{remote}", str(clone))
    _identity(clone)
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    return tid, ws, remote, upstream, clone


def _commit_work(clone):
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "local work")


def test_shallow_boundary_on_no_durable_remote_keeps_the_workspace(board, tmp_path):
    """Prism d722f02857e3: the upstream holding the boundary is gone.

    The bundle omits the boundary tree and its unchanged blobs, so nothing can
    restore it; complete_task must refuse and keep the workspace.
    """
    import shutil

    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, upstream, clone = _one_commit_shallow_workspace(board, tmp_path)
    shutil.rmtree(remote)
    shutil.rmtree(upstream)
    git(clone, "remote", "remove", "origin")
    _commit_work(clone)
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    message = str(raised.value)
    assert "not reachable from any durable remote" in message, message
    # The boundary is gone upstream: `fetch --unshallow` cannot help, so it is not offered.
    assert "push the work to a durable remote" in message, message
    assert "fetch --unshallow" not in message, message
    assert ws.exists()
    assert (clone / "work.txt").read_text() == "unpublished\n"


def test_shallow_boundary_force_pushed_away_keeps_the_workspace(board, tmp_path):
    """The remote still exists, but its branch no longer contains the boundary."""
    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, upstream, clone = _one_commit_shallow_workspace(board, tmp_path)
    git(upstream, "checkout", "-q", "--orphan", "rewrite")
    (upstream / "other.txt").write_text("other\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "unrelated")
    git(upstream, "push", "-q", "--force", str(remote), "rewrite:main")
    git(remote, "gc", "-q", "--prune=now")
    _commit_work(clone)
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    assert "not reachable from any durable remote" in str(raised.value)
    assert ws.exists()


def _hang_on(branch_name, real, seen):
    """A `_reachable_from_live` that hangs PAST its timeout on one branch.

    A fetch that ignores its budget is the worst case for a shared budget: it
    eats the whole site budget and every candidate sorted after it (Prism
    dbe35c381e8f). Every other branch goes through ``real``.
    """
    import time

    def probe(repo, remote, branch, sha, *, timeout):
        seen.append(branch)
        if branch == branch_name:
            time.sleep(timeout + 0.5)
            return False
        return real(repo, remote, branch, sha, timeout=timeout)
    return probe


def test_boundary_probe_hung_first_candidate_does_not_starve_a_healthy_one(board, tmp_path,
                                                                          monkeypatch):
    """Prism dbe35c381e8f (`_require_live_boundary`): `origin/aaa` sorts before
    `origin/main` and hangs; the boundary is still proven through main."""
    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    boundary = git(clone, "rev-parse", "HEAD")
    # aaa's live tip is remote main's advanced tip (absent locally, so no base is found).
    git(remote, "update-ref", "refs/heads/aaa", git(remote, "rev-parse", "refs/heads/main"))
    git(clone, "update-ref", "refs/remotes/origin/aaa", boundary)
    _commit_work(clone)
    seen = []
    monkeypatch.setattr(survivor, "_LIVE_BASE_TIMEOUT", 1)
    monkeypatch.setattr(survivor, "_reachable_from_live",
                        _hang_on("aaa", survivor._reachable_from_live, seen))
    assert kb.complete_task(board, tid)
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "bundle"
    assert "main" in seen, seen


def test_live_ancestor_hung_first_candidate_does_not_starve_a_healthy_one(board, tmp_path,
                                                                         monkeypatch):
    """Prism dbe35c381e8f (`_live_ancestor_survivor`): a clean shallow HEAD is
    still published through `main` when `aaa`, sorted first, hangs."""
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_canonical_survivor", lambda *a, **k: None)
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    head = git(clone, "rev-parse", "HEAD")
    git(remote, "update-ref", "refs/heads/aaa", git(remote, "rev-parse", "refs/heads/main"))
    git(clone, "update-ref", "refs/remotes/origin/aaa", head)
    seen = []
    monkeypatch.setattr(survivor, "_LIVE_BASE_TIMEOUT", 1)
    monkeypatch.setattr(survivor, "_reachable_from_live",
                        _hang_on("aaa", survivor._reachable_from_live, seen))
    assert kb.complete_task(board, tid)
    ref = kb.latest_run(board, tid).metadata["survivor"]["refs"][0]
    assert (ref["branch"], ref["matched_by"], ref["head"]) == ("main", "live-ancestor", head)


def test_every_live_probe_of_a_complete_shares_one_deadline(board, tmp_path, monkeypatch):
    """No probe outlives the per-`preserve()` budget (420 s tool ceiling).

    With every live fetch hung and a 120 s site budget, the complete still
    returns inside `_PROBE_BUDGET` and refuses (fail-closed, workspace kept),
    naming the spent budget.
    """
    import threading
    import time

    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    _commit_work(clone)
    monkeypatch.setattr(survivor, "_PROBE_BUDGET", 1.0)
    budgets, release = [], threading.Event()

    def hung(repo, remote, branch, sha, *, timeout):
        budgets.append(timeout)
        release.wait(timeout)
        return False
    monkeypatch.setattr(survivor, "_reachable_from_live", hung)
    started = time.monotonic()
    try:
        with pytest.raises(survivor.SurvivorUnavailable) as raised:
            kb.complete_task(board, tid)
    finally:
        release.set()
    assert time.monotonic() - started < 10
    assert budgets and max(budgets) <= 1.0, budgets
    assert "live-probe budget of this complete ran out" in str(raised.value)
    assert ws.exists()


def test_a_spent_complete_budget_skips_ls_remote(tmp_path, monkeypatch):
    """`_published_refs` must not start a 30 s ls-remote past the deadline."""
    import time

    import hermes_cli.kanban_survivor as survivor
    calls = []
    real = survivor._git

    def spy(repo, *args, **kwargs):
        if args and args[0] == "ls-remote":
            calls.append(kwargs.get("timeout"))
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(survivor, "_git", spy)
    monkeypatch.setattr(survivor, "_durable_remote", lambda *a, **k: True)
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    token = survivor._probe_deadline.set(time.monotonic() - 1)
    try:
        assert list(survivor._published_refs(repo, tmp_path)) == []
    finally:
        survivor._probe_deadline.reset(token)
    assert calls == []
    token = survivor._probe_deadline.set(time.monotonic() + 5)
    try:
        list(survivor._published_refs(repo, tmp_path))
    finally:
        survivor._probe_deadline.reset(token)
    assert calls and 0 < calls[0] <= 5


def test_queued_branch_of_an_answered_remote_is_not_starved_by_hung_remotes(tmp_path, monkeypatch):
    """Prism bf66bcf399d2: aaa/a0 answers False at once, aaa/a1 is healthy and
    bbb..eee hang. Four slots, five hung-or-queued candidates: aaa/a1 must still
    be probed (and win) inside the site budget."""
    import time

    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_LIVE_BASE_TIMEOUT", 1)
    ws = tmp_path / "ws"
    git(tmp_path, "init", "-q", "-b", "main", str(ws))
    _identity(ws)
    shas = []
    for n in "abc":
        (ws / n).write_text(n, encoding="utf-8")
        git(ws, "add", ".")
        git(ws, "commit", "-qm", n)
        shas.append(git(ws, "rev-parse", "HEAD"))
    base, recorded = shas[0], shas[1]
    pairs = [("aaa", "a0"), ("aaa", "a1")] + [(r, "main") for r in ("bbb", "ccc", "ddd", "eee")]
    published = [{"remote": r, "branch": b, "sha": "0" * 40} for r, b in pairs]
    for r, b in pairs:
        git(ws, "update-ref", f"refs/remotes/{r}/{b}", recorded)

    def probe(repo, remote, branch, sha, *, timeout):
        if (remote, branch) == ("aaa", "a0"):
            return False
        if (remote, branch) == ("aaa", "a1"):
            return True
        time.sleep(timeout + 0.3)
        return False
    monkeypatch.setattr(survivor, "_reachable_from_live", probe)
    assert survivor._recorded_base(ws, recorded, published, base) == recorded


def test_queued_candidates_keep_a_share_of_the_budget_behind_hung_probes(tmp_path, monkeypatch):
    """Eight candidates, four slots, all but the last hang: the last one is
    still started with time left, because no probe takes the whole budget."""
    import time

    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_LIVE_BASE_TIMEOUT", 2)
    pairs = [(f"r{i}", "main") for i in range(8)]
    started = {}

    def probe(repo, remote, branch, sha, *, timeout):
        started[remote] = timeout
        if remote == "r7":
            return True
        time.sleep(timeout)
        return False
    monkeypatch.setattr(survivor, "_reachable_from_live", probe)
    assert survivor._first_live(tmp_path, pairs, "0" * 40) == ("r7", "main")
    assert started["r7"] > 0.2, started


def _helper_remote(tmp_path, monkeypatch):
    """A repo whose `slow` remote is an ext:: transport that records its pid and never answers."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file")
    pidfile = tmp_path / "helper.pid"
    helper = tmp_path / "helper.sh"
    helper.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec sleep 60\n", encoding="utf-8")
    helper.chmod(0o755)
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _identity(repo)
    (repo / "a").write_text("a\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "a")
    git(repo, "remote", "add", "slow", f"ext::{helper}")
    good = tmp_path / "good.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(good))
    git(repo, "remote", "add", "good", str(good))
    git(repo, "push", "-q", "good", "HEAD:refs/heads/main")
    return repo, git(repo, "rev-parse", "HEAD"), pidfile


def _helper_pid(pidfile):
    import time
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text(encoding="ascii").strip():
            return int(pidfile.read_text(encoding="ascii"))
        time.sleep(0.05)
    raise AssertionError("transport helper never started")


def _gone(pid, within=2.0):
    import time

    import psutil
    stop = time.monotonic() + within
    while time.monotonic() < stop:
        if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            return True
        time.sleep(0.05)
    psutil.Process(pid).kill()
    return False


@pytest.mark.platforms("posix")
def test_hung_fetch_kills_the_transport_helper_on_timeout(tmp_path, monkeypatch):
    """A timed-out probe fetch kills git's whole process group, not just `git
    fetch`: the transport helper grandchild must not be orphaned to init."""
    import time

    import hermes_cli.kanban_survivor as survivor
    repo, sha, pidfile = _helper_remote(tmp_path, monkeypatch)
    started = time.monotonic()
    assert survivor._reachable_from_live(repo, "slow", "main", sha, timeout=1.5) is False
    assert time.monotonic() - started < 5
    assert _gone(_helper_pid(pidfile))


@pytest.mark.platforms("posix")
def test_first_live_kills_abandoned_probes_and_joins_its_threads(tmp_path, monkeypatch):
    """The winner returns; the hung sibling's helper dies with the call and no
    probe thread outlives it."""
    import threading
    import time

    import hermes_cli.kanban_survivor as survivor
    repo, sha, pidfile = _helper_remote(tmp_path, monkeypatch)
    monkeypatch.setattr(survivor, "_LIVE_BASE_TIMEOUT", 30)
    real = survivor._reachable_from_live

    def probe(repo, remote, branch, sha, *, timeout):
        if remote == "good":
            _helper_pid(pidfile)            # the hung probe is really in flight
        return real(repo, remote, branch, sha, timeout=timeout)
    monkeypatch.setattr(survivor, "_reachable_from_live", probe)
    started = time.monotonic()
    assert survivor._first_live(repo, [("slow", "main"), ("good", "main")], sha) == ("good", "main")
    assert time.monotonic() - started < 10
    assert _gone(_helper_pid(pidfile), within=0.5)
    assert not [t for t in threading.enumerate() if t.name.startswith("kanban-live-probe")]


def test_preserve_joins_its_probe_pool_before_returning(board, tmp_path, monkeypatch):
    """Every live probe runs on its `preserve()` call's pool, shut down (joined)
    before the call returns: no probe thread is left running after it."""
    import threading

    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    _commit_work(clone)
    pools = []
    real = survivor._first_live

    def spy(*a, **k):
        pools.append(survivor._probe_pool.get())
        return real(*a, **k)
    monkeypatch.setattr(survivor, "_first_live", spy)
    assert kb.complete_task(board, tid)
    assert pools and all(p is not None and p._shutdown for p in pools)
    assert survivor._probe_pool.get() is None
    assert not [t for t in threading.enumerate() if t.name.startswith("kanban-live-probe")]


@pytest.mark.parametrize("raw, expected", [
    ("", 90), ("30", 30.0), ("2.5", 2.5), ("9999", 110), ("0", 90), ("-4", 90), ("soon", 90),
])
def test_probe_budget_env_override_is_capped_under_the_tool_ceiling(monkeypatch, raw, expected):
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_PROBE_BUDGET", raw)
    assert survivor._probe_budget() == expected
    assert survivor._PROBE_BUDGET_MAX + survivor._SNAPSHOT_TIMEOUT < 420
