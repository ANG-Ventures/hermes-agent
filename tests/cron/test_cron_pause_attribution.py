"""A pause with no reason names its caller (t_9da8a02b).

The 11-03 home-reconcile-tick pause left ``paused_reason`` empty-ish and a journal actor of
``hermes:<pid>``, a pid nobody could trace afterwards. A reasonless pause now records the kanban card,
the session and the parent process command line, in the row and in the lifecycle journal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_CHILD = """
from cron.jobs import create_job, pause_job
job = create_job(prompt="p", schedule="every 5m", name="w", deliver="local")
print(pause_job(job["id"])["id"])
"""


@pytest.mark.platforms("posix")
def test_reasonless_pause_names_card_session_and_parent(tmp_path):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in {"HERMES_KANBAN_TASK", "HERMES_SESSION_ID"}}
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_TASK="t_test", HERMES_SESSION_ID="sess_test",
               PYTHONPATH=str(_REPO))
    child = tmp_path / "child.py"
    child.write_text(_CHILD)
    # A real parent whose command line carries a marker: sh forks python (a trailing command defeats exec).
    parent = f'"{sys.executable}" "{child}" ; : parent-marker-9da8'
    out = subprocess.run(["/bin/sh", "-c", parent], env=env, cwd=_REPO, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    job_id = out.stdout.strip().splitlines()[-1]

    row = next(j for j in json.loads((home / "cron" / "jobs.json").read_text())["jobs"]
               if j["id"] == job_id)
    paused = [json.loads(ln) for ln in (home / "cron" / "lifecycle.jsonl").read_text().splitlines()
              if json.loads(ln).get("event") == "paused"]
    assert len(paused) == 1
    for text in (row["paused_reason"], paused[0]["reason"]):
        assert "t_test" in text
        assert "sess_test" in text
        assert "parent-marker-9da8" in text
        assert f"hermes cron resume {job_id}" in text


def test_explicit_reason_is_kept_verbatim(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    job = cron.jobs.create_job(prompt="p", schedule="every 5m", name="w", deliver="local")
    assert cron.jobs.pause_job(job["id"], reason="until #123 lands")["paused_reason"] == "until #123 lands"
