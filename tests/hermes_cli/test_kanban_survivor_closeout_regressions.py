"""Closeout regressions from real merged PRs whose merge commits are no longer tips."""
import argparse
import contextlib
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as cli
from hermes_cli import kanban_external_survivor as ext
from hermes_cli import kanban_survivor as survivor

HOME = "https://github.com/ANG-Ventures/hermes-home.git"
HOME_MERGE = "fd791c40dcfb2b58e08f0805bc512974cfedab19"
HOMELAB = "https://github.com/Kyzcreig/ace-media-homelab.git"
HOMELAB_MERGE = "deb2603bee7f566b44f13db4c411d612a88835a9"


def test_merged_pr_landed_tree_binds_named_card_without_unbound():
    result = survivor._verified_explicit("t_3684ae00", None, "ANG-Ventures/hermes-home#457")
    assert result[None]["sha"] == HOME_MERGE
    assert result[None]["corroborated_by"] == "landed-tree"


def test_merged_pr_without_card_id_still_requires_unbound():
    with pytest.raises(survivor.SurvivorUnavailable, match="does not name"):
        survivor._verified_explicit("t_fc150853", None, "Kyzcreig/ace-media-homelab#195")
    result = survivor._verified_explicit("t_fc150853", None, "Kyzcreig/ace-media-homelab#195",
                                         unbound=True)
    assert result[None]["sha"] == HOMELAB_MERGE


def test_merged_pr_tree_mismatch_remains_weak(monkeypatch):
    monkeypatch.setattr(ext, "_merged_tree_matches", lambda *a: False)
    with pytest.raises(survivor.SurvivorUnavailable, match="mention"):
        survivor._verified_explicit("t_3684ae00", None, "ANG-Ventures/hermes-home#457")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def test_real_merged_sha_reachable_from_default_branch():
    result = ext.verify_ref(f"{HOME}#{HOME_MERGE}")
    assert result["sha"] == HOME_MERGE
    assert result["branch"] == "refs/heads/main"


def test_real_merged_sha_on_master_tip():
    result = ext.verify_ref(f"{HOMELAB}#{HOMELAB_MERGE}")
    assert result["sha"] == HOMELAB_MERGE
    assert result["branch"] == "refs/heads/master"


def test_remote_failure_names_command_target_and_rc(monkeypatch):
    def fail(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, b"", b"fatal: inaccessible")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ext.RemoteUnavailable) as exc:
        ext.verify_ref(f"{HOME}#{HOME_MERGE}")
    message = str(exc.value)
    assert "git ls-remote" in message and "128" in message and HOME in message


def test_capture_failure_names_step_and_error(board, monkeypatch):
    tid = kb.create_task(board, title="capture steps")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)
    monkeypatch.setattr(survivor, "_repos", lambda _: (_ for _ in ()).throw(OSError("disk read failed")))
    with pytest.raises(survivor.SurvivorUnavailable, match="_repos.*disk read failed"):
        kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})


def test_explicit_verified_pr_does_not_drop_foreign_untracked_file(board):
    tid = kb.create_task(board, title="PR closure")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)
    repo = ws / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
    (repo / "untracked.txt").write_text("unpublished work")
    assert kb.complete_task(board, tid, survivor_pr="Kyzcreig/ace-media-homelab#195",
                            survivor_unbound=True, metadata={"changed_files": ["code.py"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "bundle"
    assert Path(saved["bundles"][0]["path"]).is_file()


def test_survivor_none_records_reason_and_followup(board):
    followup = kb.create_task(board, title="land repo implementation")
    tid = kb.create_task(board, title="deployed directly on host")
    assert kb.complete_task(board, tid, survivor_none=True, survivor_reason=f"deployed to host; landing tracked by {followup}",
                            metadata={"changed_files": ["/host/tool"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "none"
    assert saved["reason"].endswith(followup)
    assert saved["follow_up_card"] == followup


def test_survivor_none_refuses_missing_followup(board):
    tid = kb.create_task(board, title="deployed directly on host")
    with pytest.raises(survivor.SurvivorUnavailable, match="follow-up"):
        kb.complete_task(board, tid, survivor_none=True, survivor_reason="deployed to host",
                         metadata={"changed_files": ["/host/tool"]})


def test_cli_survivor_none_roundtrip(board, monkeypatch):
    followup = kb.create_task(board, title="repo landing")
    tid = kb.create_task(board, title="host deployment")
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    cli.build_parser(parser.add_subparsers(dest="command"))
    monkeypatch.setattr(kb, "connect_closing", lambda *a, **kw: contextlib.nullcontext(board))
    args = parser.parse_args(["kanban", "complete", tid, "--survivor-none", "--reason",
                              f"deployed to host; repo landing {followup}"])
    assert cli.kanban_command(args) == 0
    assert kb.get_task(board, tid).status == "done"
    assert kb.latest_run(board, tid).metadata["survivor"]["follow_up_card"] == followup


def test_survivor_none_cannot_reauthorize_workspace_removal(board):
    followup = kb.create_task(board, title="repo landing")
    tid = kb.create_task(board, title="host deployment")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "host-patch.py").write_text("unpublished copy")
    kb.set_workspace_path(board, tid, ws)
    assert kb.complete_task(board, tid, survivor_none=True,
                            survivor_reason=f"deployed to host; repo landing {followup}")
    assert (ws / "host-patch.py").exists(), "completion must not delete uncaptured bytes"
    assert survivor.remove_workspace_dir(board, tid, ws) is False
    assert (ws / "host-patch.py").exists()


def test_oversize_foreign_checkout_does_not_discard_unpublished_bytes(board, monkeypatch):
    tid = kb.create_task(board, title="explicit PR and oversized checkout")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)
    repo = ws / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
    (repo / "source.py").write_text("unpublished bytes" * 20)
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 100)
    with pytest.raises(survivor.SurvivorUnavailable, match="attachment limit"):
        kb.complete_task(board, tid, survivor_pr="Kyzcreig/ace-media-homelab#195",
                         survivor_unbound=True, metadata={"changed_files": ["source.py"]})
    assert (repo / "source.py").exists()
    assert kb.get_task(board, tid).status != "done"


def test_explicit_pr_closes_shared_dir_without_claiming_foreign_worktree(board, tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["git", "init", "-q", str(shared)], check=True, stdin=subprocess.DEVNULL)
    foreign = shared / ".worktrees" / "argus" / "another-card"
    subprocess.run(["git", "init", "-q", str(foreign)], check=True, stdin=subprocess.DEVNULL)
    (foreign / "unpublished.py").write_text("foreign work\n")
    tid = kb.create_task(board, title="shared tree deliverable", workspace_kind="dir",
                         workspace_path=str(shared))
    monkeypatch.setattr(ext, "verify_pr", lambda *a, **kw: {
        "remote": HOME, "branch": "refs/pull/457/head", "sha": HOME_MERGE,
        "external": True, "corroborated_by": "landed-tree"})
    assert kb.complete_task(board, tid, survivor_pr="ANG-Ventures/hermes-home#457")
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["refs"][0]["sha"] == HOME_MERGE
    assert saved["refs"][0]["repository"] == "."
    assert (foreign / "unpublished.py").read_text() == "foreign work\n"
    assert survivor.remove_workspace_dir(board, tid, shared) is False
    assert (foreign / "unpublished.py").exists(), "a closeout claim is not deletion authority"
