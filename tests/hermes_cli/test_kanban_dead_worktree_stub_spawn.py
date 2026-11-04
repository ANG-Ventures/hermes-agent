"""A dead linked-worktree stub at a card's worktree target is held, uncharged,
and never burns the retry budget (t_42e59999).

Regression for t_5c6c67c5: ``.worktrees/<id>`` was a directory whose ``.git``
gitfile pointed at a pruned ``.git/worktrees/<id>``. Every ``git worktree add``
refused "already exists", and the card burned all 3 spawns to ``gave_up``.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import gateway.kanban_watchers as kw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worktree_stub as stubs


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def conn(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "a")
    return repo


def card_with_dead_stub(conn, repo):
    """The incident shape: branch wt/<id> at main tip, its worktree's admin dir pruned."""
    tid = kb.create_task(conn, title="dead stub", assignee="daedalus-opus",
                         workspace_kind="worktree", workspace_path=str(repo))
    stub = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", f"wt/{tid}", str(stub), "HEAD")
    shutil.rmtree(repo / ".git" / "worktrees" / tid)
    with pytest.raises(subprocess.CalledProcessError):  # git itself refuses it now
        git(stub, "rev-parse", "--git-dir")
    return tid, stub


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_file() else "dir"
        for p in sorted(root.rglob("*"))
    }


def spawned_into(record):
    def spawn(task, workspace, board=None):
        record.append(Path(workspace))
        return 4242
    return spawn


def test_dead_stub_is_held_uncharged_untouched_and_pages_once(conn, repo, monkeypatch):
    tid, stub = card_with_dead_stub(conn, repo)  # holds nothing unique: still held
    before = snapshot(stub)
    pages = []
    monkeypatch.setattr(kw, "_send_workspace_refusal_alert",
                        lambda board, summary: pages.append(summary) or True)
    notifier = kw._WorkspaceRefusalOutageNotifier()
    launched = []

    for _ in range(4):  # more ticks than the default retry budget
        result = kbd.dispatch_once(conn, spawn_fn=spawned_into(launched))
        kw._observe_workspace_refusal_outages(notifier, [("default", result)])
        assert tid not in result.spawn_failed
        assert dict(result.workspace_refused)[tid].startswith(f"{stubs.DEAD_STUB_HELD}: {stub}")

    assert launched == []
    assert snapshot(stub) == before
    assert not (stub.parent / ".aside").exists()
    row = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,),
    ).fetchone()
    assert len(pages) == 1 and tid in pages[0]


def test_hold_message_names_the_stub_its_gitfile_and_a_targeted_recovery(repo):
    stub = repo / ".worktrees" / "t_x"
    git(repo, "worktree", "add", "-q", "--detach", str(stub), "HEAD")
    admin = (repo / ".git" / "worktrees" / "t_x").resolve()
    (stub / ".git").write_text("gitdir: /nonexistent/admin\n")  # dead gitfile, admin kept

    msg = str(stubs.dead_stub_hold(repo, stub))

    assert msg.startswith(f"{stubs.DEAD_STUB_HELD}: {stub} ")
    assert "its .git points at /nonexistent/admin, which does not exist" in msg
    assert f"registered to it: {admin}" in msg
    assert f"mv {shlex.quote(str(stub))} " in msg
    assert f"{shlex.quote(str(admin))} still points at it" in msg
    assert "removing ONLY that dir" in msg
    assert "worktree prune" not in msg
    assert msg == str(stubs.dead_stub_hold(repo, stub))  # stable: the page latch's key


def _recovery_argv(msg: str, cwd: Path) -> list[str]:
    """Run the message's recovery command with mkdir/mv replaced by printers."""
    cmd = re.search(r"inspect it; (.*?)(?:; then, if |\. No repo-wide prune\.)", msg).group(1)
    subprocess.run(["bash", "-n", "-c", cmd], check=True)
    printers = 'mkdir() { printf "%s\\0" mkdir "$@"; }; mv() { printf "%s\\0" mv "$@"; }; '
    out = subprocess.run(["bash", "-c", printers + cmd], cwd=cwd, capture_output=True,
                         text=True, check=True).stdout
    return out.split("\0")[:-1]


@pytest.mark.parametrize("parent_name", ["repo$(touch INJECTED)", "repo`touch INJECTED`", "it's repo"])
def test_recovery_command_is_paste_safe(tmp_path, parent_name):
    """Prism P1 6bd05bad33ec: a path's `$(...)`, backticks or apostrophe stay literal."""
    parent = tmp_path / parent_name
    parent.mkdir()
    git(parent, "init", "-q", "-b", "main")
    stub = parent / ".worktrees" / "t_x"
    stub.mkdir(parents=True)
    (stub / ".git").write_text("gitdir: /nonexistent/admin\n")
    shell_cwd = tmp_path / "cwd"
    shell_cwd.mkdir()

    argv = _recovery_argv(str(stubs.dead_stub_hold(parent, stub)), shell_cwd)

    assert not list(tmp_path.rglob("INJECTED"))
    aside = f"{stub.parent}/.aside"
    assert argv[:5] == ["mkdir", "-p", aside, "mv", str(stub)]
    assert len(argv) == 6 and re.fullmatch(re.escape(f"{aside}/t_x-stub-") + r"\d{8}T\d{6}Z", argv[5])


def test_relative_gitdir_registration_is_named(repo):
    """Prism P2 c309eaeb9e7c: `--relative-paths` registrations resolve against the admin dir."""
    stub = repo / ".worktrees" / "t_r"
    git(repo, "worktree", "add", "-q", "--detach", str(stub), "HEAD")
    admin = repo / ".git" / "worktrees" / "t_r"
    (admin / "gitdir").write_text("../../../.worktrees/t_r/.git\n")
    (stub / ".git").write_text("gitdir: ../../.git/worktrees/t_r\n")

    msg = str(stubs.dead_stub_hold(repo, stub))

    assert f"registered to it: {admin.resolve()}" in msg
    assert f"its .git points at {admin.resolve()}, which exists" in msg


def test_unreadable_target_is_held_not_charged(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    stub.chmod(0o000)
    try:
        result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))
    finally:
        stub.chmod(0o755)

    refused = dict(result.workspace_refused)[tid]
    assert refused.startswith(f"{stubs.DEAD_STUB_HELD}: {stub}") and "unreadable" in refused
    assert tid not in result.spawn_failed
    row = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    assert (stub / "a.txt").read_text() == "a\n"


def test_live_worktree_at_target_is_reused_unchanged(conn, repo):
    tid = kb.create_task(conn, title="live", assignee="daedalus-opus",
                         workspace_kind="worktree", workspace_path=str(repo))
    target = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", f"wt/{tid}", str(target), "HEAD")
    (target / "wip.txt").write_text("in-flight\n")
    launched = []

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into(launched))

    assert tid not in result.spawn_failed and not result.workspace_refused
    assert len(launched) == 1
    assert (target / "wip.txt").read_text() == "in-flight\n"
