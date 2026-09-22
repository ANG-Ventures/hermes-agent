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
SPAWN_CEILING = 30


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
        f"published heads — a per-ref scan is back"
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
