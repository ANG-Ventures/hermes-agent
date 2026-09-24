"""Closeout regressions from real merged PRs whose merge commits are no longer tips."""
import argparse
import ast
import contextlib
import os
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

# These fixtures are the REAL merged PRs from the incident (card t_6d221fe6):
# private GitHub repos, so they need the operator's git/gh credentials. CI has
# none ("could not read Username"), so they carry the repo's standard
# ``integration`` marker (deselected by addopts). Run locally with
# ``-m integration``. The cwd/env contract they exercise is ALSO gated
# hermetically below (``test_query_*``) against a local bare repo.
LIVE_REMOTE = pytest.mark.integration


@LIVE_REMOTE
def test_merged_pr_landed_tree_binds_named_card_without_unbound():
    result = survivor._verified_explicit("t_3684ae00", None, "ANG-Ventures/hermes-home#457")
    assert result[None]["sha"] == HOME_MERGE
    assert result[None]["corroborated_by"] == "landed-tree"


@LIVE_REMOTE
def test_merged_pr_without_card_id_still_requires_unbound():
    with pytest.raises(survivor.SurvivorUnavailable, match="does not name"):
        survivor._verified_explicit("t_fc150853", None, "Kyzcreig/ace-media-homelab#195")
    result = survivor._verified_explicit("t_fc150853", None, "Kyzcreig/ace-media-homelab#195",
                                         unbound=True)
    assert result[None]["sha"] == HOMELAB_MERGE


@LIVE_REMOTE
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


@LIVE_REMOTE
def test_real_merged_sha_reachable_from_default_branch():
    result = ext.verify_ref(f"{HOME}#{HOME_MERGE}")
    assert result["sha"] == HOME_MERGE
    assert result["branch"] == "refs/heads/main"


@LIVE_REMOTE
def test_real_merged_sha_reachable_from_clone_url_without_dot_git():
    result = ext.verify_ref(f"{HOME.removesuffix('.git')}#{HOME_MERGE}")
    assert result["sha"] == HOME_MERGE
    assert result["branch"] == "refs/heads/main"


@LIVE_REMOTE
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


@LIVE_REMOTE
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


@LIVE_REMOTE
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


HOME_PR_HEAD = "23672b199402bc2afafb8ea2df43e75b7aae7daf"  # squash-merged: NOT on main


@pytest.fixture
def tripwire_cwd(tmp_path, monkeypatch):
    """The worker's real cwd shape: a dir under a gitfile pointing at /nonexistent."""
    root = tmp_path / "workspaces"
    (root / "t_cwdprobe").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /nonexistent/kanban-scratch-workspace-is-not-a-repo\n")
    monkeypatch.chdir(root / "t_cwdprobe")
    probe = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True,
                           stdin=subprocess.DEVNULL)
    assert probe.returncode == 128, "fixture must reproduce the tripwire"
    return root / "t_cwdprobe"


@LIVE_REMOTE
def test_ref_verifies_from_tripwire_cwd(tripwire_cwd):
    """Argus r1 F1: ls-remote from an inherited scratch cwd exited 128."""
    result = ext.verify_ref(f"{HOMELAB}#{HOMELAB_MERGE}")
    assert result["sha"] == HOMELAB_MERGE


@LIVE_REMOTE
def test_merged_sha_ancestry_verifies_from_tripwire_cwd(tripwire_cwd):
    result = ext.verify_ref(f"{HOME}#{HOME_MERGE}")
    assert result["branch"] == "refs/heads/main"


@LIVE_REMOTE
def test_landed_tree_binds_from_tripwire_cwd(tripwire_cwd):
    result = survivor._verified_explicit("t_3684ae00", None, "ANG-Ventures/hermes-home#457")
    assert result[None]["corroborated_by"] == "landed-tree"


@LIVE_REMOTE
def test_inherited_git_dir_does_not_redirect_remote_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    assert ext.verify_ref(f"{HOMELAB}#{HOMELAB_MERGE}")["sha"] == HOMELAB_MERGE


