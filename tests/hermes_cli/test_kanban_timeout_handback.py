"""A worker that already pushed its PR times out into review, not a retry (t_e7537cb1).

Incident t_e019e240 (2026-10-30): PR pushed 32 min into a 90 min run; the worker then idled for a
Prism round, the dispatcher killed it at 5419 s and set the card back to ``ready`` with the finished
PR on the branch. These tests pin: the handback routes to review, a run with no pushed head still
retries, the soft deadline fires once per run, and the spawn env carries the worker's clock.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_policy as kwp


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    kb.init_db()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)  # SIGTERM "worked" at once
    yield tmp_path


def _running(conn, *, limit: int, started_ago: int, contract: str = "ANG-Ventures/hermes-home"):
    tid = kb.create_task(conn, title="lane drill", assignee="daedalus-opus",
                         max_runtime_seconds=limit, completion_contract=contract)
    assert kb.claim_task(conn, tid) is not None
    kbd._set_worker_pid(conn, tid, os.getpid())
    start = int(time.time()) - started_ago
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (start, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = "
                     "(SELECT current_run_id FROM tasks WHERE id = ?)", (start, tid))
    return tid, start


def _sig(pid, sig):
    return None


def test_timed_out_run_with_pushed_pr_goes_to_review(home):
    seen = {}

    def handback(conn, tid, run_started_at):
        seen["args"] = (tid, run_started_at)
        return {"pr": "ANG-Ventures/hermes-home#7842", "sha": "0207294" + "a" * 33, "url": None}

    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=60, started_ago=120)
        out = kbd.enforce_max_runtime(conn, signal_fn=_sig, handback_fn=handback)
        assert tid in out
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.worker_pid is None and task.claim_lock is None
        assert seen["args"] == (tid, start)
        events = kb.list_events(conn, tid)
        to = next(e for e in events if e.kind == "timed_out")
        assert to.payload["handback_pr"] == "ANG-Ventures/hermes-home#7842"
        assert to.payload["retry_status"] == "review"
        assert any(e.kind == "review_requested" and e.payload.get("timed_out_handback") for e in events)
        assert not any(e.kind == "gave_up" for e in events)
        run = kb.list_runs(conn, tid)[-1]
        assert run.outcome == "timed_out"
        assert run.summary.startswith("timed-out handback: PR ANG-Ventures/hermes-home#7842 @")
        assert run.metadata["own_prs"] == ["ANG-Ventures/hermes-home#7842"]
        # The failure counter is not charged for a delivered handback.
        assert int(conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?",
                                (tid,)).fetchone()[0] or 0) == 0


def test_timed_out_run_without_pushed_head_retries_as_before(home):
    with kbc.connect_closing() as conn:
        tid, _ = _running(conn, limit=60, started_ago=120)
        out = kbd.enforce_max_runtime(conn, signal_fn=_sig, handback_fn=lambda *a: None)
        assert tid in out
        assert kb.get_task(conn, tid).status == "ready"
        to = next(e for e in kb.list_events(conn, tid) if e.kind == "timed_out")
        assert to.payload["retry_status"] == "ready"
        assert "handback_pr" not in to.payload


def test_handback_lookup_error_retries(home):
    def boom(*a):
        raise RuntimeError("gh down")

    with kbc.connect_closing() as conn:
        tid, _ = _running(conn, limit=60, started_ago=120)
        kbd.enforce_max_runtime(conn, signal_fn=_sig, handback_fn=boom)
        assert kb.get_task(conn, tid).status == "ready"


def test_incident_replay_pr_at_32m_kill_at_90m_is_review(home, monkeypatch):
    """t_e019e240 shape: 90 min cap, head pushed at t+32m, killed at t+90m19s -> review.
    The real lookup runs against a stubbed gh: the push time from the activity API decides."""
    limit = 5400
    calls = []

    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=limit, started_ago=5419)
        pushed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + 32 * 60))

        def fake_gh(endpoint):
            calls.append(endpoint)
            if "/pulls?" in endpoint:
                return [{"number": 7842, "html_url": "u",
                         "head": {"ref": f"daedalus/{tid}-apple-egress-lane", "sha": "c0ffee" * 6 + "abcd"}},
                        {"number": 1, "head": {"ref": "daedalus/t_00000000-other", "sha": "f" * 40}}]
            assert "/activity?ref=refs/heads/daedalus/" in endpoint
            return [{"activity_type": "push", "after": "c0ffee" * 6 + "abcd", "timestamp": pushed}]

        monkeypatch.setattr(kbd, "_gh_json", fake_gh)
        kbd.enforce_max_runtime(conn, signal_fn=_sig, handback_fn=kbd.timed_out_handback_pr)
        assert kb.get_task(conn, tid).status == "review"
        to = next(e for e in kb.list_events(conn, tid) if e.kind == "timed_out")
        assert to.payload["handback_pr"] == "ANG-Ventures/hermes-home#7842"
        assert calls[0].startswith("repos/ANG-Ventures/hermes-home/pulls?state=open")


def test_head_older_than_run_start_is_not_a_handback(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=60, started_ago=120)
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start - 3600))
        monkeypatch.setattr(kbd, "_gh_json", lambda ep: (
            [{"number": 9, "head": {"ref": f"wt/{tid}", "sha": "a" * 40}}] if "/pulls?" in ep
            else [{"activity_type": "push", "after": "a" * 40, "timestamp": stale}]))
        assert kbd.timed_out_handback_pr(conn, tid, start) is None


def _ts(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def test_future_dated_commit_pushed_before_run_is_not_a_handback(home, monkeypatch):
    """Prism 50102547ef05: pushed 11:50, committer date 12:05, run 12:00-12:10 pushed nothing.
    The push time decides, so the stale PR is not this run's handback -> retry."""
    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=600, started_ago=610)
        sha = "b" * 40

        def fake_gh(ep):
            if "/pulls?" in ep:
                return [{"number": 9, "head": {"ref": f"wt/{tid}", "sha": sha}}]
            if "/commits/" in ep:  # the committer date would wrongly say "during the run"
                return {"commit": {"committer": {"date": _ts(start + 300)}}}
            return [{"activity_type": "push", "after": sha, "timestamp": _ts(start - 600)}]

        monkeypatch.setattr(kbd, "_gh_json", fake_gh)
        assert kbd.timed_out_handback_pr(conn, tid, start) is None
        kbd.enforce_max_runtime(conn, signal_fn=_sig, handback_fn=kbd.timed_out_handback_pr)
        assert kb.get_task(conn, tid).status == "ready"


