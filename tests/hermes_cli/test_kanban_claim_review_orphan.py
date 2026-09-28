"""A CLI ``claim --review`` must never strand its card in ``running`` (t_c3cf232e).

Incident 2026-09-27 (t_887f9584, run 13727): ``hermes kanban claim <id> --review``
from a sessionless shell opened a review run with ``claim_lock=<host>:<cli pid>``
and no worker pid. The CLI exited at once, but ``reclaim`` / ``reassign --reclaim``
were refused ``liveness_unprovable`` and the dispatcher kept deferring
(``ttl_expired_worker_alive``): the dead-claimer path held it for the whole
launch bound meant for a dispatcher that died mid-spawn, although a CLI claim
never spawns anything. ``request-changes`` was refused too (sessionless claim).

These tests run the claim in a REAL subprocess that exits, as in the incident.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

REPO = Path(__file__).resolve().parents[2]
SESSION = "20260927_200000_operator"

_SCRUB_PREFIXES = ("HERMES_KANBAN",)
_SCRUB_KEYS = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT",
    "_HERMES_GATEWAY", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACE",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith(_SCRUB_PREFIXES):
            monkeypatch.delenv(key)
    for key in _SCRUB_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return tmp_path


def _cli(home_dir: Path, rest: str, *, session=None) -> subprocess.CompletedProcess:
    """Run ``/kanban <rest>`` in a fresh interpreter that exits when done."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_SCRUB_PREFIXES) and k not in _SCRUB_KEYS}
    env.update({
        "HERMES_HOME": str(home_dir / "hermes"),
        "HOME": str(home_dir),
        "PYTHONPATH": str(REPO),
    })
    if session:
        env["HERMES_SESSION_ID"] = session
    code = (
        "import sys\n"
        "from hermes_cli import kanban as cli\n"
        f"sys.stdout.write(cli.run_slash({rest!r}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(REPO),
        capture_output=True, text=True, timeout=120,
    )


def _parked_review(session_id=None) -> str:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="orphan review", assignee="builder",
                             session_id=session_id)
        impl = kb.claim_task(conn, tid)
        assert impl
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
        assert kb.get_task(conn, tid).status == "review"
    return tid


def test_exited_cli_review_claim_is_reclaimable_at_once(home):
    tid = _parked_review()
    proc = _cli(home, f"claim {tid} --review", session=SESSION)
    assert "Claimed" in proc.stdout, (proc.stdout, proc.stderr)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.worker_pid is None
        run_id = task.current_run_id
        claimed = [e for e in kb.list_events(conn, tid)
                   if e.kind == "claimed" and e.run_id == run_id]
        assert claimed[-1].payload.get("operator_claim") is True

    # The claiming CLI process has exited; its pid is the whole claimant.
    proc = _cli(home, f"reclaim {tid} --reason orphaned-cli-claim")
    assert "Reclaimed" in proc.stdout, (proc.stdout, proc.stderr)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.claim_lock is None
        kinds = [e.kind for e in kb.list_events(conn, tid) if e.run_id == run_id]
        assert "reclaim_refused" not in kinds
        assert "reclaimed" in kinds


def test_expired_exited_cli_review_claim_is_released_by_ttl_sweep(home):
    tid = _parked_review()
    proc = _cli(home, f"claim {tid} --review", session=SESSION)
    assert "Claimed" in proc.stdout, (proc.stdout, proc.stderr)
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
        conn.commit()
        assert kb.release_stale_claims(conn) == 1
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.claim_lock is None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "reclaim_deferred" not in kinds


def test_claiming_session_can_request_changes_from_a_later_process(home):
    tid = _parked_review(session_id=SESSION)
    assert "Claimed" in _cli(home, f"claim {tid} --review", session=SESSION).stdout
    coverage = json.dumps({
        "lenses": {k: "done" for k in REQUIRED_REVIEW_LENSES},
        "findings": 1, "items": ["Missing guard at handler:42"],
        "review_minutes": 5, "batch_id": "batch-orphan-1",
        "head_sha": "n/a: fixture card has no PR",
    })
    proc = _cli(
        home,
        f'request-changes {tid} "BEHAVIOUR: add the guard" --coverage {shlex.quote(coverage)}',
        session=SESSION,
    )
    assert "Requested changes" in proc.stdout, (proc.stdout, proc.stderr)
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_sessionless_cli_review_claim_is_refused_not_stranded(home):
    tid = _parked_review()
    proc = _cli(home, f"claim {tid} --review")
    assert proc.returncode == 0
    assert "Claimed" not in proc.stdout
    assert "no session identity" in proc.stdout, (proc.stdout, proc.stderr)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.claim_lock is None


def test_dispatcher_review_claim_with_dead_claimer_still_held(home):
    """The launch-bound hold for a dispatcher that died mid-spawn is unchanged:
    only an explicit operator claim is released at once."""
    tid = _parked_review()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        assert kb.claim_review_task(conn, tid, claimer=f"{host}:{dead.pid}")
        assert kb.reclaim_task(conn, tid, reason="probe") is False
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        refused = [e for e in kb.list_events(conn, tid) if e.kind == "reclaim_refused"]
        assert refused[-1].payload["dead_claimer_release_basis"] == "launch_bound"
