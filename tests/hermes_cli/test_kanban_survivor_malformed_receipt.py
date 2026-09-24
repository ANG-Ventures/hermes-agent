"""A malformed RECORDED landed receipt must HOLD on reclamation, never go silent.

Reclamation re-verifies a `kind: "landed"` receipt's live commits before it may
delete the workspace. The receipt is persisted JSON; a hand-edited, partially
written or legacy row (`landed: null`, a non-object entry, an entry without
`sha`, `refs: null` while a recorded repository is missing) used to raise
`TypeError`/`KeyError` out of `preserve()` and `remove_workspace_dir()`. The
workspace survived, but with no `held_reason` and no `workspace_held` event --
the card's recovery state was silent (Argus r4 P10/P12 on #924, t_ad32cdab).

The contract pinned here: every malformed shape HOLDs through `_hold()` with a
meaningful persisted reason and a `workspace_held` event, on both deletion
entry points, and a well-formed receipt still reclaims.
"""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as ks


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *map(str, args)], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def landed_task(conn, tmp_path):
    """A card whose completion recorded a VALID landed receipt; workspace kept."""
    tid = kb.create_task(conn, title="landed receipt")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    repo = ws / "w"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Worker")
    git(repo, "config", "user.email", "worker@example.invalid")
    (repo / "w.py").write_text("implementation = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "impl")
    sha = git(repo, "rev-parse", "HEAD")
    live = tmp_path / "live-w"
    git(tmp_path, "clone", "--no-local", repo, live)
    kb.set_workspace_path(conn, tid, ws)
    ks.record_baseline(conn, tid, ws)
    receipt = ks.preserve(conn, tid, {
        "changed_files": ["w/w.py"], "landed": [{"repo_path": str(live), "sha": sha}],
    })
    assert receipt["kind"] == "landed", receipt
    return tid, ws


def corrupt(conn, tid, shape):
    row = conn.execute(
        "SELECT bases, survivor FROM task_workspace_survivors WHERE task_id = ?", (tid,),
    ).fetchone()
    bases, survivor = json.loads(row[0]), json.loads(row[1])
    if shape == "landed_null":
        survivor["landed"] = None
    elif shape == "landed_not_list":
        survivor["landed"] = {"repository": survivor["landed"][0]["repository"]}
    elif shape == "landed_empty":
        survivor["landed"] = []
    elif shape == "entry_junk":
        survivor["landed"] = ["junk"]
    elif shape == "entry_no_sha":
        survivor["landed"][0].pop("sha")
    elif shape == "entry_no_repository":
        survivor["landed"][0].pop("repository")
    elif shape == "refs_null_with_missing_repo":
        # A recorded repository has vanished, so reclamation must consult the
        # recorded refs -- which are `null`. Before the fix: TypeError.
        survivor["refs"] = None
        bases["gone"] = "0" * 40
    else:  # pragma: no cover - test typo guard
        raise AssertionError(shape)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_workspace_survivors SET bases = ?, survivor = ? WHERE task_id = ?",
            (json.dumps(bases), json.dumps(survivor), tid),
        )


def held_events(conn, tid):
    return [json.loads(r[0]).get("reason") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'workspace_held' ORDER BY id",
        (tid,),
    )]


def reclaim(conn, tid, ws, entry):
    if entry == "remove_workspace_dir":
        return ks.remove_workspace_dir(conn, tid, ws)
    return kb.safe_remove_workspace_dir(ws, task_id=tid, reason="test-reclaim", conn=conn)


ENTRY_POINTS = ["remove_workspace_dir", "safe_remove_workspace_dir"]

MALFORMED = {
    "landed_null": "recorded landed receipt is malformed",
    "landed_not_list": "recorded landed receipt is malformed",
    "landed_empty": "recorded landed receipt is malformed",
    "entry_junk": "recorded landed receipt is malformed",
    "entry_no_sha": "recorded landed receipt is malformed",
    "entry_no_repository": "recorded landed receipt is malformed",
    "refs_null_with_missing_repo": "recorded repository missing",
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_malformed_recorded_landed_receipt_holds_with_a_persisted_reason(board, tmp_path, shape, entry):
    tid, ws = landed_task(board, tmp_path)
    corrupt(board, tid, shape)

    assert reclaim(board, tid, ws, entry) is False
    assert ws.exists() and (ws / "w" / "w.py").exists()
    held = ks._state(board, tid)[1]
    assert held and held.startswith("survivor_unavailable:"), held
    assert MALFORMED[shape] in held, held
    reasons = held_events(board, tid)
    assert reasons and MALFORMED[shape] in reasons[-1], reasons


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_valid_recorded_landed_receipt_still_reclaims(board, tmp_path, entry):
    tid, ws = landed_task(board, tmp_path)
    assert reclaim(board, tid, ws, entry) is True
    assert not ws.exists()
    assert ks._state(board, tid)[1] is None
    assert held_events(board, tid) == []


@pytest.mark.parametrize("exc", [TypeError, KeyError, AttributeError, IndexError])
def test_unvalidated_record_shape_is_held_by_the_backstop(board, tmp_path, monkeypatch, exc):
    """Any future unvalidated read of the persisted row still fails closed.

    The explicit validators cover the shapes known today; the backstop in
    `preserve()` is what keeps the NEXT unvalidated index from escaping
    `_hold()` again.
    """
    tid, ws = landed_task(board, tmp_path)

    def boom(previous):
        raise exc("synthetic malformed row")

    monkeypatch.setattr(ks, "_recorded_landed_claims", boom)
    assert ks.remove_workspace_dir(board, tid, ws) is False
    assert ws.exists()
    held = ks._state(board, tid)[1]
    assert held == ("survivor_unavailable: recorded survivor state is malformed "
                    f"({exc.__name__}); repair the row or recover the workspace by hand")
    assert held in held_events(board, tid)
