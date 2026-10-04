"""Operator-token flag gate on the kanban CLI (spec 4.7 layer i, AC-A6).

``--takeover`` and ``--operator`` let a caller act on a card it does not own.
While a run is active on the card they are refused unless the process presents
``KANBAN_OPERATOR_TOKEN`` equal to the ``operator-token`` file next to the
board. A refusal appends a ``takeover_refused`` event carrying the caller pid
and argv. The one exemption is the dispatcher's own worker, identified by
``HERMES_KANBAN_OWNER_PID == os.getpid()`` exactly, never by env value alone or by
process ancestry.

The six AC-A6 arms, on the CLI path, against a temp ``HERMES_HOME``:

- A6-1 plain ``complete`` from a child that inherited the worker env: refused
  by the #1356 owner check, no flag so no ``takeover_refused``.
- A6-2 ``--takeover`` and A6-3 ``--operator`` with no token: refused, event.
- A6-4 a direct sqlite UPDATE: outside this guard by construction. The test
  records what the shim's reconciliation keys on: the row moved and no event.
- A6-5 a harness-authored script run as a child of a process holding the
  grant: env is inherited, the pid is not, so it is refused.
- A6-6 the token read: a same-uid process can read the file and pass. That is
  the known limit (Q15); the test asserts it so a future wall turns it red.

Also here: Q16 branch A (``unblock`` already resets ``consecutive_failures``)
and INV-A7 (``_default_spawn`` never hands the token to a worker).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TOKEN_ENV = "KANBAN_OPERATOR_TOKEN"
TOKEN_VALUE = "a" * 64

_WORKER_VARS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_OWNER_PID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_SESSION_ID",
    TOKEN_ENV,
)


def _base_env(home: Path) -> dict:
    env = os.environ.copy()
    for name in _WORKER_VARS:
        env.pop(name, None)
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(home / "workspaces")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run(home: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = _base_env(home)
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60,
    )


def _show(home: Path, task_id: str) -> dict:
    shown = _run(home, "show", task_id, "--json")
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


def _state(home: Path, task_id: str) -> tuple[str, int | None]:
    data = _show(home, task_id)
    open_runs = [r["id"] for r in data.get("runs") or [] if r.get("ended_at") is None]
    return data["task"]["status"], (open_runs[-1] if open_runs else None)


def _refusals(home: Path, task_id: str) -> list[dict]:
    return [e for e in _show(home, task_id).get("events") or [] if e["kind"] == "takeover_refused"]


def _db(home: Path) -> Path:
    return home / "kanban.db"


def _running_card(home: Path, title: str = "operator gate probe") -> tuple[str, int]:
    created = _run(home, "create", title, "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    claimed = _run(home, "claim", task_id)
    assert claimed.returncode == 0, claimed.stderr
    status, run_id = _state(home, task_id)
    assert status == "running" and run_id is not None
    return task_id, run_id


def _write_token(home: Path, value: str = TOKEN_VALUE) -> Path:
    path = home / "kanban" / "operator-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def _worker_env(task_id: str, run_id: int, owner: str) -> dict:
    return {
        "HERMES_KANBAN_TASK": task_id,
        "HERMES_KANBAN_RUN_ID": str(run_id),
        "HERMES_KANBAN_OWNER_PID": owner,
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes"
    h.mkdir()
    return h


# --- A6-1 plain ------------------------------------------------------------

def test_a6_1_plain_complete_from_non_owner_child_is_refused_without_takeover_event(home):
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, "complete", task_id, "--summary", "impostor",
               extra=_worker_env(task_id, run_id, str(os.getpid())))

    assert out.returncode != 0, out.stdout + out.stderr
    assert "owner grant" in out.stderr
    assert _state(home, task_id) == ("running", run_id)
    assert _refusals(home, task_id) == []  # no flag, so the flag gate had nothing to say


# --- A6-2 / A6-3 flags without the token -----------------------------------

@pytest.mark.parametrize("action,flag,value,extra_args", [
    ("complete", "--takeover", "harness says done", ("--summary", "x")),
    ("complete", "--operator", "Ace via harness: close it", ("--summary", "x")),
    ("block", "--takeover", "harness blocks", ("reason text",)),
    ("block", "--operator", "Ace via harness: block it", ("reason text",)),
])
def test_a6_2_3_takeover_or_operator_flag_refused_on_active_run(home, action, flag, value, extra_args):
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, action, task_id, *extra_args, flag, value)

    assert out.returncode != 0, out.stdout + out.stderr
    assert "KANBAN_OPERATOR_TOKEN" in out.stderr
    assert _state(home, task_id) == ("running", run_id)
    events = _refusals(home, task_id)
    assert len(events) == 1, events
    payload = events[0]["payload"]
    assert payload["action"] == action
    assert payload["flags"] == [flag]
    assert payload["task_ids"] == [task_id]
    assert "argv" not in payload
    assert isinstance(payload["caller_pid"], int) and payload["caller_pid"] > 0
    assert payload["caller_pid"] != os.getpid()  # the CLI subprocess, not the test
    assert payload["token"] == "absent"
    assert TOKEN_VALUE not in json.dumps(payload)


def test_takeover_refused_with_wrong_token_records_mismatch(home):
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, "complete", task_id, "--summary", "x", "--takeover", "r",
               extra={TOKEN_ENV: "b" * 64})

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    [event] = _refusals(home, task_id)
    assert event["payload"]["token"] == "mismatch"
    assert "b" * 64 not in json.dumps(event["payload"])


def test_takeover_refused_when_token_file_absent_even_with_env_token(home):
    task_id, run_id = _running_card(home)

    out = _run(home, "complete", task_id, "--summary", "x", "--takeover", "r",
               extra={TOKEN_ENV: TOKEN_VALUE})

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    [event] = _refusals(home, task_id)
    assert event["payload"]["token"] == "no_token_file"


def test_takeover_with_operator_token_completes(home):
    """Positive control: the operator presents the token inline."""
    task_id, _ = _running_card(home)
    _write_token(home)

    out = _run(home, "complete", task_id, "--summary", "operator close", "--takeover", "ruled",
               extra={TOKEN_ENV: TOKEN_VALUE})

    assert out.returncode == 0, out.stdout + out.stderr
    assert _state(home, task_id)[0] == "done"
    assert _refusals(home, task_id) == []


def test_takeover_without_active_run_is_not_gated(home):
    """Scope control: the gate bites only while a run is active."""
    created = _run(home, "create", "idle card", "--json")
    task_id = json.loads(created.stdout)["id"]
    _write_token(home)

    out = _run(home, "block", task_id, "idle block", "--takeover", "r")

    assert out.returncode == 0, out.stdout + out.stderr
    assert _refusals(home, task_id) == []
    unblocked = _run(home, "unblock", task_id, "--takeover", "r")
    assert unblocked.returncode == 0, unblocked.stdout + unblocked.stderr


def test_forged_worker_env_does_not_pass_flag_gate(home):
    """t_920c6b4a (FleetReview 80796f262c18): the env pair is caller-controlled.
    A process naming a live card and binding the ``pending`` grant to its own
    pid is NOT the dispatcher's worker: the card's run carries no worker_pid
    stamped for it, so the flag is refused."""
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, "complete", task_id, "--summary", "forged", "--takeover", "r",
               extra=_worker_env(task_id, run_id, "pending"))

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    assert len(_refusals(home, task_id)) == 1


def test_worker_env_without_owner_pid_does_not_pass_flag_gate(home):
    """Env value alone is not the grant: no owner pid means no exemption here."""
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, "complete", task_id, "--summary", "x", "--takeover", "r",
               extra={"HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": str(run_id)})

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    assert len(_refusals(home, task_id)) == 1


# --- A6-4 direct sqlite -----------------------------------------------------

def test_a6_4_direct_sqlite_update_is_outside_the_guard_and_leaves_no_event(home):
    """Not a guard arm: a raw UPDATE never touches the CLI. This records the
    evidence the shim's reconciliation (spec 4.5 DELTA 4) keys on instead:
    the status moved and no terminal event exists."""
    task_id, _ = _running_card(home)
    before = {e["kind"] for e in _show(home, task_id).get("events") or []}

    conn = sqlite3.connect(_db(home))
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    data = _show(home, task_id)
    assert data["task"]["status"] == "done"
    after = {e["kind"] for e in data.get("events") or []}
    assert after == before
    assert not ({"completed", "takeover_refused"} & after)


# --- A6-5 harness-authored test file ---------------------------------------

def test_a6_5_harness_script_child_of_grant_holder_is_refused(home, tmp_path):
    """The shim's env reaches the script; the shim's pid cannot."""
    task_id, run_id = _running_card(home)
    _write_token(home)
    script = tmp_path / "t.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'"{sys.executable}" -m hermes_cli.main kanban complete {task_id} --summary plain\n'
        "echo plain_rc=$?\n"
        f'"{sys.executable}" -m hermes_cli.main kanban complete {task_id} --summary flag --takeover r\n'
        "echo flag_rc=$?\n"
    )
    env = _base_env(home)
    # The grant names THIS pytest process: the script and its CLI children are not it.
    env.update(_worker_env(task_id, run_id, str(os.getpid())))

    out = subprocess.run(["sh", str(script)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)

    assert "plain_rc=1" in out.stdout, out.stdout + out.stderr
    assert "flag_rc=1" in out.stdout, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    # Both attempts stop at the #1356 owner check (it runs before the flag
    # gate, since the child names the ambient card), so no flag event here.
    assert out.stdout.count("does not hold its owner grant") == 0  # stderr, not stdout
    assert out.stderr.count("does not hold its owner grant") == 2, out.stderr


def test_a6_5_harness_script_flag_on_other_active_card_is_refused_by_token_gate(home, tmp_path):
    """Same script shape aimed at a card the inherited env does NOT name: the
    #1356 check does not apply, and the flag gate refuses by pid, not env."""
    task_id, run_id = _running_card(home)
    other_id, other_run = _running_card(home, "second unrelated active card")
    _write_token(home)
    script = tmp_path / "t.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'"{sys.executable}" -m hermes_cli.main kanban complete {other_id} --summary flag --takeover r\n'
        "echo flag_rc=$?\n"
    )
    env = _base_env(home)
    env.update(_worker_env(task_id, run_id, str(os.getpid())))

    out = subprocess.run(["sh", str(script)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)

    assert "flag_rc=1" in out.stdout, out.stdout + out.stderr
    assert _state(home, other_id) == ("running", other_run)
    [event] = _refusals(home, other_id)
    assert event["payload"]["flags"] == ["--takeover"]


# --- A6-6 token read: the recorded limit ------------------------------------

def test_a6_6_token_read_by_same_uid_process_passes_known_limit(home):
    """KNOWN LIMIT, not a green. A same-uid process can read the token file and
    present it; the gate is a tripwire, not a wall. Only a uid boundary (Q15)
    closes this, and when it lands this test must be inverted."""
    task_id, _ = _running_card(home)
    path = _write_token(home)

    stolen = path.read_text().strip()
    out = _run(home, "complete", task_id, "--summary", "x", "--takeover", "r",
               extra={TOKEN_ENV: stolen})

    assert out.returncode == 0, out.stdout + out.stderr
    assert _state(home, task_id)[0] == "done"


# --- Q16 branch A: unblock already resets the breaker ------------------------

def test_unblock_reset_failures_already_happens_q16_branch_a(home):
    """Spec 9.9 step 5: ``unblock`` already zeroes ``consecutive_failures``, so
    no ``--reset-failures`` flag is added."""
    created = _run(home, "create", "breaker card", "--json")
    task_id = json.loads(created.stdout)["id"]
    blocked = _run(home, "block", task_id, "quota: no eligible sub")
    assert blocked.returncode == 0, blocked.stderr
    conn = sqlite3.connect(_db(home))
    conn.execute("UPDATE tasks SET consecutive_failures=2 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    out = _run(home, "unblock", task_id)

    assert out.returncode == 0, out.stdout + out.stderr
    conn = sqlite3.connect(_db(home))
    status, failures = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    conn.close()
    assert (status, failures) == ("ready", 0)


# --- INV-A7: the dispatcher never hands the token to a worker ----------------

def test_default_spawn_strips_operator_token_from_worker_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = kb.Task(
        id="t_operator", title="slice", body=None, assignee="default",
        status="running", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="scratch",
        workspace_path=None, claim_lock=None, claim_expires=None, tenant=None,
    )
    with monkeypatch.context() as m:
        m.setattr("subprocess.Popen", _fake_popen)
        m.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
        m.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
        kbd._default_spawn(task, str(workspace))

    assert TOKEN_ENV not in captured["env"]
    assert os.environ[TOKEN_ENV] == TOKEN_VALUE  # stripped from the child only


# --- t_920c6b4a: FleetReview P1s on #1382 ------------------------------------

def test_expired_claim_on_running_card_is_still_gated(home):
    """9094c8989646: a live worker's claim lapses inside one long LLM call
    until the next sweep extends it; the card is still ``running``."""
    task_id, run_id = _running_card(home)
    _write_token(home)
    conn = sqlite3.connect(_db(home))
    conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    out = _run(home, "complete", task_id, "--summary", "x", "--takeover", "r")

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    assert len(_refusals(home, task_id)) == 1


def test_schedule_takeover_on_active_run_is_gated(home):
    """76fd55c87bb2: ``schedule_task`` ends the live run, so it is gated too."""
    task_id, run_id = _running_card(home)
    _write_token(home)

    out = _run(home, "schedule", task_id, "later", "--takeover", "r")

    assert out.returncode != 0, out.stdout + out.stderr
    assert _state(home, task_id) == ("running", run_id)
    [event] = _refusals(home, task_id)
    assert event["payload"]["action"] == "schedule"


def test_refusal_event_records_no_argument_values(home):
    """fc8a7bdcf814: ``--summary``/``--metadata`` values never reach the event."""
    task_id, _ = _running_card(home)
    _write_token(home)
    marker = "sk-live-" + "z" * 24

    out = _run(home, "complete", task_id, "--summary", marker,
               "--metadata", json.dumps({"k": marker}), "--takeover", "r")

    assert out.returncode != 0, out.stdout + out.stderr
    [event] = _refusals(home, task_id)
    assert marker not in json.dumps(event["payload"])
    assert set(event["payload"]) == {"action", "flags", "task_ids", "caller_pid", "token"}


@pytest.fixture
def inproc(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    h = tmp_path / "hermes"
    h.mkdir()
    for name in _WORKER_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    _write_token(h)
    with kb.connect_closing() as conn:
        yield kb, conn


def _claimed(kb, conn) -> tuple[str, int]:
    tid = kb.create_task(conn, title="gate probe", assignee="daedalus")
    kb.claim_task(conn, tid)
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    return tid, int(row["current_run_id"])


def test_worker_grant_needs_the_dispatcher_stamped_pid(inproc, monkeypatch):
    """80796f262c18 positive control: the grant holds only when the card's
    stamped ``worker_pid`` IS this process."""
    kb, conn = inproc
    tid, run_id = _claimed(kb, conn)
    for k, v in _worker_env(tid, run_id, str(os.getpid())).items():
        monkeypatch.setenv(k, v)

    assert kb._caller_holds_grant_for(conn, tid) is False  # nothing stamped
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid() + 1, tid))
    conn.commit()
    assert kb._caller_holds_grant_for(conn, tid) is False  # someone else's pid
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
    conn.commit()
    assert kb._caller_holds_grant_for(conn, tid) is True
    kb.enforce_operator_flag_gate(conn, [tid], "complete", flags=["--takeover"])


def test_gate_is_rechecked_inside_the_mutation_txn(inproc):
    """c12b9f27d054: the preflight passes on an idle card, the dispatcher then
    claims it, and the handler's mutation must still be refused."""
    kb, conn = inproc
    tid = kb.create_task(conn, title="idle then claimed", assignee="daedalus")
    kb.enforce_operator_flag_gate(conn, [tid], "complete", flags=["--takeover"])  # idle: passes
    kb.claim_task(conn, tid)  # the race: a run lands between preflight and mutation

    with pytest.raises(kb.OperatorTokenRequiredError):
        with kb.operator_flag_gate_scope([tid], "complete", flags=["--takeover"]):
            kb.complete_task(conn, tid, summary="tokenless override")

    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running" and row["current_run_id"] is not None
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
    assert kinds.count("takeover_refused") == 1
    assert "completed" not in kinds


# --- t_920c6b4a round 2: FleetReview P1s on #1431 ----------------------------

def test_recheck_stays_armed_after_a_caught_refusal(inproc):
    """d292b198bb11: a caller that swallows the first refusal and writes again
    is refused again, and both refusals are audited."""
    kb, conn = inproc
    tid, _ = _claimed(kb, conn)

    with kb.operator_flag_gate_scope([tid], "complete", flags=["--takeover"]):
        for _ in range(2):
            with pytest.raises(kb.OperatorTokenRequiredError):
                kb.complete_task(conn, tid, summary="retry after catching")

    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
    assert kinds.count("takeover_refused") == 2


def test_reclaim_is_authorized_before_the_worker_is_signalled(inproc):
    """946120c1ae67: the card is claimed after the preflight; the tokenless
    reclaim must be refused BEFORE any signal reaches the new worker."""
    kb, conn = inproc
    tid = kb.create_task(conn, title="idle then claimed", assignee="daedalus")
    kb.enforce_operator_flag_gate(conn, [tid], "reclaim", flags=["--operator"])  # idle: passes
    kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid() + 7, tid))
    conn.commit()
    signals = []

    with pytest.raises(kb.OperatorTokenRequiredError):
        with kb.operator_flag_gate_scope([tid], "reclaim", flags=["--operator"]):
            kb.reclaim_task(conn, tid, operator="x", signal_fn=lambda *a, **k: signals.append(a))

    assert signals == []
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running" and row["current_run_id"] is not None


def test_lost_refusal_audit_is_logged_not_swallowed(inproc, monkeypatch, caplog):
    """667f8318dd40: if the audit write fails the refusal still stands and the
    failure is logged."""
    kb, conn = inproc
    tid, _ = _claimed(kb, conn)

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    with caplog.at_level("WARNING"):
        with pytest.raises(kb.OperatorTokenRequiredError):
            with kb.operator_flag_gate_scope([tid], "complete", flags=["--takeover"]):
                monkeypatch.setattr(kb, "connect_closing", _boom)
                kb.authorize_pending_operator_gate(conn)

    assert "takeover_refused audit NOT recorded" in caplog.text
