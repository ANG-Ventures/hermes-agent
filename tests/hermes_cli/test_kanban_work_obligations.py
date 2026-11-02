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

# The producer (detached-run / apollo-land / landing queue) declares whether a job lands; the
# fork never parses job text (Apollo ruling, Prism round 2 on #2533).
LANDER = True
WATCHER = False
TOKEN = "obligations-drill-operator-token"


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
    # In-process mutators (reconciler, drainer) run with the operator token; the authority
    # tests below remove it.
    (h / "kanban").mkdir(exist_ok=True)
    kb.operator_token_path().write_text(TOKEN)
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, TOKEN)
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
    # 08c3db594e35: the holder is gone, the binding is kept.
    assert a.holder_ref is None and a.holder_kind == "operator" and a.bound_ref == "land-8012l-9"


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
    kw = dict(job_name="land-8012-ci-wait", lands=WATCHER)
    assert ob.decide_binding(conn, tid, **kw).status == "unbound"                              # --card
    assert ob.decide_binding(conn, tid, repo="ANG-Ventures/hermes-home", pr=8012, **kw).status == "unbound"
    assert ob.decide_binding(conn, tid, obligation_id=a.id, **kw).status == "refused"         # explicit
    # The watcher exits green: its run id never holds A, so a green resolve on it is refused.
    with pytest.raises(ob.ObligationRefused, match="not bound"):
        ob.resolve(conn, a.id, "requeued-green:land-8012-ci-wait-77", now=T0 + 5)
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED
    # Only a bound lander resolves green.
    b = ob.decide_binding(conn, tid, job_name="land-8012l", lands=LANDER,
                          repo="ANG-Ventures/hermes-home", pr=8012)
    assert (b.status, b.obligation_id, b.rule) == ("bound", a.id, "same-pr")
    ob.set_executor(conn, a.id, "land-8012l-88", rule=b.rule, now=T0 + 6)
    assert ob.resolve(conn, a.id, "requeued-green:land-8012l-88", now=T0 + 9).state == ob.RESOLVED


def test_worker_owned_lander_requeue_is_a_receipt(conn):
    tid = _card(conn)
    ob.open_obligation(conn, tid, "detached:x1", repo="o/r", pr=1, now=T0)
    b = ob.decide_binding(conn, tid, job_name="land-x", lands=LANDER, repo="o/r", pr=1, operator_owned=False)
    assert b.status == "unbound"


def test_p6rc4_binding_is_persisted_once_and_never_retargeted(conn):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "queue:o/r#5@aaa#1", repo="o/r", pr=5, now=T0)
    ob.set_executor(conn, a.id, "queue:o/r#5@aaa#2", rule="queue-reenqueue", now=T0 + 1)
    # A row opening later never changes what the queue row is bound to.
    c, _ = ob.open_obligation(conn, tid, "detached:late", repo="o/r", pr=5, now=T0 + 2)
    b = ob.decide_binding(conn, tid, job_name=None, lands=LANDER, repo="o/r", pr=5,
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
    b = ob.decide_binding(conn, tid, job_name="land-other", lands=LANDER)   # also unparsed
    assert b.status == "refused" and "--obligation" in b.reason             # not bound NULL==NULL
    tid2 = _card(conn, "two unrelated PRs")
    r1, _ = ob.open_obligation(conn, tid2, "detached:p1", job_name="land-a", now=T0)
    ob.open_obligation(conn, tid2, "detached:p2", job_name="land-b", now=T0)
    assert ob.decide_binding(conn, tid2, job_name="land-c", lands=LANDER).status == "refused"
    # same-name still binds under its own contract
    b = ob.decide_binding(conn, tid2, job_name="land-a", lands=LANDER)
    assert (b.status, b.obligation_id, b.rule) == ("bound", r1.id, "same-name")
    # explicit bind across a KNOWN different PR is refused
    r3, _ = ob.open_obligation(conn, tid2, "detached:p3", repo="o/r", pr=3, now=T0)
    assert ob.decide_binding(conn, tid2, job_name="j", lands=LANDER, repo="o/r", pr=4,
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
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO_ROOT),
               KANBAN_OPERATOR_TOKEN=TOKEN)
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
               HERMES_KANBAN_SANDBOX="1", KANBAN_OPERATOR_TOKEN=TOKEN)
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


