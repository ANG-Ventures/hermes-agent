"""A timed-out ``git worktree add`` is killed whole; its leftover is never handed to a worker (t_f609a5a0).

The hang is a REAL git: a smudge filter that sleeps runs inside the add's checkout child, i.e. inside the window
where git holds the new tree locked ``initializing`` and has not written its index yet, the exact state
t_33172156's spawn left on the Studio. The filter records its pid so the test can prove no process outlived the kill.

The hand-over gate (ruling:t_f609a5a0-cut-plus-proven-usable-gate): a linked tree is reused only when proven
finished from the tree itself (gitfile, samefile backlink, index without index.lock, no young ``initializing`` lock).
"""
from __future__ import annotations

import ast
import inspect
import os
import shlex
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worktree_add as kwa
from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / ".gitattributes").write_text("*.slow filter=slow\n", encoding="utf-8")
    for i in range(3):
        (repo / f"f{i}.slow").write_text(f"{i}\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    monkeypatch.setattr(kwa, "ADD_TIMEOUT_S", 2)
    return repo


def slow_checkout(repo, pidfile: Path, on=True):
    """The smudge filter records its pid and hangs. ``pidfile`` is shell-quoted (Prism #265 P2 c-d5881e1a2f1d7ae5)."""
    script = f"echo $$ >> {shlex.quote(str(pidfile))}; sleep 60; cat"
    git(repo, "config", "filter.slow.smudge", f"sh -c {shlex.quote(script)}" if on else "cat")


def _wait_for(pred, timeout=30.0):
    """Poll ``pred`` (a readiness marker, never a fixed sleep: Prism #265 P2 c-ca6771b4c8c38a46)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def registrations(repo) -> int:
    return git(repo, "worktree", "list", "--porcelain").count("\nworktree ") + 1


def alive(pid: int) -> bool:
    """False once ``pid`` is gone OR a zombie (Prism #265 P2 c-658240d2a3610b9e)."""
    try:
        os.kill(pid, 0)  # windows-footgun: ok — the test drives POSIX process groups
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8",
                        errors="replace").stdout.strip()
    return bool(st) and not st.startswith("Z")


def _hash_tree(root: Path) -> list:
    import hashlib
    if not root.exists():
        return []
    return [(str(p.relative_to(root)),
             hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else "-")
            for p in sorted(root.rglob("*"))]


def _snapshot_repo(repo) -> tuple:
    """Registrations, refs, and the CONTENT of every file in the trees and the admin dirs."""
    return (git(repo, "worktree", "list", "--porcelain"), git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            _hash_tree(repo / ".worktrees"), _hash_tree(repo / ".git" / "worktrees"))


def _task(tid, path, branch):
    return type("_Task", (), {"id": tid, "workspace_kind": "worktree", "workspace_path": str(path),
                              "branch_name": branch})()


def _killed_add(repo, tmp_path, tid):
    """A real group-killed add: returns (target, admin)."""
    slow_checkout(repo, tmp_path / f"{tid}.pids")
    target = repo / ".worktrees" / tid
    with pytest.raises(RuntimeError, match="timed out"):
        kbw._ensure_git_worktree(repo, target, f"wt/{tid}")
    slow_checkout(repo, tmp_path / f"{tid}.pids", on=False)
    return target, Path(git(target, "rev-parse", "--absolute-git-dir"))


def _finished(repo, tid, lock=None, age_s=0, branch=None):
    """A finished linked tree (index written), optionally locked ``lock`` with a lock ``age_s`` old."""
    target = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", branch or f"wt/{tid}", str(target), "HEAD")
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    if lock:
        git(repo, "worktree", "lock", "--reason", lock, str(target))
        if age_s:
            old = time.time() - age_s
            os.utime(admin / "locked", (old, old))
    return target, admin


# --- the group kill -------------------------------------------------------------------------------------------


def test_a_hung_add_is_killed_whole_and_the_spawn_removes_nothing(repo, tmp_path):
    pidfile = tmp_path / "smudge pids 'q'"                            # a space and quotes: the path is quoted
    slow_checkout(repo, pidfile)
    target = repo / ".worktrees" / "t_hang"
    before = registrations(repo)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out after 2 seconds") as ei:
        kbw._ensure_git_worktree(repo, target, "wt/t_hang")
    assert time.monotonic() - t0 < 30
    assert "nothing removed" in str(ei.value) and "inspect" in str(ei.value)

    pids = [int(p) for p in pidfile.read_text(encoding="utf-8-sig").split()]
    assert pids, "the smudge filter never ran: the add did not hang inside its checkout"
    assert _wait_for(lambda: not [p for p in pids if alive(p)], 5.0)  # no process of the group outlived it
    # SIGKILL only: git's SIGTERM cleanup never ran, so the half-built tree, its registration and the branch stay.
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    assert target.is_dir() and not (admin / "index").exists()
    assert (admin / "locked").read_text(encoding="utf-8").strip() == "initializing"
    assert registrations(repo) == before + 1
    assert git(repo, "branch", "--list", "wt/t_hang") != ""


def test_a_file_written_into_the_target_before_the_timeout_survives_the_kill(repo, tmp_path):
    """Prism #265 P1 c-f6873eb7b7d1e8fb: SIGTERM first let `git worktree add` run its own cleanup, which deleted the
    incomplete tree and a file someone wrote into it. The group gets SIGKILL directly; the file survives."""
    import threading
    pidfile = tmp_path / "smudge.pids"
    slow_checkout(repo, pidfile)
    target = repo / ".worktrees" / "t_written"
    seen = {}

    def write_when_hung():
        if _wait_for(lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8-sig").strip(), 20.0):
            (target / "someones-note.txt").write_text("mine\n", encoding="utf-8")
            seen["written"] = True

    th = threading.Thread(target=write_when_hung)
    th.start()
    with pytest.raises(RuntimeError, match="timed out"):
        kbw._ensure_git_worktree(repo, target, "wt/t_written")
    th.join()
    assert seen.get("written"), "the add never reached its checkout"
    assert (target / "someones-note.txt").read_text(encoding="utf-8-sig") == "mine\n"


def test_an_exceptional_exit_while_waiting_kills_the_group_and_reraises(tmp_path, monkeypatch):
    """Prism #265 P2 c-038d5f46d9069eb3: KeyboardInterrupt (any BaseException) mid-wait kills the group too."""
    pidfile = tmp_path / "child pid"
    calls = {"n": 0}
    real = subprocess.Popen.communicate

    def interrupted(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            assert _wait_for(lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8-sig").strip(), 10.0)
            raise KeyboardInterrupt
        return real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    inner = f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep 60"
    script = f"sh -c {shlex.quote(inner)} & wait"                     # a grandchild in the group, not the leader
    with pytest.raises(KeyboardInterrupt):
        kwa.run_git_group(["sh", "-c", script], timeout=30)
    child = int(pidfile.read_text(encoding="utf-8-sig").split()[0])
    assert _wait_for(lambda: not alive(child), 5.0)


def test_alive_treats_a_zombie_as_terminated(tmp_path):
    p = subprocess.Popen(["sh", "-c", "exit 0"])
    assert _wait_for(lambda: not alive(p.pid), 10.0)
    p.wait()
    assert not alive(p.pid)


# --- item 3: what the NEXT spawn does with a group-killed add's leftover ------------------------------------------


def test_the_next_spawn_never_hands_over_a_group_killed_adds_tree(repo, tmp_path):
    """On main baa4f3d9 both hand-over returns gave this tree to the worker (reuse return in _ensure_git_worktree,
    linked-checkout return in _resolve_worktree_workspace). With the gate both hold, uncharged, and touch nothing."""
    target, admin = _killed_add(repo, tmp_path, "t_next")
    before = _snapshot_repo(repo)
    with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD + r".*rule 3"):
        kbw._ensure_git_worktree(repo, target, "wt/t_next")
    with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):
        kbw._resolve_worktree_workspace(_task("t_next", target, "wt/t_next"))
    with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):        # board-anchored path, same tree
        kbw._anchored_worktree(repo, "t_next", "wt/t_next")
    assert _snapshot_repo(repo) == before and not (admin / "index").exists()


def test_the_wrong_branch_realign_does_not_check_out_into_a_half_built_tree(repo, tmp_path):
    target, admin = _killed_add(repo, tmp_path, "t_realign")
    before = _snapshot_repo(repo)
    with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):
        kbw._ensure_git_worktree(repo, target, "wt/other")
    assert _snapshot_repo(repo) == before


