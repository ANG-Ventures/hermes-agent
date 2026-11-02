"""Durable work obligations (spec "Durable Work Ownership" v1.7, card P1-fork t_19dc7f6d).

Contract tests for ``hermes_cli/kanban_work_obligations.py`` on a real sqlite board: the §4.1
record, the §4.3 state machine with an injected clock, the binding contract (P6-B1, P6-RC4,
P6-RC5), the one-transaction supersede (P6-RC8), resolve authority + receipts (P6-RC9), a
two-process CAS race, and a CLI E2E on the ``obligations-drill`` board.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_work_obligations as ob

BOARD = "obligations-drill"
T0 = 1_793_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_MARKERS = ob.WORKER_ENV_MARKERS + ("HERMES_KANBAN_DB", "HERMES_KANBAN_OWNER_PID")

LANDER = "bash ~/.hermes/scripts/fleet-merge.sh ANG-Ventures/hermes-home 8012 --card t_x\n"
WATCHER = "python3 ~/.hermes/scripts/wait-land-ci.py --repo ANG-Ventures/hermes-home --pr 8012\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for m in WORKER_MARKERS + ("OBLIGATIONS",):
        monkeypatch.delenv(m, raising=False)
    kb.create_board(BOARD)
    return h


@pytest.fixture
def conn(home):
    c = kbc.connect(board=BOARD)
    yield c
    c.close()


def _card(conn, title="lander decision", session=None) -> str:
    tid = kb.create_task(conn, title=title, assignee="nobody-drill")
    if session is not None:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session, tid))
        conn.commit()
    return tid


def _events(conn, tid) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind LIKE 'obligation_%' ORDER BY id", (tid,))]


def _operator_state_db(home: Path, *session_ids: str) -> None:
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, session_key TEXT,"
               " parent_session_id TEXT, started_at REAL)")
    for sid in session_ids:
        db.execute("INSERT OR IGNORE INTO sessions (id, source, session_key) VALUES (?, 'discord', ?)",
                   (sid, f"key-{sid}"))
    db.commit()
    db.close()


# --- §4.1 record ------------------------------------------------------------------------------

def test_open_is_idempotent_per_source_key_even_after_resolve(conn):
    tid = _card(conn)
    a, created = ob.open_obligation(conn, tid, "detached:land-8012d-111", created_at=T0, now=T0 + 5)
    assert created and a.state == ob.OPEN_UNCLAIMED and a.cls == "decision"
    assert a.state_since == T0 and a.created_at == T0  # the EVENT time, not insert time
    again, created2 = ob.open_obligation(conn, tid, "detached:land-8012d-111", now=T0 + 9)
    assert not created2 and again.id == a.id and again.holder_gen == a.holder_gen
    ob.resolve(conn, a.id, "card-closed", now=T0 + 20)
    reopened, created3 = ob.open_obligation(conn, tid, "detached:land-8012d-111", now=T0 + 30)
    assert not created3 and reopened.state == ob.RESOLVED
    assert len(ob.list_obligations(conn, task_id=tid)) == 1
    assert _events(conn, tid) == ["obligation_opened", "obligation_resolved"]


def test_two_source_keys_are_two_rows_and_resolve_independently(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:land-8012d-1", now=T0)
    b, _ = ob.open_obligation(conn, tid, "detached:land-8012f-2", now=T0)
    ob.resolve(conn, a.id, "merged:abc123", now=T0 + 1)
    assert [o.id for o in ob.list_obligations(conn, task_id=tid, open_only=True)] == [b.id]


def test_source_key_shape_and_class_are_pinned_at_open(conn):
    tid = _card(conn)
    assert ob.open_obligation(conn, tid, "review:991", now=T0)[0].cls == "review"
    with pytest.raises(ob.ObligationRefused):
        ob.open_obligation(conn, tid, "landed:x", now=T0)
    with pytest.raises(ob.ObligationNotFound):
        ob.open_obligation(conn, "t_nope", "detached:x", now=T0)


def test_obligation_event_kinds_are_not_owner_wake_scan_kinds():
    from gateway import kanban_owner_wake

    scan = set(kanban_owner_wake.SCAN_KINDS)
    assert not {k for k in scan if k.startswith("obligation")}
    assert not scan & set(ob.EVENT_KINDS)


def test_policy_fallback_equals_registry_shape_and_reads_the_file(home):
    assert ob.load_policy() == ob.POLICY_DEFAULTS  # no registry file: compiled fallback
    reg = json.loads(json.dumps(ob.POLICY_DEFAULTS))
    reg["classes"]["decision"]["lease_min"] = 7
    (home / "fleet").mkdir()
    (home / "fleet" / "obligations-policy.json").write_text(json.dumps(reg))
    assert ob.load_policy()["classes"]["decision"]["lease_min"] == 7
    (home / "fleet" / "obligations-policy.json").write_text('{"classes": {"decision": {"lease_min": -1}}}')
    assert ob.load_policy() == ob.POLICY_DEFAULTS  # invalid file never half-applies


# --- §4.3 state machine + clocks (injected) ---------------------------------------------------

def test_lease_expiry_banks_at_claim_expires_and_second_expiry_escalates(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r1", created_at=T0, now=T0)
    a = ob.claim(conn, a.id, session="s1", now=T0 + 600)          # 10 min unclaimed
    assert a.state == ob.OPEN_CLAIMED and a.claim_expires == T0 + 600 + 1800 and a.holder_gen == 1
    assert ob.expire_leases(conn, now=T0 + 2399) == []            # lease still live
    [a] = ob.expire_leases(conn, now=T0 + 5000)                    # detected late
    assert a.state == ob.OPEN_UNCLAIMED and a.lease_expiries == 1
    assert a.claimed_s == 1800 and a.state_since == T0 + 2400      # banked AT claim_expires
    assert a.unclaimed_s == 600
    # unclaimed clock resumed at claim_expires: 600 + (t - 2400)
    assert ob.escalation_due(a, T0 + 2400 + 1199) is None
    assert ob.escalation_due(a, T0 + 2400 + 1200) == "unclaimed >= escalate_min (30 min)"
    a = ob.claim(conn, a.id, session="s1", now=T0 + 2500)
    [a] = ob.expire_leases(conn, now=T0 + 2500 + 1801)
    assert ob.escalation_due(a, T0 + 2500 + 1801) == "second lease expiry"
    assert _events(conn, tid) == ["obligation_opened", "obligation_claimed", "obligation_lease_expired",
                                  "obligation_claimed", "obligation_lease_expired"]


def test_clock_pauses_while_claimed_and_max_claim_caps_renewals(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r2", created_at=T0, now=T0)
    a = ob.claim(conn, a.id, session="s1", now=T0 + 60)
    t = T0 + 60
    while t < T0 + 60 + 7200 - 600:          # renew every 10 min for ~2 h
        t += 600
        a = ob.progress(conn, a.id, "waiting on prism", now=t)
        assert ob.escalation_due(a, t) is None, t   # unclaimed clock is paused
    assert ob.unclaimed_total(a, t) == 60
    assert ob.escalation_due(a, T0 + 60 + 7200) == "claimed >= max_claim_min (120 min)"


def test_progress_on_unclaimed_is_refused_and_does_not_renew(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r3", now=T0)
    with pytest.raises(ob.ObligationRefused, match="claim first"):
        ob.progress(conn, a.id, "x", now=T0 + 1)
    a = ob.claim(conn, a.id, session="s", now=T0 + 1)
    with pytest.raises(ob.ObligationRefused):
        ob.progress(conn, a.id, "   ", now=T0 + 2)


def test_executor_suspends_the_clock_and_release_resumes_it(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r4", created_at=T0, now=T0)
    a = ob.set_executor(conn, a.id, "land-8012l-9", rule="same-pr", now=T0 + 300)
    assert (a.state, a.holder_kind, a.unclaimed_s) == (ob.OPEN_EXECUTOR, "executor", 300)
    assert ob.escalation_due(a, T0 + 99_999) is None               # 27 h under a lander: no page
    a = ob.release_executor(conn, a.id, now=T0 + 99_999, why="operator --stop")
    assert a.state == ob.OPEN_UNCLAIMED and a.unclaimed_s == 300 and a.state_since == T0 + 99_999


def test_resolve_needs_a_real_resolution(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r5", now=T0)
    for bad in ("", "   ", "dropped:", "dropped:  ", "merged", "whatever:x"):
        with pytest.raises(ob.ObligationRefused):
            ob.resolve(conn, a.id, bad, now=T0 + 1)


def test_receipt_never_moves_state_or_gen(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:r6", now=T0)
    a = ob.claim(conn, a.id, session="s", now=T0 + 1)
    r = ob.receipt(conn, a.id, "answered", now=T0 + 2)
    r = ob.receipt(conn, a.id, "noanswer", now=T0 + 3)
    assert (r.state, r.holder_gen) == (a.state, a.holder_gen)
    assert (r.wake_attempts, r.first_wake_at, r.last_wake_at, r.last_wake_outcome) == (2, T0 + 2, T0 + 3, "noanswer")


# --- binding: P6-B1, P6-RC4, P6-RC5 -----------------------------------------------------------

def test_p6b1_land_named_watcher_is_an_unbound_receipt_and_cannot_resolve_green(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:land-8012d-1", repo="ANG-Ventures/hermes-home",
                              pr=8012, job_name="land-8012d", now=T0)
    kw = dict(job_name="land-8012-ci-wait", code=WATCHER)
    assert ob.decide_binding(conn, tid, **kw).status == "unbound"                              # --card
    assert ob.decide_binding(conn, tid, repo="ANG-Ventures/hermes-home", pr=8012, **kw).status == "unbound"
    assert ob.decide_binding(conn, tid, obligation_id=a.id, **kw).status == "refused"         # explicit
    # Name alone and --allow-lander alone are not landing code (the old wide predicate).
    assert not ob.is_landing_code("")
    assert not ob.is_landing_code(WATCHER)
    # The watcher exits green: its run id never holds A, so a green resolve on it is refused.
    with pytest.raises(ob.ObligationRefused, match="not bound"):
        ob.resolve(conn, a.id, "requeued-green:land-8012-ci-wait-77", now=T0 + 5)
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED
    # Only a bound lander resolves green.
    b = ob.decide_binding(conn, tid, job_name="land-8012l", code=LANDER,
                          repo="ANG-Ventures/hermes-home", pr=8012)
    assert (b.status, b.obligation_id, b.rule) == ("bound", a.id, "same-pr")
    ob.set_executor(conn, a.id, "land-8012l-88", rule=b.rule, now=T0 + 6)
    assert ob.resolve(conn, a.id, "requeued-green:land-8012l-88", now=T0 + 9).state == ob.RESOLVED


@pytest.mark.parametrize("code", [
    LANDER, "gh pr merge 8012 --squash", "gh api -X PUT repos/o/r/pulls/8012/merge",
    "python3 scripts/lib/landing_queue.py enqueue --repo o/r --pr 1", "apollo-land.py t_x",
    "python3 land_request.py --pr 3",
])
def test_landing_code_shapes_qualify(code):
    assert ob.is_landing_code(code)


def test_worker_owned_lander_requeue_is_a_receipt(conn):
    tid = _card(conn)
    ob.open_obligation(conn, tid, "detached:x1", repo="o/r", pr=1, now=T0)
    b = ob.decide_binding(conn, tid, job_name="land-x", code=LANDER, repo="o/r", pr=1, operator_owned=False)
    assert b.status == "unbound"


def test_p6rc4_binding_is_persisted_once_and_never_retargeted(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "queue:o/r#5@aaa#1", repo="o/r", pr=5, now=T0)
    ob.set_executor(conn, a.id, "queue:o/r#5@aaa#2", rule="queue-reenqueue", now=T0 + 1)
    # A row opening later never changes what the queue row is bound to.
    c, _ = ob.open_obligation(conn, tid, "detached:late", repo="o/r", pr=5, now=T0 + 2)
    b = ob.decide_binding(conn, tid, job_name=None, code="landing_queue.py", repo="o/r", pr=5,
                          holder_ref="queue:o/r#5@aaa#2")
    assert (b.status, b.obligation_id, b.rule) == ("bound", a.id, "persisted")
    with pytest.raises(ob.ObligationRefused, match="never retargeted"):
        ob.set_executor(conn, c.id, "queue:o/r#5@aaa#2", rule="x", now=T0 + 3)
    with pytest.raises(ob.ObligationRefused, match="never retargeted"):
        ob.set_executor(conn, a.id, "queue:o/r#5@bbb#3", rule="x", now=T0 + 3)
    # same ref again is a no-op, not a second holder change
    assert ob.set_executor(conn, a.id, "queue:o/r#5@aaa#2", rule="x", now=T0 + 4).holder_gen == 1


def test_p6rc5_null_identity_never_matches(conn):
    tid = _card(conn)
    ob.open_obligation(conn, tid, "detached:u1", now=T0)                   # unparsed repo/pr
    b = ob.decide_binding(conn, tid, job_name="land-other", code=LANDER)   # also unparsed
    assert b.status == "refused" and "--obligation" in b.reason             # not bound NULL==NULL
    tid2 = _card(conn, "two unrelated PRs")
    r1, _ = ob.open_obligation(conn, tid2, "detached:p1", job_name="land-a", now=T0)
    ob.open_obligation(conn, tid2, "detached:p2", job_name="land-b", now=T0)
    assert ob.decide_binding(conn, tid2, job_name="land-c", code=LANDER).status == "refused"
    # same-name still binds under its own contract
    b = ob.decide_binding(conn, tid2, job_name="land-a", code=LANDER)
    assert (b.status, b.obligation_id, b.rule) == ("bound", r1.id, "same-name")
    # explicit bind across a KNOWN different PR is refused
    r3, _ = ob.open_obligation(conn, tid2, "detached:p3", repo="o/r", pr=3, now=T0)
    assert ob.decide_binding(conn, tid2, job_name="j", code=LANDER, repo="o/r", pr=4,
                             obligation_id=r3.id).status == "refused"


# --- P6-RC8 one-transaction supersede ---------------------------------------------------------

def test_p6rc8_supersede_is_one_transaction(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:land-a-1", now=T0)
    a = ob.set_executor(conn, a.id, "land-b-2", rule="explicit", now=T0 + 1)
    old, succ = ob.supersede(conn, a.id, "detached:land-b-2", now=T0 + 2)
    assert old.resolution == "superseded:detached:land-b-2" and succ.state == ob.OPEN_UNCLAIMED
    # A lost CAS commits nothing, successor included, and never reverts to open_unclaimed.
    c, _ = ob.open_obligation(conn, tid, "detached:land-c-3", now=T0)
    with pytest.raises(ob.ObligationConflict):
        ob.supersede(conn, c.id, "detached:land-d-4", now=T0 + 3, expected_gen=c.holder_gen + 1)
    assert ob.get_by_source(conn, tid, "detached:land-d-4") is None
    assert ob.get(conn, c.id).state == ob.OPEN_UNCLAIMED and ob.get(conn, c.id).holder_gen == c.holder_gen


# --- P6-RC9 authority + receipts --------------------------------------------------------------

def _actor(**kw) -> ob.Actor:
    return ob.Actor(**{"profile": "default", "session": "s-op", "worker_profile": None,
                       "worker_env": (), **kw})


def test_p6rc9_worker_ancestry_cannot_gain_operator_authority(conn, home, monkeypatch):
    """Strip every worker env marker and set operator env: the process is still a worker by its
    ancestry (a task_runs row whose worker_pid + fingerprint is THIS process)."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    _operator_state_db(home, "s-op")
    wt = _card(conn, "the worker's own card")
    kb.claim_task(conn, wt)
    conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, profile = 'daedalus' "
                 "WHERE task_id = ?", (os.getpid(), _process_fingerprint(os.getpid()), wt))
    conn.commit()
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_ID", "s-op")
    actor = ob.current_actor()
    assert actor.worker_env == () and actor.worker_profile == "daedalus"
    assert "dispatched worker" in ob.operator_refusal(actor)
    monkeypatch.setenv("HERMES_KANBAN_TASK", wt)
    assert "worker-run marker" in ob.operator_refusal(ob.current_actor())