# --- Prism round 1 on #2533 (Apollo ruling: all P1s, one push) --------------------------------

def test_binding_survives_release_and_claim_and_is_never_retargeted(conn):
    """15a74715fd28 + 6c403d160479: bound_ref is write-once, independent of holder_kind."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:A", now=T0)
    b, _ = ob.open_obligation(conn, tid, "detached:B", now=T0)
    ob.set_executor(conn, a.id, "run-1", rule="explicit", now=T0 + 1)
    a = ob.release_executor(conn, a.id, now=T0 + 2)
    assert a.bound_ref == "run-1" and a.holder_kind == "operator"
    with pytest.raises(ob.ObligationRefused, match="never retargeted"):
        ob.set_executor(conn, b.id, "run-1", rule="explicit", now=T0 + 3)
    with pytest.raises(ob.ObligationRefused):
        ob.resolve(conn, b.id, "requeued-green:run-1", now=T0 + 4)
    # a claim overwrites holder_ref with the session; the binding stays
    a = ob.claim(conn, a.id, session="s-op", now=T0 + 5)
    assert a.bound_ref == "run-1" and a.holder_ref == "s-op"
    with pytest.raises(ob.ObligationRefused, match="never retargeted"):
        ob.set_executor(conn, a.id, "run-2", rule="explicit", now=T0 + 6)
    assert ob.decide_binding(conn, tid, job_name=None, lands=LANDER, holder_ref="run-1").obligation_id == a.id
    # the original job re-holding its own row is allowed
    assert ob.set_executor(conn, a.id, "run-1", rule="explicit", now=T0 + 7).state == ob.OPEN_EXECUTOR


def test_producer_declares_lands_and_the_fork_never_reads_job_text(conn):
    """9870151e9903 + 23635639de47 (Apollo ROOT 2): lands=False is an unbound receipt and refuses
    --obligation; lands=True follows the binding rules. There is no text input to misread."""
    tid = _card(conn)
    o, _ = ob.open_obligation(conn, tid, "detached:c1", repo="o/r", pr=1, job_name="land-1", now=T0)
    assert ob.decide_binding(conn, tid, job_name="land-1", lands=False, repo="o/r", pr=1).status == "unbound"
    assert ob.decide_binding(conn, tid, job_name="w", lands=False, obligation_id=o.id).status == "refused"
    b = ob.decide_binding(conn, tid, job_name="w", lands=True, repo="o/r", pr=1)
    assert (b.status, b.obligation_id, b.rule) == ("bound", o.id, "same-pr")
    assert not hasattr(ob, "is_landing_code")
    with pytest.raises(TypeError):
        ob.decide_binding(conn, tid, job_name="w", code="echo land")  # no text path left


def test_progress_after_lease_expiry_is_refused_and_the_expiry_is_banked(conn):
    """084b6e67d069."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:e1", created_at=T0, now=T0)
    ob.claim(conn, a.id, session="s", now=T0, lease_s=60)
    with pytest.raises(ob.ObligationConflict, match="lease expired") as exc:
        ob.progress(conn, a.id, "update", now=T0 + 120, lease_s=60)
    assert exc.value.rc == ob.RC_CONFLICT
    a = ob.get(conn, a.id)
    assert (a.state, a.lease_expiries, a.claimed_s, a.state_since) == (ob.OPEN_UNCLAIMED, 1, 60, T0 + 60)
    assert _events(conn, tid)[-1] == "obligation_lease_expired"


