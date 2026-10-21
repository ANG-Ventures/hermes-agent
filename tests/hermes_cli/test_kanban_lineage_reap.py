"""K6 lineage tag: a delegated child's tool process is reaped with its worker's leftovers.

Option C (docs/dev/delegate-background-on-kanban-runtime.md §6 K6, card t_7f2f8ecf). A worker's
in-process ``delegate_task`` child runs its tools with ``scrub_kanban_env`` applied, which drops
``HERMES_KANBAN_TASK``/``RUN_ID``; the escapee reaper matched only those, so such a tool process
that ``setsid()``s outlived the run. The dispatcher now stamps ``HERMES_KANBAN_LINEAGE=<task>:<run>``
on every worker; the scrub keeps it and the reaper matches it, still bounded by the run's birth window.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.delegation_context import scrub_kanban_env
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

pytestmark = [
    pytest.mark.platforms("posix"),
    # Escapees are reparented to init: outside the test's subtree by construction,
    # but still only processes this test started.
    pytest.mark.live_system_guard_bypass,
]


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _tool_process(task_id: str, run_id: str) -> subprocess.Popen:
    """What a delegated child's terminal tool leaves behind: the worker's env after
    ``scrub_kanban_env``, in its OWN session."""
    worker_env = {**os.environ, "HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": run_id,
                  kb.KANBAN_LINEAGE_ENV: f"{task_id}:{run_id}"}
    env = scrub_kanban_env(worker_env)
    assert "HERMES_KANBAN_TASK" not in env and "HERMES_KANBAN_RUN_ID" not in env
    p = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(120)"],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env,
                         start_new_session=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "ready"
    return p


def _kill(*procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_scrub_keeps_the_lineage_tag():
    env = scrub_kanban_env({"HERMES_KANBAN_TASK": "t_x", "HERMES_KANBAN_RUN_ID": "1",
                            kb.KANBAN_LINEAGE_ENV: "t_x:1"})
    assert env[kb.KANBAN_LINEAGE_ENV] == "t_x:1"


def test_tool_process_carrying_only_the_lineage_tag_is_reaped():
    born_after = time.time() - 5
    victim = _tool_process("t_lineage1", "4242")
    bystander = _tool_process("t_lineage1", "4243")  # same card, a newer run: not ours
    try:
        n = kb._reap_run_env_escapees("t_lineage1", 4242, born_after=born_after,
                                      born_before=time.time(), grace=2.0)
        assert n == 1
        assert _gone(victim.pid), "a lineage-tagged escapee outlived its run"
        assert not _gone(bystander.pid, timeout=0.5), "another run's process was signalled"
    finally:
        _kill(victim, bystander)


def test_lineage_escapee_born_outside_the_window_is_not_reaped():
    victim = _tool_process("t_lineage2", "7")
    try:
        assert kb._reap_run_env_escapees("t_lineage2", 7, born_after=1.0, born_before=2.0) == 0
        assert not _gone(victim.pid, timeout=0.5)
    finally:
        _kill(victim)


_CAPTURE = r'''
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root = Path(sys.argv[2]); home = root / ".hermes"; home.mkdir(exist_ok=True)
for k in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
    del os.environ[k]
os.environ.update(HERMES_HOME=str(home), HERMES_KANBAN_SANDBOX="1", HOME=str(root))
from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
kb.init_db(); kb.create_board("projects")
kbd._resolve_hermes_argv = lambda: ["hermes"]
got = {}
class P: pid = 4242
def fake(cmd, *a, **kw):
    got.update(cmd=list(cmd), env=dict(kw.get("env") or {})); return P()
subprocess.Popen = fake
ws = root / "ws"; ws.mkdir(exist_ok=True)
t = kb.Task(id="t_negdiff", title="x", body=None, assignee="worker", status="running", priority=0,
            created_by="t", created_at=1, started_at=None, completed_at=None, workspace_kind="dir",
            workspace_path=str(ws), claim_lock="l", claim_expires=None, tenant=None, current_run_id=11)
kbd._default_spawn(t, str(ws), board="projects")
print(json.dumps(got))
'''


def _capture(tree: Path, root: Path) -> dict:
    out = subprocess.run([sys.executable, "-c", _CAPTURE, str(tree), str(root)], check=True,
                         capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
    return __import__("json").loads(out.stdout.strip().splitlines()[-1])


def test_normal_board_spawn_differs_from_base_by_exactly_the_tag(tmp_path):
    """Spec §9 P1 negative: argv identical, env = base env + HERMES_KANBAN_LINEAGE only.
    Base = this tree's merge-base with main, checked out as a detached worktree."""
    repo = Path(__file__).resolve().parents[2]
    git = ["git", "-C", str(repo)]
    # The closest merge-base among the main refs present (a checkout may carry an
    # upstream remote beside the fork's): the newest by commit time.
    bases = []
    for ref in ("origin/main", "fork/main", "main"):
        sha = subprocess.run([*git, "merge-base", "HEAD", ref], capture_output=True,
                             text=True).stdout.strip()
        if sha:
            ts = subprocess.run([*git, "show", "-s", "--format=%ct", sha], capture_output=True,
                                text=True).stdout.strip()
            bases.append((int(ts or 0), sha))
    if not bases:
        pytest.skip("no merge-base with main (shallow clone)")
    base_sha = max(bases)[1]
    base_tree = tmp_path / "base"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(base_tree), base_sha], check=True)
    try:
        (tmp_path / "rb").mkdir()
        (tmp_path / "rh").mkdir()
        base = _capture(base_tree, tmp_path / "rb")
        head = _capture(repo, tmp_path / "rh")
    finally:
        subprocess.run([*git, "worktree", "remove", "--force", str(base_tree)], check=False)
    if kb.KANBAN_LINEAGE_ENV in base["env"]:
        pytest.skip("base already carries the tag (this PR has landed)")

    def norm(env, root):
        return {k: v.replace(str(root), "<ROOT>") for k, v in env.items()}

    assert head["cmd"] == base["cmd"]
    b, h = norm(base["env"], tmp_path / "rb"), norm(head["env"], tmp_path / "rh")
    assert {k: v for k, v in h.items() if k != kb.KANBAN_LINEAGE_ENV} == b
    assert h[kb.KANBAN_LINEAGE_ENV] == "t_negdiff:11"


def test_dispatcher_stamps_the_tag_on_every_worker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    got = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *a, **kw):
        got["env"] = dict(kw.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = kb.Task(id="t_tagged", title="x", body=None, assignee="worker", status="running",
                   priority=0, created_by="t", created_at=1, started_at=None, completed_at=None,
                   workspace_kind="scratch", workspace_path=None, claim_lock="l",
                   claim_expires=None, tenant=None, current_run_id=11)
    kbd._default_spawn(task, str(tmp_path))
    assert got["env"][kb.KANBAN_LINEAGE_ENV] == "t_tagged:11"
