"""Real Git/SQLite regressions for code surviving task completion and GC."""
import hashlib
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def fixture_repo(conn, nested=False):
    tid = kb.create_task(conn, title="implement fixture")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    repo = ws / "repo" if nested else ws
    repo.mkdir(exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws, repo


@pytest.mark.parametrize("nested", [False, True])
def test_completion_captures_patch_before_deleting(board, tmp_path, nested):
    tid, ws, repo = fixture_repo(board, nested)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("value = 2\n")
    git(repo, "add", "code.py")
    git(repo, "commit", "-m", "unpublished implementation")
    (repo / "new.bin").write_bytes(b"\x00\xff\x01")
    (repo / "ignored.txt").write_text("must not be captured")
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    attachments = kb.list_attachments(board, tid)
    assert len(attachments) == 1
    patch = Path(attachments[0].stored_path)
    assert patch.name == "implementation.patch"
    data = patch.read_bytes()
    assert b"must not be captured" not in data
    assert not ws.exists()
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["sha256"] == hashlib.sha256(data).hexdigest()
    assert survivor["bytes"] == len(data)
    # Recreate the exact base, then APPLY the stored artifact, not just grep it.
    restored = tmp_path / "restored"
    restored.mkdir()
    git(restored, "init")
    prefix = restored / "repo" if nested else restored
    prefix.mkdir(exist_ok=True)
    (prefix / "code.py").write_text("value = 1\n")
    (prefix / ".gitignore").write_text("ignored.txt\n")
    git(restored, "apply", str(patch))
    assert (prefix / "code.py").read_text() == "value = 2\n"
    assert (prefix / "new.bin").read_bytes() == b"\x00\xff\x01"
    assert base


def test_completion_with_pushed_clean_head_records_remote(board, tmp_path):
    tid, ws, repo = fixture_repo(board)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "code.py").write_text("value = 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "implementation")
    git(repo, "push", "origin", "HEAD:refs/heads/feature")
    sha = git(repo, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref"
    assert survivor["refs"][0]["sha"] == sha
    assert not kb.list_attachments(board, tid)


def test_pushed_head_does_not_cover_dirty_files(board, tmp_path):
    tid, ws, repo = fixture_repo(board)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "HEAD:main")
    (repo / "code.py").write_text("dirty = True\n")
    assert kb.complete_task(board, tid)
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "patch"


def test_write_failure_refuses_completion_and_holds_cleanup(board, monkeypatch):
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("value = 2\n")
    import importlib
    survivor = importlib.import_module("hermes_cli.kanban_survivor")
    monkeypatch.setattr(survivor, "_write_patch", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid)
    assert kb.get_task(board, tid).status != "done"
    kb._cleanup_workspace(board, tid)
    assert ws.exists()
    assert any(e.kind == "workspace_held" for e in kb.list_events(board, tid))


def test_reaper_preserves_before_removal(board):
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("value = 9\n")
    kb._cleanup_workspace(board, tid)
    assert not ws.exists()
    assert b"value = 9" in Path(kb.list_attachments(board, tid)[0].stored_path).read_bytes()




def test_temporary_index_preserves_git_racy_clean_detection(board):
    import os
    from hermes_cli.kanban_survivor import _snapshot
    tid, ws, repo = fixture_repo(board)
    git(repo, "config", "core.trustctime", "false")
    source = repo / "code.py"
    stamp = source.stat()
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns - 60_000_000_000))
    git(repo, "add", "code.py")
    stamp = source.stat()
    source.write_text("value = 9\n")
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    # Git must rehash an entry as old as its index, even when stat appears clean.
    os.utime(repo / ".git" / "index", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    data = _snapshot(repo, git(repo, "rev-parse", "HEAD"), "")
    assert b"value = 9" in data


def test_missing_workspace_with_code_claim_refuses(board):
    tid = kb.create_task(board, title="missing candidate")
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_retry_does_not_rebase_new_repository_after_edits(board):
    tid = kb.create_task(board, title="repository created by worker")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    kb.set_workspace_path(board, tid, ws)
    git(ws, "init", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    (ws / "implementation.py").write_text("answer = 42\n")
    git(ws, "add", ".")
    git(ws, "commit", "-m", "implementation")
    kb.set_workspace_path(board, tid, ws)
    assert kb.complete_task(board, tid)
    assert kb.list_attachments(board, tid), "retry must not bless unpublished HEAD as base"


def test_new_force_staged_ignored_file_survives(board):
    tid, ws, repo = fixture_repo(board)
    (repo / "ignored.txt").write_text("intentionally staged source\n")
    git(repo, "add", "-f", "ignored.txt")
    assert kb.complete_task(board, tid)
    attachments = kb.list_attachments(board, tid)
    assert attachments
    assert b"intentionally staged source" in Path(attachments[0].stored_path).read_bytes()


def test_subdirectory_workspace_captures_only_its_scope(board):
    tid, ws, repo = fixture_repo(board)
    sub = repo / "package"
    sub.mkdir()
    (sub / "module.py").write_text("old = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "package")
    other = kb.create_task(board, title="package task", workspace_kind="dir", workspace_path=str(sub))
    kb.set_workspace_path(board, other, sub)
    (sub / "module.py").write_text("new = True\n")
    (repo / "code.py").write_text("unrelated = True\n")
    assert kb.complete_task(board, other)
    attachments = kb.list_attachments(board, other)
    assert attachments
    data = Path(attachments[0].stored_path).read_bytes()
    assert b"new = True" in data and b"unrelated" not in data


def test_capture_is_idempotent_and_never_changes_real_index(board):
    from hermes_cli.kanban_survivor import preserve
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("staged = True\n")
    git(repo, "add", "code.py")
    (repo / "code.py").write_text("working = True\n")
    before = (repo / ".git" / "index").read_bytes()
    first = preserve(board, tid)
    second = preserve(board, tid)
    assert first == second
    assert len(kb.list_attachments(board, tid)) == 1
    assert (repo / ".git" / "index").read_bytes() == before


def test_reaper_write_failure_holds_workspace_even_after_disk_recovers(board, monkeypatch):
    import importlib
    survivor = importlib.import_module("hermes_cli.kanban_survivor")
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("must_survive = True\n")
    with monkeypatch.context() as m:
        m.setattr(survivor, "_write_patch", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        kb._cleanup_workspace(board, tid)
    kb._cleanup_workspace(board, tid)
    assert ws.exists()
    assert kb.complete_task(board, tid)
    assert not ws.exists()


def test_empty_code_claim_refuses(board):
    tid, ws, repo = fixture_repo(board)
    with pytest.raises(ValueError, match="empty patch"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    assert ws.exists()


def test_stale_remote_tracking_ref_is_not_a_survivor(board, tmp_path):
    tid, ws, repo = fixture_repo(board)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "code.py").write_text("unpublished_again = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "implementation")
    git(repo, "push", "origin", "HEAD:feature")
    git(remote, "update-ref", "-d", "refs/heads/feature")
    assert git(repo, "rev-parse", "refs/remotes/origin/feature")
    assert kb.complete_task(board, tid)
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "patch"


def test_archive_gc_uses_same_capture_guard(board, monkeypatch):
    import argparse
    from hermes_cli import kanban
    import importlib
    survivor = importlib.import_module("hermes_cli.kanban_survivor")
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("gc_recovery = True\n")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
    with monkeypatch.context() as m:
        m.setattr(survivor, "_write_patch", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        assert kanban._cmd_gc(argparse.Namespace()) == 0
    assert ws.exists()
    survivor.preserve(board, tid)
    assert kanban._cmd_gc(argparse.Namespace()) == 0
    assert not ws.exists()
    assert kb.list_attachments(board, tid)


def test_deferred_parent_capture_includes_later_edits(board):
    tid, ws, repo = fixture_repo(board)
    child = kb.create_task(board, title="review child", parents=[tid])
    (repo / "code.py").write_text("first = True\n")
    assert kb.complete_task(board, tid)
    assert ws.exists()
    (repo / "code.py").write_text("second = True\n")
    assert kb.complete_task(board, child)
    assert not ws.exists()
    attachments = kb.list_attachments(board, tid)
    assert len(attachments) == 2
    assert any(b"second = True" in Path(a.stored_path).read_bytes() for a in attachments)


def test_remote_timeout_falls_back_to_patch(board, monkeypatch):
    import importlib
    survivor = importlib.import_module("hermes_cli.kanban_survivor")
    tid, ws, repo = fixture_repo(board)
    git(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    (repo / "code.py").write_text("changed = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "implementation")
    real = survivor._git
    def offline(repo, *args, **kwargs):
        if args[0] == "ls-remote":
            raise subprocess.TimeoutExpired("git", 30)
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(survivor, "_git", offline)
    assert kb.complete_task(board, tid)
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "patch"


def test_embedded_repository_is_held_instead_of_invalid_gitlink_patch(board):
    tid, ws, repo = fixture_repo(board)
    child = repo / "embedded"
    child.mkdir()
    git(child, "init")
    (child / "new.py").write_text("nested = True\n")
    with pytest.raises(ValueError, match="nested repository"):
        kb.complete_task(board, tid)
    assert ws.exists()