def test_operator_authority_needs_profile_session_and_state_db(home):
    _operator_state_db(home, "s-op")
    assert ob.operator_refusal(_actor()) is None
    assert "not an operator" in ob.operator_refusal(_actor(profile="daedalus"))
    assert "no session" in ob.operator_refusal(_actor(session=None))
    assert "not in any operator" in ob.operator_refusal(_actor(session="s-forged"))


def test_p6rc9_dropped_and_non_home_resolve_emit_comment_and_logs(conn, home, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(ob, "post_logs", sent.append)
    tid = _card(conn, session="s-home")
    a, _ = ob.open_obligation(conn, tid, "detached:d1", now=T0)
    b, _ = ob.open_obligation(conn, tid, "detached:d2", now=T0)
    c, _ = ob.open_obligation(conn, tid, "detached:d3", now=T0)
    actor = _actor(session="s-other")
    ob.emit_resolve_receipt(conn, ob.resolve(conn, a.id, "dropped:dupe of d2", now=T0 + 1), actor,
                            non_home=False, board=BOARD)
    ob.emit_resolve_receipt(conn, ob.resolve(conn, b.id, "handed:ace", now=T0 + 2), actor,
                            non_home=True, board=BOARD)
    assert ob.emit_resolve_receipt(conn, ob.resolve(conn, c.id, "handed:ace", now=T0 + 3), actor,
                                   non_home=False, board=BOARD) is None
    bodies = [r[0] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = ? ORDER BY id", (tid, ob.RECEIPT_AUTHOR))]
    assert len(bodies) == 2 and all(x.startswith(ob.RECEIPT_MARK) for x in bodies)
    assert "dropped" in bodies[0] and "non-home" in bodies[1] and "s-other" in bodies[1]
    assert len(sent) == 2 and all(tid in s for s in sent)
    assert ob.is_home_session(conn, tid, "s-home") and not ob.is_home_session(conn, tid, "s-other")


# --- INV-ONE-HOLDER: two processes race claim vs repair ---------------------------------------

_RACER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from hermes_cli import kanban_db_connect as kbc, kanban_work_obligations as ob
    who, ob_id, gen, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
    conn = kbc.connect(board="obligations-drill")
    while not go.exists():
        time.sleep(0.001)
    try:
        if who == "claim":
            ob.claim(conn, ob_id, session="s-op", expected_gen=gen)
        else:
            ob.set_executor(conn, ob_id, "land-repair-1", rule="repair", expected_gen=gen)
        print("WON")
    except ob.ObligationConflict:
        print("LOST")
""")


def test_cas_race_claim_vs_repair_exactly_one_wins(conn, home, tmp_path):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:race", now=T0)
    go = tmp_path / "go"
    env = {k: v for k, v in os.environ.items() if k not in WORKER_MARKERS}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO_ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", _RACER, who, a.id, str(a.holder_gen), str(go)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              cwd=REPO_ROOT)
             for who in ("claim", "repair")]
    time.sleep(1.5)  # both connected and spinning on the go file
    go.touch()
    outs = [p.communicate(timeout=60) for p in procs]
    results = sorted(o[0].strip() for o in outs)
    assert results == ["LOST", "WON"], outs
    final = ob.get(conn, a.id)
    assert final.holder_gen == a.holder_gen + 1
    assert final.state in (ob.OPEN_CLAIMED, ob.OPEN_EXECUTOR)


# --- E2E: CLI on the obligations-drill board --------------------------------------------------

def _cli(home: Path, tmp_path: Path, *argv: str, env_extra=None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in WORKER_MARKERS}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO_ROOT),
               HERMES_KANBAN_SANDBOX="1")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "--board", BOARD,
                           "obligation", *argv], env=env, capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)


def test_cli_e2e_open_claim_progress_resolve_with_events_in_order(conn, home, tmp_path):
    _operator_state_db(home, "s-op")
    tid = _card(conn, session="s-op")
    op = {"HERMES_PROFILE": "default", "HERMES_SESSION_ID": "s-op"}

    r = _cli(home, tmp_path, "open", tid, "--source-key", "detached:land-drill-1",
             "--summary", "rc 6: needs a ruling", "--repo", "o/r", "--pr", "1")
    assert r.returncode == 0, r.stderr
    oid = json.loads(r.stdout)["obligation"]["id"]
    again = _cli(home, tmp_path, "open", tid, "--source-key", "detached:land-drill-1")
    assert json.loads(again.stdout)["opened"] is False

    nosess = _cli(home, tmp_path, "claim", oid, env_extra={"HERMES_PROFILE": "default"})
    assert nosess.returncode == 2 and "no session" in nosess.stderr
    worker = _cli(home, tmp_path, "claim", oid, env_extra={**op, "HERMES_KANBAN_TASK": tid})
    assert worker.returncode == 2 and "worker-run marker" in worker.stderr
    early = _cli(home, tmp_path, "progress", oid, "looking", env_extra=op)
    assert early.returncode == 2 and "claim first" in early.stderr

    r = _cli(home, tmp_path, "claim", oid, env_extra=op)
    assert r.returncode == 0, r.stderr
    claimed = json.loads(r.stdout)
    assert claimed["obligation"]["state"] == "open_claimed" and claimed["non_home"] is False
    assert claimed["obligation"]["claim_session"] == "s-op"
    assert _cli(home, tmp_path, "progress", oid, "re-queued", "with", "fix", env_extra=op).returncode == 0
    assert _cli(home, tmp_path, "resolve", oid, "", env_extra=op).returncode == 2
    r = _cli(home, tmp_path, "resolve", oid, "merged:deadbeef", env_extra=op)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["obligation"]["state"] == "resolved"

    lst = json.loads(_cli(home, tmp_path, "list", "--card", tid).stdout)["obligations"]
    assert [o["state"] for o in lst] == ["resolved"]
    audit = _cli(home, tmp_path, "audit")
    assert audit.returncode == 0 and json.loads(audit.stdout)["open"] == 0

    rows = conn.execute("SELECT kind, actor_session_id FROM task_events WHERE task_id = ? "
                        "AND kind LIKE 'obligation_%' ORDER BY id", (tid,)).fetchall()
    assert [r[0] for r in rows] == ["obligation_opened", "obligation_claimed",
                                    "obligation_progress", "obligation_resolved"]
    assert rows[1][1] == "s-op"   # the claim is attributed to the claiming session


def test_cli_kill_switch_opens_nothing(conn, home, tmp_path):
    tid = _card(conn)
    r = _cli(home, tmp_path, "open", tid, "--source-key", "detached:off", env_extra={"OBLIGATIONS": "0"})
    assert r.returncode == 0 and json.loads(r.stdout) == {"disabled": True, "opened": False}
    assert ob.list_obligations(conn, task_id=tid) == []


def test_cli_audit_exits_1_on_an_overdue_row(conn, home, tmp_path):
    tid = _card(conn)
    ob.open_obligation(conn, tid, "detached:old", created_at=int(time.time()) - 31 * 60)
    r = _cli(home, tmp_path, "audit")
    assert r.returncode == 1 and len(json.loads(r.stdout)["overdue"]) == 1