def test_decompose_siblings_of_a_half_built_root_fall_back_to_their_own_trees(repo, tmp_path):
    """Children inherit the root card's workspace_path. On another branch they fall back to .worktrees/<child>
    as on main; only the root (whose branch the tree carries) is held."""
    root, admin = _killed_add(repo, tmp_path, "t_root")
    for child in ("t_c1", "t_c2"):
        got, branch = kbw._resolve_worktree_workspace(_task(child, root, f"wt/{child}"))
        assert Path(got) == (repo / ".worktrees" / child).resolve() and branch == f"wt/{child}"
        assert (Path(got) / "f0.slow").is_file()
    with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):
        kbw._resolve_worktree_workspace(_task("t_root", root, "wt/t_root"))
    assert not (admin / "index").exists()


# --- the gate: proven trees are handed over --------------------------------------------------------------------


def test_a_finished_unlocked_tree_is_handed_over_by_both_returns(repo):
    target, _ = _finished(repo, "t_ok")
    kbw._ensure_git_worktree(repo, target, "wt/t_ok")
    got, branch = kbw._resolve_worktree_workspace(_task("t_ok", target, "wt/t_ok"))
    assert Path(got) == target.resolve() and branch == "wt/t_ok"


@pytest.mark.parametrize("lock,age_s", [("operator: keep this", 0), ("initializing", 10_000)])
def test_a_finished_tree_locked_otherwise_or_long_ago_is_proven(repo, lock, age_s):
    """Rule 4: another reason, or `initializing` older than 2 x ADD_TIMEOUT_S with the index present (the 150
    finished-but-locked legacy trees). The lock is left exactly as it was."""
    target, admin = _finished(repo, "t_lk", lock=lock, age_s=age_s)
    before = (admin / "locked").read_bytes()
    assert kwa.unproven(target) is None
    got, _ = kbw._resolve_worktree_workspace(_task("t_lk", target, "wt/t_lk"))
    assert Path(got) == target.resolve() and (admin / "locked").read_bytes() == before