def test_audit_counts_an_escalated_row_still_past_its_bound(conn, home, tmp_path):
    """0b614a798c3e: escalated_at acknowledges the page, it does not clear the violation."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "blocked:e1", created_at=int(time.time()) - 31 * 60)
    ob.mark_escalated(conn, a.id)
    assert ob.escalation_due(ob.get(conn, a.id), int(time.time())) is None
    assert ob.bound_violation(ob.get(conn, a.id), int(time.time())) is not None
    r = _cli(home, tmp_path, "audit")
    assert r.returncode == 1 and json.loads(r.stdout)["overdue"] == [a.id]


def test_resolve_superseded_needs_an_existing_successor(conn):
    """5a66819210c2."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:s1", now=T0)
    with pytest.raises(ob.ObligationRefused, match="supersede"):
        ob.resolve(conn, a.id, "superseded:blocked:missing", now=T0 + 1)
    with pytest.raises(ob.ObligationRefused, match="supersede"):
        ob.resolve(conn, a.id, "superseded:detached:s1", now=T0 + 1)   # itself
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED
    ob.open_obligation(conn, tid, "blocked:present", now=T0)
    assert ob.resolve(conn, a.id, "superseded:blocked:present", now=T0 + 2).state == ob.RESOLVED


def test_unverifiable_worker_ancestry_fails_closed(conn, home, monkeypatch):
    """f799f2e773a9: a live run row on our ancestry with an `unverified` fingerprint, an
    unreadable board, or an exception refuses; an operator outside any worker tree passes."""
    _operator_state_db(home, "s-op")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_ID", "s-op")
    assert ob.operator_refusal(ob.current_actor()) is None          # outside any worker tree
    wt = _card(conn, "unverified worker")
    kb.claim_task(conn, wt)
    conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = 'unverified', "
                 "profile = 'daedalus' WHERE task_id = ?", (os.getpid(), wt))
    conn.commit()
    why = ob.operator_refusal(ob.current_actor())
    assert why and "unverif" in why
    conn.execute("UPDATE task_runs SET ended_at = ? WHERE task_id = ?", (T0, wt))
    conn.commit()
    assert ob.operator_refusal(ob.current_actor()) is None          # an ended run is history
    from hermes_cli import kanban_identity as ki
    monkeypatch.setattr(ob, "_live_run_rows", lambda chain: ([], False))
    assert "indeterminate" in ob.operator_refusal(ob.current_actor())
    monkeypatch.setattr(ki, "_ancestry", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "indeterminate" in ob.operator_refusal(ob.current_actor())


def test_operator_profile_compare_is_byte_exact(home):
    _operator_state_db(home, "s-op")
    assert "not an operator" in ob.operator_refusal(_actor(profile="Default"))


def test_board_transfer_clears_obligation_holders_and_keeps_the_binding(conn, home, tmp_path):
    """5e52d26c2578."""
    from hermes_cli import kanban_transfer as kt

    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:x", now=T0)
    ob.set_executor(conn, a.id, "source-host-run", rule="explicit", now=T0 + 1)
    c, _ = ob.open_obligation(conn, tid, "detached:y", now=T0)
    ob.claim(conn, c.id, session="s-src", now=T0 + 1)
    done, _ = ob.open_obligation(conn, tid, "detached:z", now=T0)
    ob.resolve(conn, done.id, "merged:abc", now=T0 + 2)
    archive = kt.export_board(BOARD, str(tmp_path / "out"))["archive"]
    slug = kt.import_board(archive, slug="obl-imported")["board"]
    with kbc.connect_closing(board=slug) as c2:
        rows = {o.source_key: o for o in ob.list_obligations(c2)}
    assert rows["detached:x"].state == ob.OPEN_UNCLAIMED and rows["detached:x"].holder_ref is None
    assert rows["detached:x"].bound_ref == "source-host-run"
    assert rows["detached:y"].state == ob.OPEN_UNCLAIMED and rows["detached:y"].claim_session is None
    assert rows["detached:z"].state == ob.RESOLVED and rows["detached:z"].resolution == "merged:abc"
    # the destination's clock runs again: 30 min after import with no holder it escalates
    assert ob.escalation_due(rows["detached:x"], int(time.time()) + 31 * 60) is not None


# --- Prism round 2 on #2533 (Apollo ruling: ROOT 1 clocks, ROOT 2 lands, fingerprint) ---------

LEASE = 1800
# (offset from claim, claimed_s, unclaimed_s, lease_expiries): before, at, after claim_expires.
_STINT_CASES = [(1000, 1000, 0, 0), (LEASE, LEASE, 0, 0), (3660, LEASE, 1860, 1)]


@pytest.mark.parametrize("off,want_cl,want_un,want_exp", _STINT_CASES)
@pytest.mark.parametrize("path", ["bank-executor", "bank-resolve", "bank-supersede", "total", "transfer"])
def test_claimed_stint_splits_at_claim_expires_everywhere(conn, monkeypatch, path, off, want_cl,
                                                          want_un, want_exp):
    """c3dbaa56c41a / 7d4c51761297 / 5bc956bf76fd: time after claim_expires is UNCLAIMED time and
    the expiry is counted, whichever path banks or reads it first (sweep timing never matters)."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:s", created_at=T0, now=T0)
    a = ob.claim(conn, a.id, session="s", now=T0, lease_s=LEASE)
    t = T0 + off
    assert ob.split_stint(a, t) == (want_cl, want_un, bool(want_exp))
    if path == "total":
        got = (ob.claimed_total(a, t), ob.unclaimed_total(a, t), a.lease_expiries + ob.split_stint(a, t)[2])
    else:
        if path == "bank-executor":
            ob.set_executor(conn, a.id, "run-x", rule="explicit", now=t)
        elif path == "bank-resolve":
            ob.resolve(conn, a.id, "merged:abc", now=t)
        elif path == "bank-supersede":
            ob.supersede(conn, a.id, "blocked:next", now=t)
        else:
            from hermes_cli import kanban_transfer as kt
            monkeypatch.setattr(kt.time, "time", lambda: float(t))
            kt._scrub_local_state(conn)
            conn.commit()
        r = ob.get(conn, a.id)
        got = (r.claimed_s, r.unclaimed_s, r.lease_expiries)
    assert got == (want_cl, want_un, want_exp)


def test_audit_sees_unclaimed_time_after_an_unswept_expiry(conn):
    """5bc956bf76fd repro: open+claim at 100 with a 60 s lease, no sweep, read at 2000."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:u", created_at=100, now=100)
    ob.claim(conn, a.id, session="s", now=100, lease_s=60)
    assert ob.bound_violation(ob.get(conn, a.id), 2000) == "unclaimed >= escalate_min (30 min)"