def test_old_commit_pushed_during_run_is_a_handback(home, monkeypatch):
    """An old local commit force-pushed during the run counts: push time, not commit date."""
    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=60, started_ago=120)
        sha = "c" * 40
        monkeypatch.setattr(kbd, "_gh_json", lambda ep: (
            [{"number": 9, "head": {"ref": f"wt/{tid}", "sha": sha}}] if "/pulls?" in ep
            else [{"activity_type": "force_push", "after": sha, "timestamp": _ts(start + 30)}]))
        hb = kbd.timed_out_handback_pr(conn, tid, start)
        assert hb and hb["sha"] == sha


def test_latest_push_not_the_pr_head_is_not_a_handback(home, monkeypatch):
    """The newest ref update must be the push of the PR's head sha; anything else retries."""
    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=60, started_ago=120)
        monkeypatch.setattr(kbd, "_gh_json", lambda ep: (
            [{"number": 9, "head": {"ref": f"wt/{tid}", "sha": "d" * 40}}] if "/pulls?" in ep
            else [{"activity_type": "push", "after": "e" * 40, "timestamp": _ts(start + 30)}]))
        assert kbd.timed_out_handback_pr(conn, tid, start) is None


def test_soft_deadline_emitted_once_per_run_at_limit_minus_margin(home):
    limit = 5400
    margin = kwp.soft_margin_seconds(limit)
    assert margin == 810  # max(600, 15% of 5400)
    assert kwp.soft_margin_seconds(1800) == 600
    with kbc.connect_closing() as conn:
        tid, start = _running(conn, limit=limit, started_ago=0)
        soft = start + limit - margin
        assert kbd.emit_soft_deadlines(conn, now=soft - 1) == []
        assert kbd.emit_soft_deadlines(conn, now=soft) == [tid]
        assert kbd.emit_soft_deadlines(conn, now=soft + 60) == []
        events = [e for e in kb.list_events(conn, tid) if e.kind == "soft_deadline"]
        assert len(events) == 1
        assert events[0].payload["hard_at"] == start + limit
        comments = [c for c in kb.list_comments(conn, tid)
                    if c.body.startswith(kwp.SOFT_DEADLINE_COMMENT_PREFIX)]
        assert len(comments) == 1 and "hard kill at" in comments[0].body


def test_spawn_env_carries_deadline_epoch(home, tmp_path, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="clock", assignee="a", max_runtime_seconds=5400)
        claimed = kb.claim_task(conn, tid)
        start = conn.execute("SELECT started_at FROM task_runs WHERE id=?",
                             (claimed.current_run_id,)).fetchone()[0]
    captured = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, *a, **kw):
        captured["env"] = kw.get("env", {})
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ws = tmp_path / "ws"
    ws.mkdir()
    kbd._default_spawn(claimed, str(ws))
    env = captured["env"]
    assert env[kwp.DEADLINE_ENV] == str(int(start) + 5400)
    assert env[kwp.MAX_RUNTIME_ENV] == "5400"


def test_worker_context_renders_deadline_line(home):
    with kbc.connect_closing() as conn:
        tid, _ = _running(conn, limit=5400, started_ago=10)
        ctx = kb.build_worker_context(conn, tid)
    assert "Deadline: Hard limit at " in ctx and "hand back by " in ctx
