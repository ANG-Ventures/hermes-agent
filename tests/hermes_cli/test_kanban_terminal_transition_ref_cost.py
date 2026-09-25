"""All four terminal transitions must survive a pathological remote ref count.

Card t_cbeb632f required checking the CLASS, not just `kanban_complete`:
"any terminal transition that can be starved by workspace size has the same
hole. Check all four."

Measured answer: only `complete_task` reaches the survivor ref scan (via
`preserve`); `block_task`, `request_review` and `request_changes` never call it,
so they were never exposed. This test PINS that — it fails if a future change
routes a ref scan onto one of the other three, and it fails if
`kanban_complete` regains a per-ref loop.

The gate is a git-spawn count, not wall clock, so it cannot flake on a loaded
runner.
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


NHEADS = 60


def _card_with_many_published_heads(conn):
    tid = kb.create_task(conn, title="terminal transition under many refs")
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
    for i in range(NHEADS):
        (ws / "code.py").write_text(f"value = {i}\n")
        git(ws, "add", "code.py")
        git(ws, "commit", "-m", f"published {i}")
        git(ws, "push", "origin", f"HEAD:refs/heads/published-{i}")
    git(ws, "checkout", "-q", "main")
    (ws / "impl.py").write_text("implementation = True\n")
    git(ws, "add", "impl.py")
    git(ws, "commit", "-m", "unpublished implementation")
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws


def _spawns_during(monkeypatch, fn):
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
    return spawns, result


# A fixed-cost transition issues a small constant number of git calls. Anything
# per-ref lands at >= NHEADS. The bound sits far below NHEADS and far above the
# handful a correct capture needs.
#
# Raised 30 -> 40 for t_6c46905a, which added two FIXED calls per `preserve`
# (`git worktree list --porcelain` + `git ls-files --stage`) so the survivor no
# longer has to walk a whole home directory to discover a nested repository.
# Measured flat before raising it -- 15 total spawns at 6 published heads and 15
# at 60, registry cost 2 in both -- so the property this constant guards
# (fixed-cost, not per-ref) still holds; only the constant was stale. If this
# assertion fires again, re-measure the SLOPE across two ref counts before
# raising it: a per-ref regression shows as a difference between the two, not as
# a larger number at one.
SPAWN_CEILING = 40


def test_complete_is_not_starved_by_ref_count(board, monkeypatch):
    tid, ws = _card_with_many_published_heads(board)
    spawns, ok = _spawns_during(
        monkeypatch,
        lambda: kb.complete_task(
            board, tid, summary="done", metadata={"changed_files": ["impl.py"]},
        ),
    )
    assert ok is True
    assert kb.get_task(board, tid).status == "done"
    # The regression the card is about: the transition must LAND, durably.
    assert len(spawns) < SPAWN_CEILING, (
        f"complete_task issued {len(spawns)} git spawns against {NHEADS} "
        f"published heads — a per-ref scan is back: {spawns}"
    )
    # And it must not have "fixed" the timeout by dropping the recovery index.
    row = board.execute(
        "SELECT survivor, held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,),
    ).fetchone()
    assert row is not None and row["survivor"], "survivor row must still be recorded"
    assert row["held_reason"] is None


@pytest.mark.parametrize("transition", ["block", "request_review", "request_changes"])
def test_other_terminal_transitions_never_reach_the_ref_scan(
    board, monkeypatch, transition
):
    tid, ws = _card_with_many_published_heads(board)
    calls = {
        "block": lambda: kb.block_task(board, tid, reason="needs a decision"),
        "request_review": lambda: kb.request_review(
            board, tid, summary="please review", reviewer="argus",
        ),
        "request_changes": lambda: kb.request_changes(
            board, tid, reason="please fix",
        ),
    }
    spawns, _ = _spawns_during(monkeypatch, calls[transition])
    assert len(spawns) < SPAWN_CEILING, (
        f"{transition} issued {len(spawns)} git spawns against {NHEADS} "
        f"published heads — it has acquired the starvation hole "
        f"kanban_complete had"
    )


def _card_contained_behind_a_tip(conn):
    """A CLEAN card whose HEAD is a strict ancestor of a published ref.

    `_card_with_many_published_heads` always leaves an unpushed commit, so it
    only ever drives `_remote_survivor`'s "HEAD has outside commits" fast path.
    This shape reaches the step that NAMES the containing ref -- which is where
    a per-ref `merge-base --is-ancestor` scan survived the first fix and where,
    measured on the real 7,939-ref workspace, the old form cost 6,082 spawns /
    653.6 s against the new form's 13 / 2.4 s (same ref, exact parity).
    """
    tid = kb.create_task(conn, title="contained HEAD under many refs")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(exist_ok=True)
    git(ws, "init", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    (ws / "code.py").write_text("value = 1\n")
    git(ws, "add", ".")
    git(ws, "commit", "-m", "base")
    root = git(ws, "rev-parse", "HEAD")
    remote = Path.home() / f"{tid}.git"
    git(ws, "init", "--bare", str(remote))
    git(ws, "remote", "add", "origin", str(remote))

    (ws / "impl.py").write_text("implementation = True\n")
    git(ws, "add", "impl.py")
    git(ws, "commit", "-m", "the commit that will be HEAD")
    head = git(ws, "rev-parse", "HEAD")
    (ws / "more.py").write_text("more = True\n")
    git(ws, "add", "more.py")
    git(ws, "commit", "-m", "advanced past HEAD")
    git(ws, "push", "-q", "origin", "HEAD:refs/heads/zzz-carrier")

    # Divergent decoys sorting BEFORE the carrier: a scan pays for all of them.
    for i in range(NHEADS):
        git(ws, "checkout", "-q", "--detach", root)
        (ws / "decoy.py").write_text(f"decoy = {i}\n")
        git(ws, "add", "decoy.py")
        git(ws, "commit", "-q", "-m", f"decoy {i}")
        git(ws, "push", "-q", "origin", f"HEAD:refs/heads/aaa-decoy-{i:04d}")
    git(ws, "checkout", "-q", "--detach", head)
    git(ws, "clean", "-qfd")
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws


def test_complete_is_not_starved_before_or_after_the_durable_write(
    board, monkeypatch
):
    """Attribute the spawns to PRE-write and POST-write, and bound both.

    Severity depends on the split. Spawns before `write_txn` burn the caller's
    ceiling *before* the transition is durable, so the worker reaches no
    terminal state at all — the card's exact defect. Spawns after it are
    best-effort cleanup (`_cleanup_workspace` -> `remove_workspace_dir`, a
    SECOND `_remote_survivor` call site) and must not be able to starve the
    caller either, per the card's required outcome #3.

    Measured with the per-ref naming loop restored: 12 -> 48 spawns from 5 to
    41 refs in EACH phase, slope 1.000/ref, 712 s projected per phase at 7,939
    refs. Both now sit at the fixed bound regardless of ref count.
    """
    tid, ws = _card_contained_behind_a_tip(board)

    phase = ["pre"]
    spawns = {"pre": [], "post": []}
    real_git, real_txn = survivor._git, kb.write_txn

    def counting(repo, *args, **kwargs):
        spawns[phase[0]].append(args[0] if args else "?")
        return real_git(repo, *args, **kwargs)

    def marking_txn(conn):
        phase[0] = "post"
        return real_txn(conn)

    monkeypatch.setattr(survivor, "_git", counting)
    monkeypatch.setattr(kb, "write_txn", marking_txn)
    try:
        ok = kb.complete_task(
            board, tid, summary="done", metadata={"changed_files": ["impl.py"]},
        )
    finally:
        monkeypatch.setattr(survivor, "_git", real_git)
        monkeypatch.setattr(kb, "write_txn", real_txn)

    assert ok is True
    assert kb.get_task(board, tid).status == "done"
    assert len(spawns["pre"]) < SPAWN_CEILING, (
        f"{len(spawns['pre'])} git spawns BEFORE the durable write against "
        f"{NHEADS + 1} published heads — the ceiling burns before the "
        f"transition lands and the worker reaches no terminal state"
    )
    assert len(spawns["post"]) < SPAWN_CEILING, (
        f"{len(spawns['post'])} git spawns in post-write cleanup against "
        f"{NHEADS + 1} published heads — best-effort cleanup can starve the "
        f"caller (remove_workspace_dir is a second _remote_survivor call site)"
    )
    row = board.execute(
        "SELECT survivor, held_reason FROM task_workspace_survivors WHERE task_id = ?",
        (tid,),
    ).fetchone()
    assert row is not None and row["survivor"], "survivor row must still be recorded"
    assert row["held_reason"] is None