def test_live_run_with_unavailable_current_fingerprint_is_indeterminate(conn, home, monkeypatch):
    """7e667df77683: a live candidate run whose CURRENT fingerprint cannot be read refuses."""
    from hermes_cli import kanban_db_dispatch as kdd

    _operator_state_db(home, "s-op")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_ID", "s-op")
    wt = _card(conn, "leader exited")
    kb.claim_task(conn, wt)
    conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, profile = 'daedalus' "
                 "WHERE task_id = ?", (os.getpid(), kdd._process_fingerprint(os.getpid()), wt))
    conn.commit()
    monkeypatch.setattr(kdd, "_process_fingerprint", lambda pid: None)
    why = ob.operator_refusal(ob.current_actor())
    assert why and "indeterminate" in why
    conn.execute("UPDATE task_runs SET ended_at = ? WHERE task_id = ?", (T0, wt))
    conn.commit()
    assert ob.operator_refusal(ob.current_actor()) is None   # an ended run is history


def test_supersession_never_targets_a_resolved_row(conn):
    """2d470c0e95d3 (round-2 text): B superseding back to resolved A would close a cycle."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:a", now=T0)
    _, b = ob.supersede(conn, a.id, "blocked:b", now=T0 + 1)
    with pytest.raises(ob.ObligationRefused, match="resolved"):
        ob.supersede(conn, b.id, "detached:a", now=T0 + 2)
    with pytest.raises(ob.ObligationRefused, match="resolved"):
        ob.resolve(conn, b.id, "superseded:detached:a", now=T0 + 2)
    assert ob.get(conn, b.id).state == ob.OPEN_UNCLAIMED


def test_executor_after_an_unswept_expiry_keeps_the_unclaimed_time(conn):
    """6c403d160479 repro: claim at 0, executor at 3600, release at 3660 -> escalation due."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:x6", created_at=T0, now=T0)
    ob.claim(conn, a.id, session="s", now=T0)
    ob.set_executor(conn, a.id, "run-6", rule="explicit", now=T0 + 3600)
    a = ob.release_executor(conn, a.id, now=T0 + 3660)
    assert (a.claimed_s, a.unclaimed_s, a.lease_expiries) == (1800, 1800, 1)
    assert ob.escalation_due(a, T0 + 3660) == "unclaimed >= escalate_min (30 min)"