def test_a_main_checkout_keeps_mains_behaviour(repo):
    assert kwa.unproven(repo) is None


def test_identity_is_by_inode_not_spelling(repo, tmp_path):
    """r8 P1: a requested path spelled differently from git's registration (here via a symlinked parent) is the
    same tree; a backlink naming another tree is not."""
    target, admin = _finished(repo, "t_sp")
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    assert kwa.unproven(alias / ".worktrees" / "t_sp") is None
    other, _ = _finished(repo, "t_other")
    (admin / "gitdir").write_text(str(other / ".git") + "\n", encoding="utf-8")
    assert "rule 2" in kwa.unproven(target) and "names another tree" in kwa.unproven(target)


def test_a_gitfile_linked_into_another_directory_is_held(repo, tmp_path):
    """Prism #265 r9 P1 :134: a symlinked or hardlinked copy of a finished tree's gitfile in another directory names
    the same admin dir and the same .git inode, but that directory is not the tree. Rule 1 refuses the symlink by
    lstat; rule 2 refuses the hardlink by the directory's own identity."""
    target, _ = _finished(repo, "t_src")
    sym, hard = tmp_path / "sym", tmp_path / "hard"
    sym.mkdir()
    hard.mkdir()
    (sym / ".git").symlink_to(target / ".git")
    os.link(target / ".git", hard / ".git")
    assert kwa.unproven(sym).startswith("rule 1") and "not a regular file" in kwa.unproven(sym)
    assert kwa.unproven(hard).startswith("rule 2") and "names another tree" in kwa.unproven(hard)
    for d in (sym, hard):
        with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):
            kwa.hold_unless_proven(d)
    assert kwa.unproven(target) is None


def test_a_tree_whose_name_ends_in_a_space_is_handed_over(repo):
    """Prism #265 r9 P2 :129: only line terminators are stripped from the gitfile and the gitdir backlink (git's
    read_gitfile strips only \\n and \\r). git sanitises the admin name it picks (`t_sp ` -> `t_sp-`), so the
    entry that ends in a space here is an admin dir renamed by hand, which git itself still reads."""
    target, admin = _finished(repo, "t_sp ", branch="wt/t_sp")         # a ref name cannot end in a space
    assert target.name.endswith(" ") and kwa.unproven(target) is None
    spaced = admin.with_name(admin.name + " ")
    admin.rename(spaced)
    (target / ".git").write_bytes(b"gitdir: " + os.fsencode(spaced) + b"\n")
    out = subprocess.run(["git", "-C", str(target), "rev-parse", "--absolute-git-dir"], capture_output=True,
                         check=True).stdout
    assert out == os.fsencode(spaced) + b"\n"                                   # git accepts the trailing space
    assert kwa.unproven(target) is None
    got, _ = kbw._resolve_worktree_workspace(_task("t_sp", target, "wt/t_sp"))
    assert Path(got) == target.resolve()