@LIVE_REMOTE
def test_unreachable_sha_names_default_branch_and_compare_status():
    """Argus r1 F4: the squashed PR head is known to GitHub but not on main."""
    with pytest.raises(survivor.SurvivorUnavailable) as exc:
        survivor._verified_explicit("t_3684ae00", f"{HOME}#{HOME_PR_HEAD}", None, unbound=True)
    message = str(exc.value)
    assert "refs/heads/main" in message
    assert "status=diverged" in message


_SURVIVOR_MODULES = ("hermes_cli/kanban_survivor.py", "hermes_cli/kanban_external_survivor.py")


def _subprocess_calls_without_cwd(source):
    missing = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                and not any(k.arg == "cwd" for k in node.keywords)):
            missing.append(node.lineno)
    return missing


def test_survivor_path_subprocesses_never_inherit_process_cwd():
    """Contract: no git/gh subprocess on the survivor path runs in the caller's cwd."""
    root = Path(__file__).resolve().parents[2]
    offenders = {mod: _subprocess_calls_without_cwd((root / mod).read_text())
                 for mod in _SURVIVOR_MODULES}
    assert not any(offenders.values()), offenders


def test_cwd_contract_detects_a_dropped_cwd():
    """Mutant arm: stripping ``cwd=`` from the real module must turn the contract RED."""
    root = Path(__file__).resolve().parents[2]
    source = (root / "hermes_cli/kanban_external_survivor.py").read_text()
    mutant = source.replace("cwd=neutral_cwd(), env=scrubbed_env())", "env=scrubbed_env())", 1)
    assert mutant != source
    assert _subprocess_calls_without_cwd(mutant)


def _dirty_repo(ws, files):
    repo = ws / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
    for name, body in files.items():
        (repo / name).write_bytes(body)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True,
                   stdin=subprocess.DEVNULL, env=env)
    # A bare "published" remote that carries HEAD, so HEAD is a published base.
    remote = ws.parent / f"{ws.name}-published.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True,
                   stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", f"file://{remote}"],
                   check=True, stdin=subprocess.DEVNULL)
    return repo


def _scratch(board, title):
    tid = kb.create_task(board, title=title)
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(board, tid, ws)
    return tid, ws


@pytest.fixture
def published_ok(monkeypatch):
    # file:// remotes under tmp are (correctly) not durable; this fixture is
    # about the capture shape, not the durability rule, so accept them here.
    monkeypatch.setattr(survivor, "_durable_remote", lambda *a, **kw: True)


def test_pure_deletion_over_limit_captures_irreversible_patch(board, monkeypatch, published_ok):
    """Argus r1 F3 (t_3684ae00 shape): deletion-only dirt against a published HEAD."""
    tid, ws = _scratch(board, "truncated checkout")
    repo = _dirty_repo(ws, {f"big{i}.bin": os.urandom(4000) for i in range(5)} | {"keep.txt": b"k\n"})
    for i in range(5):
        (repo / f"big{i}.bin").unlink()
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4000)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["x"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "patch"
    patch = Path(saved["path"]).read_bytes()
    assert b"irreversible-delete=1" in patch
    for i in range(5):
        assert f"diff --git a/repo/big{i}.bin b/repo/big{i}.bin".encode() in patch
    assert len(patch) <= 4000


def test_added_bytes_are_never_cut_and_still_refuse_over_limit(board, monkeypatch, published_ok):
    """Sibling: new content cannot be derived from any commit, so it is never cut."""
    tid, ws = _scratch(board, "real new work")
    repo = _dirty_repo(ws, {f"big{i}.bin": os.urandom(4000) for i in range(5)})
    for i in range(5):
        (repo / f"big{i}.bin").unlink()
    (repo / "new.py").write_bytes(os.urandom(6000))
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4000)
    with pytest.raises(survivor.SurvivorUnavailable, match="attachment limit"):
        kb.complete_task(board, tid, metadata={"changed_files": ["new.py"]})
    assert (repo / "new.py").exists()
    assert kb.get_task(board, tid).status != "done"


