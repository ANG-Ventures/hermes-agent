"""Survivor capture must not scale with the number of PUBLISHED REMOTE REFS.

Card t_cbeb632f. The `kanban_complete` TOOL timed out at 420 s three times on
card t_a49e8a28 and wrote NOTHING (the task stayed `running`), while the CLI
closed the SAME card against the SAME DB in 1.818 s. The card's stated suspect
was workspace SIZE (632 MB / 39,738 files / three node_modules trees).

Measured, size is NOT the cause: a seeded 39,995-file scratch workspace with
one remote completes via `complete_task` in 1.52 s. The cause is REMOTE REF
COUNT. That workspace had 5 remotes advertising 7,937 heads, and both ref scans
in `kanban_survivor` spawned a `git` process PER REF:

    _remote_survivor  1 spawn/ref   90.9 ms/ref -> 721 s projected
    _base             2 spawns/ref  90.5 ms/ref -> 718 s projected

Either one alone exceeds the 420 s concurrent-tool ceiling
(`agent.tool_executor._DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`), which is why the
tool died silently while the CLI -- which never hits the ceiling -- did not.
The transition is starved before `complete_task` reaches its write txn, so the
worker reaches NO terminal state at all.

The fix replaces both per-ref loops with set-based `git rev-list` forms. These
tests gate the COMPLEXITY, not the wall clock: a fixed-cost implementation
issues the same number of git spawns for 4 refs as for 400, so the assertion is
a spawn-count ratio. A wall-clock bound would flake on a loaded runner; a spawn
count is deterministic and is exactly the property that broke.

Mutation check: restore either per-ref loop and `test_capture_cost_is_flat_in_ref_count`
goes red (measured 9.5x and 30.1x against the ~1.0x of the fix).
"""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _workspace_with_published_heads(conn, nheads):
    """A scratch workspace whose origin advertises ``nheads`` branches.

    The workspace is deliberately TINY (one source file). Only the published
    ref count varies between the two arms, so a cost difference between them
    can only be attributed to ref count.
    """
    tid = kb.create_task(conn, title=f"refs={nheads}")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(exist_ok=True)
    git(ws, "init", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    (ws / "code.py").write_text("value = 1\n")
    git(ws, "add", ".")
    git(ws, "commit", "-m", "base")

    remote = Path.home() / f"{tid}.git"
    git(ws, "init", "--bare", str(remote))
    git(ws, "remote", "add", "origin", str(remote))
    git(ws, "push", "origin", "HEAD:main")
    # Distinct commits so the advertised heads are distinct objects: a remote
    # advertising N copies of one sha would be deduplicated and prove nothing.
    for i in range(nheads - 1):
        (ws / "code.py").write_text(f"value = {i}\n")
        git(ws, "add", "code.py")
        git(ws, "commit", "-m", f"published {i}")
        git(ws, "push", "origin", f"HEAD:refs/heads/published-{i}")
    git(ws, "checkout", "-q", "main")

    # Unpushed implementation work -- the thing a survivor must preserve.
    (ws / "impl.py").write_text("implementation = True\n")
    git(ws, "add", "impl.py")
    git(ws, "commit", "-m", "unpublished implementation")

    kb.set_workspace_path(conn, tid, ws)
    return tid, ws


def _count_git_spawns(monkeypatch, fn):
    """Number of `git` subprocesses ``fn`` issues, and its return value."""
    spawns = []
    real = survivor._git

    def counting(repo, *args, **kwargs):
        spawns.append(args[0] if args else "?")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(survivor, "_git", counting)
    try:
        result = fn()
    finally:
        monkeypatch.setattr(survivor, "_git", real)
    return len(spawns), result


def test_capture_cost_is_flat_in_ref_count(board, monkeypatch):
    """The whole point of the card: capture must not scale with ref count.

    THIS is the assertion that goes red if either per-ref loop is restored.
    """
    few_tid, few_ws = _workspace_with_published_heads(board, 4)
    many_tid, many_ws = _workspace_with_published_heads(board, 40)

    def capture(ws):
        repo = ws.resolve()
        return lambda: survivor._capture(repo, ".", repo)

    few_spawns, few_result = _count_git_spawns(monkeypatch, capture(few_ws))
    many_spawns, many_result = _count_git_spawns(monkeypatch, capture(many_ws))

    # Both must still produce a real survivor (a patch against a published base).
    for ref, base, data in (few_result, many_result):
        assert ref is None, "unpushed work must not be reported as remote-covered"
        assert base, "a published base must still be found"
        assert data, "the unpushed implementation must still be captured"

    # 10x the refs must not mean ~10x the git spawns. The fix is O(1) in refs;
    # the bound allows generous slack for the fixed per-capture calls while
    # still failing hard on anything per-ref (measured: 9.5x for the
    # _remote_survivor loop, 30.1x for the _base loop, ~1.0x for the fix).
    assert many_spawns < few_spawns * 3, (
        f"capture cost scales with published ref count: {few_spawns} spawns at "
        f"4 refs vs {many_spawns} at 40 refs — a per-ref git loop is back"
    )


def test_bulk_ref_scan_matches_the_per_ref_answer(board, monkeypatch):
    """Parity: the set-based scans must agree with the per-ref definitions.

    A faster capture that picks a DIFFERENT base would silently change what the
    recovery patch is diffed against, so speed alone is not the contract.
    """
    tid, ws = _workspace_with_published_heads(board, 12)
    repo = ws.resolve()
    published = list(survivor._published_refs(repo, repo))
    assert len(published) >= 12
    head = survivor._git(repo, "rev-parse", "--verify", "HEAD").stdout.decode().strip()

    # Per-ref ground truth, written out longhand exactly as it was before.
    per_ref_survivor = None
    for ref in published:
        if ref["sha"] == head or survivor._git(
            repo, "merge-base", "--is-ancestor", head, ref["sha"], check=False
        ).returncode == 0:
            per_ref_survivor = dict(ref, head=head)
            break
    candidates = []
    for ref in published:
        mb = survivor._git(repo, "merge-base", "HEAD", ref["sha"], check=False)
        if mb.returncode == 0:
            sha = mb.stdout.decode().strip()
            distance = int(
                survivor._git(repo, "rev-list", "--count", f"{sha}..HEAD").stdout
            )
            candidates.append((distance, sha))
    per_ref_base = min(candidates)[1] if candidates else None

    assert survivor._remote_survivor(repo, head, published) == per_ref_survivor
    assert survivor._base(repo, published) == per_ref_base


def test_pushed_head_still_bases_on_head(board):
    """An empty `rev-list --boundary` walk means HEAD ITSELF is the base.

    Regression guard for the fix's own edge case: when HEAD is already
    published, git prints no boundary commit at all. Reading that as "no base"
    downgrades a pushed-but-dirty repo from a `patch` survivor to a whole-tree
    `bundle` — caught by 5 existing tests when the fix first landed.
    """
    tid, ws = _workspace_with_published_heads(board, 3)
    git(ws, "push", "origin", "HEAD:main", "--force")
    repo = ws.resolve()
    published = list(survivor._published_refs(repo, repo))
    head = survivor._git(repo, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
    assert survivor._base(repo, published) == head


def test_missing_advertised_object_does_not_abort_the_scan(board):
    """A remote may advertise a sha we do not have locally.

    `git rev-list ^<unknown-sha>` aborts the ENTIRE walk with
    "fatal: bad object" (the real workspace was missing 1 of 2,917 advertised
    shas). The per-ref loops passed check=False and skipped such refs, so the
    set-based form must filter to locally-present commits first — otherwise one
    unknown sha silently costs the whole survivor.
    """
    tid, ws = _workspace_with_published_heads(board, 4)
    repo = ws.resolve()
    published = list(survivor._published_refs(repo, repo))
    phantom = {"remote": "origin", "branch": "ghost", "sha": "0" * 40}

    base_without = survivor._base(repo, published)
    base_with = survivor._base(repo, [*published, phantom])
    assert base_with == base_without, "an unknown advertised sha broke the scan"

    head = survivor._git(repo, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
    assert survivor._remote_survivor(repo, head, [*published, phantom]) is None
