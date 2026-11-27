"""Process-slot gate + host-transient block auto-requeue (t_b660edb6).

2026-10-02: a runaway self-recursing microbench under one kanban worker took
the Studio from 1.4k to 10.3k processes (kern.maxprocperuid 10666) over 3 h
while load1 sat at 7-8. The load gate never paused, nothing paged, and once
the limit was hit every fork() failed with EAGAIN. A worker blocked
``kind=transient`` ("out of process slots ... requeue once the host
recovers") and sat 7h50m after recovery until a human unblocked it.

Contract:
* the gate pauses spawns (state ``proc_paused``) at ``proc_pause_fraction``
  of the per-uid limit, regardless of load1 or CPU headroom, and resumes only
  below ``proc_resume_fraction``;
* an unknown count or limit never pauses (fail open);
* once the gate admits again, ``transient`` blocks whose reason names host
  exhaustion are unblocked with a comment; other transient blocks, other
  kinds, and fresh blocks are left alone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_load_gate as klg
from hermes_cli.fleet_placement import admission as ad
from hermes_cli.kanban_load_gate import LoadGate


@pytest.fixture(autouse=True)
def _util_ctx(monkeypatch):
    """Hermetic utilization inputs: memory free, no ceiling, no PSI, no fleet demand (t_b20850d2: the gate is
    admit_class only, so a test that wants "the host has room" says so with cpu_busy, not with load1)."""
    monkeypatch.setattr(klg, "utilization_context", lambda: {
        "host": "mac-studio", "ceiling": None, "ceiling_reason": "",
        "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"), "limits": ad.Limits(mem_reserve_gb=0.0),
        "mem_available_gb": 400.0, "mem_total_gb": 512.0})
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                                                    "psi_io_some10": 0.0})
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _gate(**cfg) -> LoadGate:
    return LoadGate(dict(cfg), 32)


# ---------------------------------------------------------------------------
# Process-slot gate
# ---------------------------------------------------------------------------

def test_proc_gate_pauses_at_fraction_even_with_low_load_and_idle_cpu():
    g = _gate()
    allowance, reason = g.admit(7.5, load5=7.0, now=0, cpu_busy=0.1,
                                procs=8600, proc_limit=10666)
    assert allowance == 0
    assert g.state == "proc_paused"
    assert "8600" in reason and "10666" in reason and "EAGAIN" in reason
    assert not g.host_recovered()


def test_proc_gate_admits_below_fraction():
    g = _gate()
    allowance, reason = g.admit(7.5, load5=7.0, now=0, cpu_busy=0.1, procs=1450, proc_limit=10666)
    assert reason is None and allowance and allowance > 0
    assert g.state == "admitting"
    assert g.host_recovered()


def test_proc_gate_hysteresis_holds_until_resume_fraction():
    g = _gate()
    g.admit(7.0, now=0, cpu_busy=0.1, procs=8600, proc_limit=10666)
    # 0.70 of the limit: under the pause bar but over the 0.65 resume bar.
    allowance, _ = g.admit(7.0, now=60, cpu_busy=0.1, procs=7466, proc_limit=10666)
    assert allowance == 0 and g.state == "proc_paused"
    allowance, reason = g.admit(7.0, now=120, cpu_busy=0.1, procs=1448, proc_limit=10666)
    assert reason is None and allowance > 0 and g.state == "admitting"


@pytest.mark.parametrize("procs,limit", [(None, 10666), (9000, None), (9000, 0)])
def test_proc_gate_fails_open_when_unmeasurable(procs, limit):
    g = _gate()
    allowance, reason = g.admit(7.0, now=0, cpu_busy=0.1, procs=procs, proc_limit=limit)
    assert reason is None and allowance > 0


def test_proc_gate_holds_a_pause_through_an_unreadable_tick():
    g = _gate()
    g.admit(7.0, now=0, cpu_busy=0.1, procs=9000, proc_limit=10666)
    allowance, reason = g.admit(7.0, now=60, cpu_busy=0.1, procs=None, proc_limit=None)
    assert allowance == 0 and g.state == "proc_paused" and reason


def test_proc_limit_config_override_wins():
    g = _gate(proc_limit=1000)
    allowance, _ = g.admit(7.0, now=0, cpu_busy=0.1, procs=900, proc_limit=10666)
    assert allowance == 0 and g.state == "proc_paused"


def test_proc_pause_is_a_loud_line_and_in_snapshot(caplog):
    g = _gate()
    g.admit(7.0, now=0, cpu_busy=0.1, procs=9000, proc_limit=10666)
    with caplog.at_level(logging.INFO):
        g.log_tick(logging.getLogger("t"), now=0)
    rec = [r for r in caplog.records if "proc_paused" in r.getMessage()]
    assert rec and rec[0].levelno == logging.ERROR
    assert "PROCESS SLOTS" in rec[0].getMessage()
    snap = g.snapshot()
    assert snap["procs"] == 9000 and snap["proc_limit"] == 10666


def test_saturation_still_blocks_host_recovered():
    g = _gate()
    g.admit(80.0, load5=80.0, now=0, cpu_busy=0.99, procs=1400, proc_limit=10666)
    assert g.state == "saturated" and not g.host_recovered()


def test_sample_user_procs_reads_this_host():
    from hermes_cli.kanban_load_gate import sample_user_procs

    procs, limit = sample_user_procs()
    assert procs is None or procs >= 1
    assert limit is None or limit >= 1


# ---------------------------------------------------------------------------
# Host-transient auto-requeue
# ---------------------------------------------------------------------------

def _blocked(conn, reason, kind="transient", age=600):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    predicate = "\nunblock_when: ruling:host-recovery" if kind is None else ""
    assert kb.block_task(conn, tid, reason=reason + predicate, kind=kind)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - ? "
            "WHERE task_id = ? AND kind = 'blocked'",
            (age, tid),
        )
    return tid


EAGAIN_REASON = (
    "Studio is out of process slots: every fork returns EAGAIN "
    '("/etc/profile: fork: Resource temporarily unavailable", hook spawn '
    "BlockingIOError), so I cannot run git. Requeue once the host recovers."
)


def test_host_transient_block_is_requeued_with_comment(kanban_home):
    with kb.connect_closing() as conn:
        tid = _blocked(conn, EAGAIN_REASON)
        out = kb.requeue_host_transient_blocks(conn, note="gate admitting, procs=1448/10666")
        assert out == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
        assert any(
            c["author"] == "kanban-dispatcher" and "procs=1448/10666" in c["body"]
            for c in comments
        )
        ev = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert ev["kind"] == "unblocked"


@pytest.mark.parametrize("reason", [
    "Time-gated: the card says not to start before the xAI reset at 09:00 PT.",
    "judge error: ConflictError",
    "Waiting for operator-owned lander to merge #2001",
])
def test_non_host_transient_blocks_stay_blocked(kanban_home, reason):
    with kb.connect_closing() as conn:
        tid = _blocked(conn, reason)
        assert kb.requeue_host_transient_blocks(conn, note="n") == []
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("kind", ["needs_input", "capability", None])
def test_other_kinds_with_host_reason_stay_blocked(kanban_home, kind):
    with kb.connect_closing() as conn:
        tid = _blocked(conn, EAGAIN_REASON, kind=kind)
        assert kb.requeue_host_transient_blocks(conn, note="n") == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_fresh_host_transient_block_waits_min_age(kanban_home):
    with kb.connect_closing() as conn:
        tid = _blocked(conn, EAGAIN_REASON, age=0)
        assert kb.requeue_host_transient_blocks(conn, note="n") == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_requeue_keeps_recurrence_memory(kanban_home):
    """A host that flaps must still reach the loop breaker, not spin forever."""
    with kb.connect_closing() as conn:
        tid = _blocked(conn, EAGAIN_REASON)
        assert kb.requeue_host_transient_blocks(conn, note="n") == [tid]
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason=EAGAIN_REASON, kind="transient")
        assert kb.get_task(conn, tid).status == "triage"


def test_dispatcher_requeue_runs_only_when_gate_admits(kanban_home):
    from gateway import kanban_watchers as kw

    with kb.connect_closing() as conn:
        tid = _blocked(conn, EAGAIN_REASON)

    g = _gate()
    g.admit(7.0, now=0, cpu_busy=0.1, procs=9000, proc_limit=10666)
    note = kw._host_recovery_note(g)
    assert note is None
    with kb.connect_closing() as conn:
        assert kw._requeue_host_transient_on(conn, "default", note) == []
        assert kb.get_task(conn, tid).status == "blocked"

    g.admit(7.0, now=60, cpu_busy=0.1, procs=1448, proc_limit=10666)
    note = kw._host_recovery_note(g)
    assert note and "procs=1448/10666" in note
    with kb.connect_closing() as conn:
        assert kw._requeue_host_transient_on(conn, "default", note) == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_proc_gate_config_keys_are_declared_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    g = LoadGate(DEFAULT_CONFIG["kanban"]["dispatch_load_gate"], 32)
    assert g.proc_pause_fraction == 0.80
    assert g.proc_resume_fraction == 0.65
    assert g.proc_limit_override is None