def test_a_running_add_is_held_on_rule_3_at_every_sample(repo, tmp_path):
    """Prism #265 r9 P1 :165, Apollo's measurement repeated wherever CI runs it (Linux runners, and the pool hosts
    run git 2.53.0): while a real slow add checks out, the gate never hands the tree over; once the gitfile and
    backlink exist it holds on rule 3 (no index / index.lock) until the add ends, then proves the finished tree."""
    git(repo, "config", "filter.slow.smudge", "sh -c 'sleep 1; cat'")
    target = repo / ".worktrees" / "t_run"
    index = repo / ".git" / "worktrees" / "t_run" / "index"   # fresh repo: git names the admin dir after the tree
    proc = subprocess.Popen(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "wt/t_run", str(target), "HEAD"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pre_index = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            running = proc.poll() is None
            why = kwa.unproven(target)
            # read AFTER the gate: the index only ever appears, so "absent now" means absent while the gate read
            if running and not index.exists():
                pre_index.append(why)
            if not running:
                break
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert proc.returncode == 0, proc.stderr.read()
    # Prism #265 r10 P2 :336: git unlocks `initializing` (and may run post-checkout) before it exits, so samples
    # between "index written" and "process gone" may read proven; they are not bounded. Every sample taken before
    # the index exists holds, and holds on rule 3 once the gitfile and backlink exist.
    assert all(w is not None for w in pre_index), pre_index
    rule3 = [w for w in pre_index if w.startswith("rule 3")]
    assert len(rule3) >= 10, pre_index                     # ~3 s of checkout at 0.1 s per sample
    first3 = next(i for i, w in enumerate(pre_index) if w.startswith("rule 3"))
    assert all(w.startswith("rule 3") for w in pre_index[first3:]), pre_index
    assert kwa.unproven(target) is None


# --- the gate: anything not proven holds, naming the rule -------------------------------------------------------


def _break(case, target, admin):
    if case == "gitfile_missing":
        (target / ".git").unlink()
    elif case == "gitfile_unreadable":
        os.chmod(target / ".git", 0)
    elif case == "not_a_gitfile":
        (target / ".git").write_text("hello\n", encoding="utf-8")
    elif case == "backlink_missing":
        (admin / "gitdir").unlink()
    elif case == "backlink_unreadable":
        os.chmod(admin / "gitdir", 0)
    elif case == "backlink_dangling":
        (admin / "gitdir").write_text("/nonexistent/x/.git\n", encoding="utf-8")
    elif case == "index_missing":
        (admin / "index").unlink()
    elif case == "index_lock":
        (admin / "index.lock").write_bytes(b"")
    elif case == "young_initializing":
        (admin / "locked").write_text("initializing", encoding="utf-8")
    elif case == "lock_unreadable":
        (admin / "locked").write_text("initializing", encoding="utf-8")
        os.chmod(admin / "locked", 0)


_CASES = {"gitfile_missing": "rule 1", "gitfile_unreadable": "rule 1", "not_a_gitfile": "rule 1",
          "backlink_missing": "rule 2", "backlink_unreadable": "rule 2", "backlink_dangling": "rule 2",
          "index_missing": "rule 3", "index_lock": "rule 3", "young_initializing": "rule 4",
          "lock_unreadable": "rule 4"}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_every_unproven_state_holds_and_names_its_rule(repo, case):
    if case.endswith("unreadable") and os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    target, admin = _finished(repo, "t_rd")
    _break(case, target, admin)
    try:
        why = kwa.unproven(target)
        assert why is not None and why.startswith(_CASES[case]), why
        with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD + ".*" + _CASES[case]):
            kwa.hold_unless_proven(target)
        if case not in ("gitfile_missing", "gitfile_unreadable", "not_a_gitfile"):
            # git still reads these as a linked checkout: the resolve return is the one that must hold
            with pytest.raises(WorkspaceUnavailable, match=kwa.UNPROVEN_HELD):
                kbw._resolve_worktree_workspace(_task("t_rd", target, "wt/t_rd"))
    finally:
        for p in (target / ".git", admin / "gitdir", admin / "locked"):
            if p.exists():
                os.chmod(p, 0o644)


# --- static: the PR deletes, unlocks and writes nothing --------------------------------------------------------


_DESTRUCTIVE = ("rmtree", "rmdir", "os.remove", ".unlink(", '"-d"', '"-D"', '"prune"', '"remove"', '"unlock"',
                ".write_", '"w"', '"wb"', '"a"')


def test_the_add_module_and_the_spawn_remove_nothing():  # noqa: source-proxy structural no-deletion ruling, not behaviour
    srcs = {"kanban_worktree_add.py": Path(kwa.__file__).read_text(encoding="utf-8-sig"),
            "_ensure_git_worktree": textwrap.dedent(inspect.getsource(kbw._ensure_git_worktree))}
    for name, src in srcs.items():
        for bad in _DESTRUCTIVE:
            assert bad not in src, f"{name} contains {bad!r}"
    calls = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(ast.parse(srcs["kanban_worktree_add.py"]))
             if isinstance(n, ast.Call)}
    assert not calls & {"unlink", "remove", "rmtree", "rename", "replace", "utime", "chmod"}
