"""t_be0453f9: a re-run's workspace prepare repoints git alternates its login cannot read.

Pool workers moved from kanbanw to the host owner (Option A, 2026-11-20). A clone made earlier by
``hermes kanban clone`` borrows its objects from /home/kanbanw/.hermes/mirrors, which the owner cannot read, so
every git command in a retried card's checkout failed (71 files on ci-box, 21 on ace-ai, 164 on ace-media).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 dir")


def _git(*args, cwd=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


def _host():
    return kwp.PoolHost(name="ci-box", ssh_host="ci-box", ssh_user="ace", slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)


def _local(argv, **kw):
    return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True)


@pytest.fixture
def borrowed(tmp_path, monkeypatch):
    """A workspace clone that borrows every object from a kanbanw-owned mirror the login cannot read."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_MIRRORS_ROOT", raising=False)
    origin = tmp_path / "origin"
    origin.mkdir()
    assert _git("init", "-q", "-b", "main", cwd=origin).returncode == 0
    (origin / "f.txt").write_text("x\n")
    _git("add", "f.txt", cwd=origin)
    assert _git("commit", "-qm", "c1", cwd=origin).returncode == 0
    gone = tmp_path / "kanbanw" / ".hermes" / "mirrors"
    old = gone / "ANG-Ventures" / "hermes-agent.git"
    old.parent.mkdir(parents=True)
    assert _git("clone", "-q", "--bare", str(origin), str(old)).returncode == 0
    board = tmp_path / "default"
    ws = board / "t_x"
    ws.mkdir(parents=True)
    assert _git("clone", "-q", "--shared", str(old), str(ws / "repo")).returncode == 0
    alt = ws / "repo" / ".git" / "objects" / "info" / "alternates"
    assert alt.read_text().strip() == str(old / "objects")
    (tmp_path / "kanbanw").chmod(0o000)          # the kanbanw home, as the owner login sees it
    yield {"tmp": tmp_path, "origin": origin, "ws": ws, "alt": alt, "old": old}
    (tmp_path / "kanbanw").chmod(0o755)


def test_unreadable_alternates_are_repointed_at_the_logins_mirror(borrowed, monkeypatch):
    ws, alt = borrowed["ws"], borrowed["alt"]
    assert _git("log", "-1", "--format=%s", cwd=ws / "repo").returncode != 0      # the broken shape
    mine = borrowed["tmp"] / "home" / ".hermes-worker" / "mirrors"
    new = mine / "ANG-Ventures" / "hermes-agent.git"
    new.parent.mkdir(parents=True)
    assert _git("clone", "-q", "--bare", str(borrowed["origin"]), str(new)).returncode == 0
    monkeypatch.setenv("HERMES_KANBAN_MIRRORS_ROOT", str(mine))

    report = kwh.prepare_remote_workspace(_host(), str(ws), runner=_local, board="default")

    assert alt.read_text().strip() == str(new / "objects")
    assert _git("log", "-1", "--format=%s", cwd=ws / "repo").stdout.strip() == "c1"
    assert len(report) == 1 and report[0].startswith(f"repointed {alt} {borrowed['old']}/objects->")
    # Idempotent: a readable entry is left alone and reported no more.
    assert kwh.prepare_remote_workspace(_host(), str(ws), runner=_local, board="default") == []


def test_no_readable_mirror_is_reported_and_the_file_is_left_alone(borrowed):
    ws, alt = borrowed["ws"], borrowed["alt"]
    before = alt.read_text()
    report = kwh.prepare_remote_workspace(_host(), str(ws), runner=_local, board="default")
    assert alt.read_text() == before
    assert report == [f"unreadable {alt} {borrowed['old']}/objects"]


def test_a_mirror_missing_the_head_objects_is_reported_incomplete(borrowed, monkeypatch):
    mine = borrowed["tmp"] / "home" / ".hermes" / "mirrors"
    empty = mine / "ANG-Ventures" / "hermes-agent.git"
    empty.parent.mkdir(parents=True)
    assert _git("init", "-q", "--bare", str(empty)).returncode == 0
    report = kwh.prepare_remote_workspace(_host(), str(borrowed["ws"]), runner=_local, board="default")
    assert borrowed["alt"].read_text().strip() == str(empty / "objects")
    assert len(report) == 1 and report[0].startswith("incomplete ")


def test_a_fresh_workspace_reports_nothing(tmp_path):
    assert kwh.prepare_remote_workspace(_host(), str(tmp_path / "default" / "t_new"), runner=_local,
                                        board="default") == []
