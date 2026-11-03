"""A pause with no reason names its caller (t_9da8a02b).

The 11-03 home-reconcile-tick pause left ``paused_reason`` empty-ish and a journal actor of
``hermes:<pid>``, a pid nobody could trace afterwards. A reasonless pause now records the kanban card,
the session and the parent process (executable + script basename only: argv can carry secrets), in the row
and in the lifecycle journal.
"""

from __future__ import annotations

import json
import os
import re
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
    # A real parent whose -c string carries a secret: bash forks python (a trailing command defeats exec).
    parent = f'export X_TOKEN=s3cret; "{sys.executable}" "{child}" ; : parent-marker-9da8'
    out = subprocess.run(["/bin/bash", "-c", parent], env=env, cwd=_REPO, capture_output=True,
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
        assert re.search(r"parent pid \d+: bash;", text), text
        for leaked in ("s3cret", "X_TOKEN", "parent-marker-9da8"):
            assert leaked not in text
        assert f"hermes cron resume {job_id}" in text


@pytest.mark.parametrize("argv,label", [
    (["python3", "/path/to/tick.py", "--x", "y"], "python3 tick.py"),
    (["/bin/bash", "-lc", "export X_TOKEN=s3cret; hermes cron pause J"], "bash"),
    (["/opt/py/bin/python3.13", "-u", "-c", "import os; T='s3cret'"], "python3.13"),
    (["zsh", "/Users/x/land-8694.sh", "--token", "s3cret"], "zsh land-8694.sh"),
    (["/usr/local/bin/hermes", "cron", "pause", "J", "--api-key", "s3cret"], "hermes"),
    (["bash", "-e", "/x/land-8694.sh", "s3cret"], "bash land-8694.sh"),
    # Script on stdin: every later argv is a positional for the piped script.
    (["bash", "-s", "--", "--token", "s3cret"], "bash"),
    (["sh", "-s", "s3cret"], "sh"),
    (["python3", "-", "s3cret"], "python3"),
    # Options whose next argv is a value, never a script.
    (["bash", "-o", "s3cret", "x.sh"], "bash"),
    (["bash", "--rcfile", "s3cret", "x.sh"], "bash"),
    (["python3", "-W", "s3cret", "x.py"], "python3"),
    (["python3", "-X", "s3cret", "x.py"], "python3"),
    (["python3", "-m", "s3cret"], "python3"),
    # A non-script-looking first positional is not recorded either.
    (["bash", "/x/s3cret"], "bash"),
])
def test_parent_label_keeps_no_argv_content(argv, label):
    from cron.lifecycle_journal import parent_label

    assert parent_label(argv) == label


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