# --- Prism round 3 on #2533 (Apollo 03:1x: authority inside the mutators, cascade, release) -----

_RESTRICTED = (
    ("claim", lambda c, i: ob.claim(c, i, session="s-op", now=T0 + 1)),
    ("executor", lambda c, i: ob.set_executor(c, i, "run-x", rule="explicit", now=T0 + 1)),
    ("resolve dropped:", lambda c, i: ob.resolve(c, i, "dropped:dupe", now=T0 + 1)),
    ("resolve handed:", lambda c, i: ob.resolve(c, i, "handed:ace", now=T0 + 1)),
    ("supersede", lambda c, i: ob.supersede(c, i, "blocked:9", now=T0 + 1)),
)


@pytest.mark.parametrize("verb,call", _RESTRICTED, ids=[v for v, _ in _RESTRICTED])
def test_direct_import_from_a_worker_is_refused_inside_the_mutator(conn, home, monkeypatch, verb, call):
    """c1b6fe03cdb8: a direct import with HERMES_KANBAN_TASK set gets no more than the CLI,
    even holding the operator token."""
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:w", now=T0)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(ob.ObligationRefused, match="worker-run marker"):
        call(conn, a.id)
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED and ob.get(conn, a.id).holder_gen == 0


def test_release_from_a_worker_is_refused_inside_the_mutator(conn, home, monkeypatch):
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:w", now=T0)
    ob.set_executor(conn, a.id, "run-1", rule="explicit", now=T0 + 1)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(ob.ObligationRefused, match="worker-run marker"):
        ob.release_executor(conn, a.id, now=T0 + 2)
    assert ob.get(conn, a.id).state == ob.OPEN_EXECUTOR


@pytest.mark.parametrize("verb,call", _RESTRICTED, ids=[v for v, _ in _RESTRICTED])
def test_no_token_and_no_live_operator_gateway_is_refused(conn, home, monkeypatch, verb, call):
    """849c3be13734: operator env (profile + a session in an operator state.db) is not proof."""
    _operator_state_db(home, "s-op")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_ID", "s-op")
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:w", now=T0)
    with pytest.raises(ob.ObligationRefused, match="no operator proof"):
        call(conn, a.id)
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED
    ob.resolve(conn, a.id, "merged:abc", now=T0 + 2)   # an observed outcome needs no proof


_ORPHAN = textwrap.dedent("""
    from hermes_cli import kanban_db_connect as kbc, kanban_work_obligations as ob
    import sys
    conn = kbc.connect(board="obligations-drill")
    try:
        ob.resolve(conn, sys.argv[1], "dropped:orphan helper")
        print("RESOLVED")
    except ob.ObligationRefused as exc:
        print("REFUSED", exc)
""")