def test_added_line_beside_deletions_is_captured_in_full(board, monkeypatch, published_ok):
    tid, ws = _scratch(board, "deletions plus one line")
    repo = _dirty_repo(ws, {f"big{i}.bin": os.urandom(4000) for i in range(5)} | {"keep.txt": b"k\n"})
    for i in range(5):
        (repo / f"big{i}.bin").unlink()
    (repo / "keep.txt").write_bytes(b"k\nUNPUBLISHED-LINE\n")
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4000)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["keep.txt"]})
    patch = Path(kb.latest_run(board, tid).metadata["survivor"]["path"]).read_bytes()
    assert b"+UNPUBLISHED-LINE" in patch


def test_under_limit_patch_keeps_full_reversible_form(board, published_ok):
    tid, ws = _scratch(board, "small deletion")
    repo = _dirty_repo(ws, {"a.txt": b"alpha\n", "b.txt": b"beta\n"})
    (repo / "a.txt").unlink()
    assert kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    patch = Path(kb.latest_run(board, tid).metadata["survivor"]["path"]).read_bytes()
    assert b"irreversible-delete" not in patch and b"-alpha" in patch


def test_presence_probe_never_lazy_fetches(tmp_path, monkeypatch):
    """Argus r1 F2: every local-presence probe runs with GIT_NO_LAZY_FETCH=1."""
    seen = []
    real = subprocess.run

    def spy(args, **kwargs):
        seen.append((args, kwargs.get("env") or {}, kwargs.get("cwd")))
        return real(args, **kwargs)

    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True, stdin=subprocess.DEVNULL)
    monkeypatch.setattr(subprocess, "run", spy)
    survivor._present_commits(tmp_path / "r", ["0" * 40])
    args, env, cwd = seen[-1]
    assert "cat-file" in args and env.get("GIT_NO_LAZY_FETCH") == "1"
    assert cwd == ext.neutral_cwd()


def test_explicit_claim_persists_beside_captured_delta(board, monkeypatch, published_ok):
    """Argus r1 F5: squash-landed work closed as refs:[] + NOT PUSHED."""
    tid, ws = _scratch(board, "squash landed with residual dirt")
    repo = _dirty_repo(ws, {"a.txt": b"alpha\n"})
    (repo / "a.txt").write_bytes(b"alpha\nresidual\n")
    monkeypatch.setattr(ext, "verify_pr", lambda *a, **kw: {
        "remote": HOMELAB, "branch": "refs/pull/195/head", "sha": HOMELAB_MERGE,
        "pr": "Kyzcreig/ace-media-homelab#195", "state": "MERGED", "external": True})
    assert kb.complete_task(board, tid, survivor_pr="Kyzcreig/ace-media-homelab#195",
                            survivor_unbound=True, metadata={"changed_files": ["a.txt"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["claims"][0]["sha"] == HOMELAB_MERGE
    assert saved["notice"] != "NOT PUSHED"
    assert Path(saved["path"]).is_file()


@pytest.fixture
def bare_remote(tmp_path):
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, stdin=subprocess.DEVNULL)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(src), "commit", "-q", "--allow-empty", "-m", "c"], check=True,
                   stdin=subprocess.DEVNULL, env=env)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True,
                   stdin=subprocess.DEVNULL)
    sha = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], check=True, capture_output=True,
                         stdin=subprocess.DEVNULL).stdout.decode().strip()
    return f"file://{bare}", sha


def test_query_answers_from_tripwire_cwd(tripwire_cwd, bare_remote):
    """Hermetic Argus r1 F1: the remote choke point must not inherit the tripwire cwd."""
    url, sha = bare_remote
    inherited = subprocess.run(["git", "ls-remote", "--heads", "--", url], capture_output=True,
                               stdin=subprocess.DEVNULL)
    assert inherited.returncode == 128, "without an explicit cwd the probe must reproduce F1"
    assert f"{sha}\trefs/heads/main" in ext._query(["git", "ls-remote", "--heads", "--", url])


def test_query_ignores_inherited_git_dir(monkeypatch, tmp_path, bare_remote):
    url, sha = bare_remote
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    assert sha in ext._query(["git", "ls-remote", "--heads", "--", url])
