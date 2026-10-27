"""A cron attempt whose owner died never reached ``_finalize_cron_session``, so its
``cron_<job>_*`` state.db row kept ``ended_at IS NULL`` forever and read as a live model pin
(t_fd68626f: cron_5149d33d7133_20261025_202811 on retired claude-haiku-4-5). Recovering the
ledger row as ``unknown`` now ends that session row too, bounded to the attempt's own window.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pytest

import cron.executions as executions_mod
import hermes_state
from hermes_state import SessionDB


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    db = SessionDB(db_path=db_path)
    yield db
    db.close()


def _dead_pid() -> int:
    out = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _attempt(job_id: str, *, age_s: float, owner: str = "dead") -> dict:
    record = executions_mod.create_execution(job_id, source="builtin")
    claimed_at = (executions_mod._hermes_now() - timedelta(seconds=age_s)).isoformat()
    pid = _dead_pid() if owner == "dead" else record["pid"]
    with executions_mod._transaction() as conn:
        conn.execute("UPDATE executions SET status='running', process_id=?, pid=?, "
                     "process_started_at=NULL, claimed_at=? WHERE id=?",
                     (f"{owner}-proc", pid, claimed_at, record["id"]))
    return executions_mod.get_execution(record["id"])


def _session(db: SessionDB, sid: str, *, started_at: float) -> None:
    db.create_session(sid, "cron", model="claude-haiku-4-5")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET started_at=? WHERE id=?", (started_at, sid)))


def _ended(db: SessionDB, sid: str):
    row = db._read_one("SELECT ended_at, end_reason FROM sessions WHERE id=?", (sid,))
    return row["ended_at"], row["end_reason"]


def test_dead_owner_recovery_ends_its_cron_session(env):
    from datetime import datetime
    attempt = _attempt("5149d33d7133", age_s=600)
    start = datetime.fromisoformat(attempt["claimed_at"]).timestamp()
    _session(env, "cron_5149d33d7133_20261025_202811", started_at=start + 1)
    # Another job's open row in the same window is not this attempt's session.
    _session(env, "cron_aaaaaaaaaaaa_20261025_202811", started_at=start + 1)

    assert executions_mod.recover_interrupted_executions() == 1

    ended_at, reason = _ended(env, "cron_5149d33d7133_20261025_202811")
    assert ended_at is not None and reason == "cron_orphaned"
    assert _ended(env, "cron_aaaaaaaaaaaa_20261025_202811") == (None, None)


def test_session_of_a_later_attempt_of_the_same_job_is_spared(env):
    from datetime import datetime
    dead = _attempt("jobx", age_s=3600)
    later = executions_mod.create_execution("jobx", source="builtin")  # this process: live owner
    dead_start = datetime.fromisoformat(dead["claimed_at"]).timestamp()
    later_start = datetime.fromisoformat(later["claimed_at"]).timestamp()
    _session(env, "cron_jobx_20261025_190000", started_at=dead_start + 1)
    _session(env, "cron_jobx_20261025_200000", started_at=later_start + 1)

    assert executions_mod.recover_interrupted_executions() == 1

    assert _ended(env, "cron_jobx_20261025_190000")[1] == "cron_orphaned"
    assert _ended(env, "cron_jobx_20261025_200000") == (None, None)


def test_already_finalized_session_keeps_its_reason(env):
    from datetime import datetime
    attempt = _attempt("jobf", age_s=600)
    sid = "cron_jobf_20261025_202811"
    _session(env, sid, started_at=datetime.fromisoformat(attempt["claimed_at"]).timestamp() + 1)
    env.end_session(sid, "cron_complete")

    executions_mod.recover_interrupted_executions()

    assert _ended(env, sid)[1] == "cron_complete"


def test_terminalize_dead_owner_also_ends_the_session(env):
    from datetime import datetime
    attempt = _attempt("jobt", age_s=60)
    sid = "cron_jobt_20261025_202811"
    _session(env, sid, started_at=datetime.fromisoformat(attempt["claimed_at"]).timestamp() + 1)

    assert executions_mod.terminalize_dead_owner(attempt["id"], reason="worker exited 137")

    assert _ended(env, sid)[1] == "cron_orphaned"


def test_session_store_failure_does_not_break_ledger_recovery(env, monkeypatch):
    monkeypatch.setattr(SessionDB, "end_orphaned_cron_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    attempt = _attempt("jobe", age_s=600)

    assert executions_mod.recover_interrupted_executions() == 1
    assert executions_mod.get_execution(attempt["id"])["status"] == "unknown"