def test_setsid_orphan_helper_without_token_cannot_drop(conn, home, tmp_path):
    """849c3be13734 repro: a setsid'd helper (no worker ancestor, no token, no live operator
    gateway) setting operator env is refused by the mutator itself."""
    _operator_state_db(home, "s-op")
    tid = _card(conn)
    a, _ = ob.open_obligation(conn, tid, "detached:orphan", now=T0)
    env = {k: v for k, v in os.environ.items() if k not in WORKER_MARKERS + (kb.OPERATOR_TOKEN_ENV,)}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO_ROOT),
               HERMES_KANBAN_SANDBOX="1", HERMES_PROFILE="default", HERMES_SESSION_ID="s-op")
    r = subprocess.run([sys.executable, "-c", _ORPHAN, a.id], env=env, capture_output=True, text=True,
                       cwd=REPO_ROOT, timeout=120, start_new_session=True)
    assert r.stdout.startswith("REFUSED") and "no operator proof" in r.stdout, (r.stdout, r.stderr)
    assert ob.get(conn, a.id).state == ob.OPEN_UNCLAIMED


def test_hard_delete_of_a_card_leaves_no_obligation_row(conn):
    """69225d94e7a7: the delete cascade includes obligations."""
    tid = _card(conn)
    ob.open_obligation(conn, tid, "detached:gone", now=T0)
    kb.archive_task(conn, tid)
    assert kb.delete_archived_task(conn, tid)
    assert conn.execute("SELECT count(*) FROM obligations WHERE task_id = ?", (tid,)).fetchone()[0] == 0


# --- Prism 337337e7c32a residue of #2533: progress() checks the caller's card ----------------

def _claimed_pair(conn):
    """Two cards, each with a CLAIMED row (claimed while the fixture holds the operator token)."""
    a_card, b_card = _card(conn, "card A"), _card(conn, "card B")
    a, _ = ob.open_obligation(conn, a_card, "detached:pa", now=T0)
    b, _ = ob.open_obligation(conn, b_card, "detached:pb", now=T0)
    ob.claim(conn, a.id, session="s-op", now=T0 + 1)
    ob.claim(conn, b.id, session="s-op", now=T0 + 1)
    return a_card, a, b_card, b


def _become_worker_of(conn, monkeypatch, task_id, env_task=None):
    """This process is the verified live worker of ``task_id`` (run row pid + fingerprint)."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    kb.claim_task(conn, task_id)
    conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, profile = 'daedalus' "
                 "WHERE task_id = ?", (os.getpid(), _process_fingerprint(os.getpid()), task_id))
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", env_task or task_id)


@pytest.mark.parametrize("forged_env", [False, True], ids=["own-env", "env-forged-to-B"])
def test_progress_direct_import_from_another_cards_worker_is_refused(conn, monkeypatch, forged_env):
    a_card, _a, b_card, b = _claimed_pair(conn)
    _become_worker_of(conn, monkeypatch, a_card, env_task=b_card if forged_env else None)
    before = ob.get(conn, b.id).claim_expires
    with pytest.raises(ob.ObligationRefused, match="progress refused"):
        ob.progress(conn, b.id, "renewing someone else's claim", now=T0 + 60)
    assert ob.get(conn, b.id).claim_expires == before
    assert _events(conn, b_card)[-1] == "obligation_claimed"


def test_progress_from_the_obligations_own_card_worker_is_allowed(conn, monkeypatch):
    a_card, a, _b_card, _b = _claimed_pair(conn)
    _become_worker_of(conn, monkeypatch, a_card)
    r = ob.progress(conn, a.id, "own card moved", now=T0 + 60, lease_s=600)
    assert r.claim_expires == T0 + 60 + 600 and _events(conn, a_card)[-1] == "obligation_progress"


def test_progress_from_an_operator_with_token_is_allowed_and_without_proof_refused(conn, home, monkeypatch):
    _a_card, a, _b_card, _b = _claimed_pair(conn)
    assert ob.progress(conn, a.id, "operator moved it", now=T0 + 60, lease_s=600).claim_expires == T0 + 660
    _operator_state_db(home, "s-op")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_ID", "s-op")
    with pytest.raises(ob.ObligationRefused, match="progress refused"):
        ob.progress(conn, a.id, "no proof", now=T0 + 120, lease_s=600)
    assert ob.get(conn, a.id).claim_expires == T0 + 660
